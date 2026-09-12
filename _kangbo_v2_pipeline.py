# -*- coding: utf-8 -*-
"""康波演讲片 v2 产线：真语音驱动（TTS 音色锚 + 照稿说话 + 口型同步）。

架构（语音为主轴）：
1. 演讲全文按句切镜（每镜 ≤27 字，视频 124 帧 5.17s，TTS +15% 语速）
2. 每镜 TTS（zh-CN-YunjianNeural 同一音色全程）→ wav
3. Ref2VA 生成：ref_images=真人定妆 + ref_audios=该镜 TTS（音色锚）
   提示词要求「说 <Audio 1> 的原话」→ 口型与模型音轨同步，音色与 TTS 一致
4. 段拼接（concat，各段自带音轨）→ 字幕 = 演讲原文全文
5. 背景音乐不生成（用户要求后加），成片为干净人声

幂等断点续跑；每镜约 25 分钟。控制 output/_control.txt = RUN/PAUSE/STOP。
参数：--start N 从第 N 镜开始（0 起）；--limit N 最多生成 N 镜；--final 只做拼接。
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
TTS_DIR = OUTPUT / "_kangbo2_tts"
SEGS_DIR = OUTPUT / "_kangbo2_segs"
FINAL = OUTPUT / "kangbo_v2_final.mp4"
REF_IMG = Path(r"D:\ComfyUI_Wan\output\kangbo\lecturer_real_v2_00001_.png")
VOICE = "zh-CN-YunjianNeural"
RATE = "+15%"
SEED_BASE = 20260913
LENGTH = 124
STEPS = 20
W, H = 640, 832
MAX_CHARS = 27          # 每镜讲稿字数上限（约 5.1s 语音）
SUBS = OUTPUT / "_kangbo2_subs.srt"
SUBTITLE_FONT = "Microsoft YaHei"

PROMPT_TMPL = (
    "Live-action documentary style, cinematic. The speaker from <Picture 1> stands at a wooden podium "
    "with two gooseneck microphones in a modern conference hall with dark blue stage backdrop, warm stage lights. "
    "He delivers his speech in Chinese with natural lip-sync, calm authoritative manner, subtle natural gestures. "
    "He speaks the exact words heard in <Audio 1>, continuing those same words to the very end of the shot — "
    "he never switches language, never mumbles, never falls silent. "
    "His face, thin round metal glasses, dark suit, white shirt and grey-streaked side-parted hair stay exactly "
    "consistent with <Picture 1> throughout. "
    "overall_soundscape: quiet conference room tone, his clear steady male voice speaking the words of <Audio 1> "
    "for the entire duration. non_diegetic_music: N/A. Static camera with very small drift. Sharp focus, fine detail."
)


def control() -> str:
    try:
        return CONTROL.read_text(encoding="utf-8").strip() or "RUN"
    except Exception:
        return "RUN"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_sentences() -> list:
    """演讲全文（只留周金涛发言）→ 句子列表。"""
    raw = RAW.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or re.match(r"^\d{8,}", s):
            continue
        # 头部元信息整行丢弃
        if any(s.startswith(k) for k in ("主持人", "时间：", "地点：", "会议：", "人生就是康波时间：")) or s == "人生就是康波":
            continue
        lines.append(s)
    text = "".join(lines)
    text = re.sub(r"周金涛：", "", text)  # 角色前缀（开头出现一次）
    sents = [s for s in re.split(r"(?<=[。！？；])", text) if s.strip()]
    return sents


def build_shots() -> list:
    """句子 → 镜（每镜 ≤MAX_CHARS 字，不跨句硬切：超长句按逗号分，标点保留）。"""
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
    return units


def tts_shot(i: int, text: str) -> tuple:
    """TTS 单镜 → (wav_path, dur)。幂等。音频恒补 0.7s 静音尾巴（必须 ≥ 视频时长）。"""
    TTS_DIR.mkdir(exist_ok=True)
    mp3 = TTS_DIR / f"t{i:03d}.mp3"
    wav = TTS_DIR / f"t{i:03d}.wav"
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
    # 恒定补静音：无论首次还是复用，pad 文件每次检查补齐
    padded = TTS_DIR / f"t{i:03d}_pad.wav"
    if not padded.exists():
        r0 = subprocess.run([FFMPEG, "-i", str(wav)], capture_output=True, text=True)
        m0 = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r0.stderr)
        d0 = int(m0.group(1)) * 3600 + int(m0.group(2)) * 60 + float(m0.group(3)) if m0 else 0.0
        pad_to = max(0.0, 5.5 - d0)   # 目标 5.5s：比 124帧(5.17s) 长 0.33s，模型全程有语音可依
        subprocess.run(
            [FFMPEG, "-y", "-i", str(wav), "-af", f"apad=pad_dur={pad_to:.2f}", str(padded)],
            capture_output=True, timeout=120,
        )
    r = subprocess.run([FFMPEG, "-i", str(padded)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return padded, round(dur, 2)


def upload_ref() -> str:
    up = httpx.post(
        "http://127.0.0.1:8188/upload/image",
        files={"image": (REF_IMG.name, REF_IMG.read_bytes(), "image/png")},
        data={"overwrite": "true", "type": "input"},
        timeout=60,
    )
    up.raise_for_status()
    return up.json()["name"]


def build_workflow(prompt: str, seed: int, ref_img: str, wav_path: Path) -> dict:
    return {
        "1": {"class_type": "H3ModelLoaderAny", "inputs": {"model_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": ref_img}},
        "6": {"class_type": "VHS_LoadAudio", "inputs": {"audio_file": str(wav_path)}},
        "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt,
            "width": W, "height": H, "length": LENGTH,
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
        "16": {"class_type": "SaveVideo", "inputs": {"video": ["15", 0], "filename_prefix": "kangbo_v2/seg", "format": "auto", "codec": "auto"}},
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
                log("STOP，产线退出（已完成镜保留）")
                return
            if c != "PAUSE":
                break
            log("暂停中（PAUSE）…")
            time.sleep(60)
        text = shots[i]
        wav, dur = tts_shot(i, text)
        wf = build_workflow(PROMPT_TMPL, SEED_BASE + i, ref_img, wav)
        # OOM 高发（8GB 显存临界），失败自动重试最多 3 次（每次重新提交，显存状态独立）
        success = False
        for attempt in range(3):
            pid = comfy.submit(wf)
            log(f"镜 {i + 1}/{len(shots)} 提交（{len(text)} 字 / TTS {dur:.1f}s / attempt {attempt + 1}）…")
            ok, history = comfy.wait_done(pid, timeout=5400)
            if ok:
                v = comfy.find_video(history)
                if v:
                    comfy.download_video(v, seg)
                    log(f"镜 {i:03d} 完成")
                    success = True
                    break
            log(f"镜 {i:03d} attempt {attempt + 1} 失败（多为 OOM），等 20s 重试")
            time.sleep(20)
        if not success:
            log(f"镜 {i:03d} 三次失败，跳过（--start {i} 补跑）")


def stage_final(shots: list) -> None:
    segs = [SEGS_DIR / f"seg_{i:03d}.mp4" for i in range(len(shots))]
    have = [(i, p) for i, p in enumerate(segs) if p.exists()]
    if not have:
        log("没有任何段，无法拼接")
        return
    # 字幕：有视频的镜按顺序排时间轴（时长按真实段时长读取）
    lines = []
    cursor = 0.0
    for n, (i, p) in enumerate(have):
        r = subprocess.run([FFMPEG, "-i", str(p)], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 5.17
        lines.append(f"{n + 1}\n{_ts(cursor)} --> {_ts(cursor + d - 0.15)}\n{shots[i]}\n")
        cursor += d
    SUBS.write_text("\n".join(lines), encoding="utf-8")
    # concat（各段自带音轨）
    list_file = OUTPUT / "_kangbo2_concat.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for _, p in have), encoding="utf-8")
    concat = OUTPUT / "kangbo_v2_concat.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(concat)],
                   capture_output=True, timeout=1800)
    # 烧字幕（filter_complex_script + cwd 规避 Windows 转义）
    vf_script = OUTPUT / "_vf2.txt"
    vf_script.write_text(
        f"subtitles=filename={SUBS.name}:force_style='FontName={SUBTITLE_FONT},FontSize=9,Outline=1,MarginV=22'",
        encoding="utf-8",
    )
    subprocess.run([FFMPEG, "-y", "-i", str(concat), "-filter_complex_script", str(vf_script),
                    "-c:a", "copy", str(FINAL)], capture_output=True, timeout=1800, cwd=str(OUTPUT))
    if FINAL.exists():
        log(f"成片完成 -> {FINAL}（{len(have)} 镜）")
    else:
        log(f"烧字幕失败，无字幕版 {concat}")


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def main() -> None:
    args = sys.argv[1:]
    start = int(args[args.index("--start") + 1]) if "--start" in args else 0
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
    shots = build_shots()
    log(f"演讲切镜：{len(shots)} 镜（每镜 ≤{MAX_CHARS} 字）")
    (OUTPUT / "_kangbo2_shots.json").write_text(json.dumps(shots, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--final" in args:
        stage_final(shots)
        return
    if not comfy.is_ready():
        log("引擎未启动，退出")
        return
    stage_videos(start, limit, shots)
    stage_final(shots)


if __name__ == "__main__":
    main()
