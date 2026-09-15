# -*- coding: utf-8 -*-
"""康波演讲片 v4 产线：帧数跟随 TTS 时长（模型逐字复述，自带同步语音）。

## 为什么是 v4（2026-09-15 ASR 实证）

三方 ASR 转写逐字一致 → H3 的 ref_audio 确实**逐字复述** TTS 内容：
  TTS 原声      : 人生發財靠康播財富積累完全來源於經濟週期給你的機會
  v2_seg01(基准): 人生發財靠康播財富積累完全來源於經濟週期給你的機會
  repro(复现)   : 人生發財靠康播財富積累完全來源於經濟週期給你的機會

**失败样本的统一解释**：音频短于视频（靠 pad 补静音）→ 模型尾部无内容可依 → 自由发挥（含糊/外语）。
**正确做法**：反过来让**视频 ≤ 音频**——按 TTS 实际时长选网格帧数，音频紧凑覆盖全程。

## 关键参数（全部实测）
- 帧网格 17k+5；帧数 = 最大的网格 ≤ audio_dur×24
- 每段 TTS ≤30 字（≈6.2s，141帧，显存安全）
- 124帧/640×832/20步 euler+beta；1帧≈25秒生成
- 音频与视频皆为模型输出（自带回放），**不铺轨、不 pad**

用法：--start/--limit/--final；控制 output/_control.txt
"""
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"d:\VideoStudio")
sys.stdout.reconfigure(encoding="utf-8")
import edge_tts
import httpx

import comfy
from shared.paths import FFMPEG, OUTPUT

RAW = Path(r"d:\VideoStudio\juben\kangbo.txt")
CONTROL = OUTPUT / "_control.txt"
TTS_DIR = OUTPUT / "_kangbo4_tts"
SEGS_DIR = OUTPUT / "_kangbo4_segs"
FINAL = OUTPUT / "kangbo_v4_final.mp4"
REF_IMG = Path(r"D:\ComfyUI_Wan\output\kangbo_card\D_front_00001_.png")
VOICE = "zh-CN-YunjianNeural"
RATE = "+15%"
SEED_BASE = 20260913
STEPS = 20
W, H = 640, 832
MAX_CHARS = 20      # 短段提高逐字成功率（30字实测失败、21/27字成功）
SUBS = OUTPUT / "_kangbo4_subs.srt"

GRID_FRAMES = [17 * k + 5 for k in range(1, 22)]   # 22 … 362

# ---- 自动质检（ASR 转写比对）：逐字复述是概率性的（实测二元分布：要么全对要么乱码）----
CER_OK = 0.25          # 合格阈值：转写与原文的字符错误率
MAX_RETRY = 5          # 不合格时换 seed 重做的最多次数
_ASR = None


def verify_segment(seg_path: Path, ref_text: str) -> float:
    """ASR 转写视频语音，返回与原文的 CER（越小越好）。ASR 失败返回 1.0（判不合格）。"""
    global _ASR
    try:
        if _ASR is None:
            from faster_whisper import WhisperModel
            log("加载 ASR 质检模型（small/CPU）…")
            _ASR = WhisperModel("small", device="cpu", compute_type="int8")
        wav = TTS_DIR / f"_qc_{seg_path.stem}.wav"
        subprocess.run([FFMPEG, "-y", "-i", str(seg_path), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                       capture_output=True, timeout=180)
        segs, _ = _ASR.transcribe(str(wav), language="zh", beam_size=5)
        hyp = "".join(s.text for s in segs).strip()
        wav.unlink(missing_ok=True)
        return _cer(hyp, ref_text)
    except Exception as e:
        log(f"质检异常（视为不合格）：{e}")
        return 1.0


def _cer(hyp: str, ref: str) -> float:
    norm = lambda s: re.sub(r"[^\w一-鿿]", "", s)
    h, r = norm(hyp), norm(ref)
    if not r:
        return 1.0
    d = [[0] * (len(r) + 1) for _ in range(len(h) + 1)]
    for i in range(len(h) + 1):
        d[i][0] = i
    for j in range(len(r) + 1):
        d[0][j] = j
    for i in range(1, len(h) + 1):
        for j in range(1, len(r) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (0 if h[i - 1] == r[j - 1] else 1))
    return d[len(h)][len(r)] / len(r)

PROMPT_TMPL = (
    "Live-action documentary style, cinematic. The speaker from <Picture 1> stands at a wooden podium "
    "with two gooseneck microphones in a modern conference hall with dark blue stage backdrop, warm stage light. "
    "He delivers his speech in Chinese with natural lip-sync and calm authoritative manner, small hand gestures. "
    "He speaks the exact words heard in <Audio 1>. His face, thin round metal glasses, dark suit and grey-streaked hair "
    "stay exactly consistent with <Picture 1>. "
    "overall_soundscape: conference room tone, his clear steady male voice speaking the words of <Audio 1>. "
    "non_diegetic_music: N/A. Static camera with very small drift. Sharp focus, fine detail."
)


def pick_frames(audio_dur: float) -> int:
    """最大的网格帧数，使 视频时长 ≤ 音频时长（音频紧凑覆盖 → 逐字复述）。0 = 音频太短。"""
    max_frames = int(audio_dur * 24)
    ok = [f for f in GRID_FRAMES if f <= max_frames]
    return ok[-1] if ok else 0


def control() -> str:
    try:
        return CONTROL.read_text(encoding="utf-8").strip() or "RUN"
    except Exception:
        return "RUN"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_sentences() -> list:
    raw = RAW.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or re.match(r"^\d{8,}", s):
            continue
        if any(s.startswith(k) for k in ("主持人", "时间：", "地点：", "会议：", "人生就是康波时间：")) or s == "人生就是康波":
            continue
        lines.append(s)
    text = "".join(lines)
    text = re.sub(r"周金涛：", "", text)
    return [s for s in re.split(r"(?<=[。！？；])", text) if s.strip()]


def build_shots() -> list:
    units = []
    for sent in load_sentences():
        sent = sent.strip()
        if len(sent) <= MAX_CHARS:
            units.append(sent)
            continue
        parts = [p for p in re.split(r"(?<=[，、])", sent) if p.strip()]
        buf = ""
        for p in parts:
            if len(buf) + len(p) > MAX_CHARS and buf:
                units.append(buf.strip())
                buf = p
            else:
                buf += p
        if buf.strip():
            units.append(buf.strip())
    # 二次硬切：仍超长的按 MAX_CHARS 强切（长英文/数字句按逗号切不动）
    final = []
    for u in units:
        while len(u) > MAX_CHARS + 5:
            final.append(u[:MAX_CHARS])
            u = u[MAX_CHARS:]
        if u.strip():
            final.append(u)
    return final


def tts_shot(i: int, text: str) -> tuple:
    """TTS → (wav, dur)。不 pad。"""
    TTS_DIR.mkdir(exist_ok=True)
    # 缓存按「文本内容」命名——段切分变化时自动重生成，避免复用旧文本音频
    import hashlib
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    mp3 = TTS_DIR / f"t{i:03d}_{h}.mp3"
    wav = TTS_DIR / f"t{i:03d}_{h}.wav"
    if not mp3.exists():
        for attempt in range(3):
            try:
                asyncio.run(edge_tts.Communicate(text, VOICE, rate=RATE).save(str(mp3)))
                break
            except Exception as e:
                log(f"TTS {i} retry {attempt + 1}: {e}")
                time.sleep(5)
    if not wav.exists():
        subprocess.run([FFMPEG, "-y", "-i", str(mp3), "-ar", "32000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=120)
    r = subprocess.run([FFMPEG, "-i", str(wav)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return wav, round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2) if m else 0.0


def upload_ref() -> str:
    up = httpx.post("http://127.0.0.1:8188/upload/image",
                    files={"image": (REF_IMG.name, REF_IMG.read_bytes(), "image/png")},
                    data={"overwrite": "true", "type": "input"}, timeout=60)
    up.raise_for_status()
    return up.json()["name"]


def build_workflow(prompt: str, seed: int, ref_img: str, wav_path: Path, length: int) -> dict:
    return {
        "1": {"class_type": "H3ModelLoaderAny", "inputs": {"model_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": ref_img}},
        "6": {"class_type": "VHS_LoadAudio", "inputs": {"audio_file": str(wav_path)}},
        "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt, "width": W, "height": H, "length": length,
            "ref_image_size": "match",
            "ref_images.ref_image_0": ["5", 0],
            "ref_audios.ref_audio_0": ["6", 0],
        }},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "beta", "steps": STEPS, "denoise": 1.0}},
        "11": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["7", 0]}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["8", 0], "guider": ["11", 0], "sampler": ["9", 0], "sigmas": ["10", 0], "latent_image": ["7", 1]}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
        "14": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["12", 1], "vae": ["4", 0]}},
        "15": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "audio": ["14", 0], "fps": 24, "bit_depth": 8}},
        "16": {"class_type": "SaveVideo", "inputs": {"video": ["15", 0], "filename_prefix": "kangbo_v4/seg", "format": "auto", "codec": "auto"}},
    }


def stage_videos(start: int, limit: int, shots: list) -> None:
    SEGS_DIR.mkdir(exist_ok=True)
    ref_img = upload_ref()
    log(f"定妆照已上传: {ref_img}")
    end = min(len(shots), start + limit) if limit else len(shots)
    for i in range(start, end):
        seg = SEGS_DIR / f"seg_{i:03d}.mp4"
        if seg.exists():
            log(f"镜 {i:03d} 已存在，跳过")
            continue
        while True:
            c = control()
            if c == "STOP":
                log("STOP，产线退出")
                return
            if c != "PAUSE":
                break
            log("暂停中（PAUSE）…")
            time.sleep(60)
        text = shots[i]
        wav, dur = tts_shot(i, text)
        length = pick_frames(dur)
        if length < 39:      # 太短（<1.6s）不值得出镜，合并到下一段
            log(f"镜 {i:03d} TTS {dur:.2f}s 过短（{length}帧），跳过")
            continue
        success = False
        for attempt in range(MAX_RETRY):
            seed = SEED_BASE + i + attempt * 7919        # 每次换 seed（质检不合格时重做）
            wf = build_workflow(PROMPT_TMPL, seed, ref_img, wav, length)
            pid = comfy.submit(wf)
            log(f"镜 {i + 1} 生成（{len(text)}字 / {dur:.2f}s / {length}帧={length/24:.2f}s / seed {seed} / # {attempt + 1}）…")
            ok, history = comfy.wait_done(pid, timeout=5400)
            if ok:
                v = comfy.find_video(history)
                if v:
                    comfy.download_video(v, seg)
                    score = verify_segment(seg, text)     # 自动质检：ASR 转写比对原文
                    if score <= CER_OK:
                        log(f"镜 {i:03d} 合格（CER {score:.0%}）")
                        success = True
                        break
                    log(f"镜 {i:03d} 质检不合格（CER {score:.0%}，语音非人话）→ 换 seed 重做")
                    seg.unlink(missing_ok=True)
                    continue
            log(f"镜 {i:03d} # {attempt + 1} 生成失败（多为 OOM），等 20s")
            time.sleep(20)
        if not success:
            log(f"镜 {i:03d} {MAX_RETRY} 次仍不合格，跳过待人工")


def stage_final(shots: list) -> None:
    segs = [SEGS_DIR / f"seg_{i:03d}.mp4" for i in range(len(shots))]
    have = [(i, p) for i, p in enumerate(segs) if p.exists()]
    if not have:
        log("没有任何段")
        return
    lines, cursor = [], 0.0
    for n, (i, p) in enumerate(have):
        r = subprocess.run([FFMPEG, "-i", str(p)], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 5.0
        lines.append(f"{n + 1}\n{_ts(cursor)} --> {_ts(cursor + d - 0.12)}\n{shots[i]}\n")
        cursor += d
    SUBS.write_text("\n".join(lines), encoding="utf-8")
    list_file = OUTPUT / "_kangbo4_concat.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for _, p in have), encoding="utf-8")
    concat = OUTPUT / "kangbo_v4_concat.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(concat)],
                   capture_output=True, timeout=1800)
    vf = OUTPUT / "_vf4.txt"
    vf.write_text("subtitles=filename=_kangbo4_subs.srt:force_style='FontName=Microsoft YaHei,FontSize=9,Outline=1,MarginV=22'",
                  encoding="utf-8")
    subprocess.run([FFMPEG, "-y", "-i", str(concat), "-filter_complex_script", str(vf),
                    "-c:v", "libx264", "-crf", "14", "-preset", "slow", "-c:a", "copy", str(FINAL)],
                   capture_output=True, timeout=1800, cwd=str(OUTPUT))
    log(f"成片 -> {FINAL}" if FINAL.exists() else f"烧字幕失败，无字幕版 {concat}")


def _ts(sec: float) -> str:
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}".replace(".", ",")


def main() -> None:
    args = sys.argv[1:]
    start = int(args[args.index("--start") + 1]) if "--start" in args else 0
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
    shots = build_shots()
    log(f"演讲切段：{len(shots)} 段（每段 ≤{MAX_CHARS} 字，帧数跟随 TTS 时长）")
    (OUTPUT / "_kangbo4_shots.json").write_text(json.dumps(shots, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--final" in args:
        stage_final(shots)
        return
    if not comfy.is_ready():
        log("引擎未启动")
        return
    stage_videos(start, limit, shots)
    stage_final(shots)


if __name__ == "__main__":
    main()
