# -*- coding: utf-8 -*-
"""康波演讲片 v3 产线：长镜头 + TTS 原声铺轨（音画对齐正确解）。

架构定案（2026-09-12 夜，用户批准）：
1. 演讲全文按语义切段：每段 ≤38 字（TTS +0% 自然语速 ≈ 8s 内）
2. 每段 TTS（zh-CN-YunjianNeural 同音色）→ wav（真实时长）
3. 视频：Ref2VA 生成「正在演讲」口型动作，**不带 ref_audio**（模型音频通道静音/房间底噪）
   192 帧（8s 网格 17*11+5=192）@ 512×640，参考图=定妆照+三视图（两张锚人）
4. 合成：ffmpeg 每段「视频 + TTS 原声」混流（视频 8s > 音频则尾部补静音；音频 > 视频则截断音频）
   → 声音 100% 清晰逐字正确，口型为持续演讲状态
5. 拼接：段间 concat（各段音轨即 TTS 原声，连续）+ 字幕全文 + 后期统一超分/配乐

幂等断点续跑；--start/--limit/--final 同 v2。控制 output/_control.txt。
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
TTS_DIR = OUTPUT / "_kangbo3_tts"
SEGS_DIR = OUTPUT / "_kangbo3_segs"
FINAL = OUTPUT / "kangbo_v3_final.mp4"
REF_IMG = Path(r"D:\ComfyUI_Wan\output\kangbo_card\D_front_00001_.png")
VOICE = "zh-CN-YunjianNeural"
RATE = "+0%"
SEED_BASE = 20260915
LENGTH = 192          # 17*11+5 网格，8.0s @24fps
STEPS = 20
W, H = 512, 640
MAX_CHARS = 38        # TTS 自然语速 8s ≈ 38 字
SUBS = OUTPUT / "_kangbo3_subs.srt"

PROMPT_TMPL = (
    "Live-action documentary style, cinematic. The speaker from <Picture 1> stands at a wooden podium "
    "with two gooseneck microphones in a modern conference hall with dark blue stage backdrop, a large "
    "presentation screen, warm stage lights, seated audience silhouettes in the dim foreground. "
    "He delivers his speech in Chinese: his mouth moves naturally in continuous speech, calm authoritative manner, "
    "subtle natural gestures, occasional glance at the screen then back to the audience. "
    "His face, thin round metal glasses, dark suit, white shirt, dark tie and grey-streaked side-parted hair "
    "stay exactly consistent with <Picture 1> throughout. "
    "overall_soundscape: quiet conference room tone with soft room reverb only, no speech, no music. "
    "non_diegetic_music: N/A. Static camera with very small drift. Sharp focus, fine detail."
)


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
    return units


def tts_shot(i: int, text: str) -> tuple:
    """TTS → (wav, dur)。不加 pad——原声直接铺轨。"""
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
    r = subprocess.run([FFMPEG, "-i", str(wav)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return wav, round(dur, 2)


def upload_ref() -> str:
    up = httpx.post(
        "http://127.0.0.1:8188/upload/image",
        files={"image": (REF_IMG.name, REF_IMG.read_bytes(), "image/png")},
        data={"overwrite": "true", "type": "input"},
        timeout=60,
    )
    up.raise_for_status()
    return up.json()["name"]


def build_workflow(prompt: str, seed: int, ref_img: str) -> dict:
    return {
        "1": {"class_type": "H3ModelLoaderAny", "inputs": {"model_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": ref_img}},
        "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt,
            "width": W, "height": H, "length": LENGTH,
            "ref_image_size": "match",
            "ref_images.ref_image_0": ["5", 0],
        }},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "beta", "steps": STEPS, "denoise": 1.0}},
        "11": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["7", 0]}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["8", 0], "guider": ["11", 0], "sampler": ["9", 0], "sigmas": ["10", 0], "latent_image": ["7", 1]}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
        "14": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["12", 1], "vae": ["4", 0]}},
        "15": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "audio": ["14", 0], "fps": 24, "bit_depth": 8}},
        "16": {"class_type": "SaveVideo", "inputs": {"video": ["15", 0], "filename_prefix": "kangbo_v3/seg", "format": "auto", "codec": "auto"}},
    }


def mux_video_tts(seg_video: Path, tts_wav: Path, out: Path) -> bool:
    """视频（模型 8s，含底噪音轨）+ TTS 原声铺轨 → out。
    TTS 短于视频：apad 补静音到视频长；TTS 长于视频：截断（-shortest）。
    视频流 copy；音轨取 TTS（amix 不留底噪，直接替换）。"""
    cmd = [
        FFMPEG, "-y",
        "-i", str(seg_video), "-i", str(tts_wav),
        "-filter_complex", "[1:a]apad[a1];[0:a]volume=0[a0];[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "14", "-preset", "slow",   # 高码率重编码：治 CreateVideo 262kbps 的压缩糊
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    return out.exists() and r.returncode == 0


def stage_videos(start: int, limit: int, shots: list) -> None:
    SEGS_DIR.mkdir(exist_ok=True)
    ref_img = upload_ref()
    log(f"定妆照已上传: {ref_img}")
    end = min(len(shots), start + limit) if limit else len(shots)
    for i in range(start, end):
        final_seg = SEGS_DIR / f"seg_{i:03d}.mp4"
        raw_seg = SEGS_DIR / f"raw_{i:03d}.mp4"
        if final_seg.exists():
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
        if dur > 8.0:
            log(f"镜 {i:03d} TTS {dur:.1f}s 超 8s，跳过（切句异常，需人工看）")
            continue
        wf = build_workflow(PROMPT_TMPL, SEED_BASE + i, ref_img)
        success = False
        for attempt in range(3):
            pid = comfy.submit(wf)
            log(f"镜 {i + 1} 生成（{len(text)} 字 / TTS {dur:.1f}s / attempt {attempt + 1}）…")
            ok, history = comfy.wait_done(pid, timeout=5400)
            if ok:
                v = comfy.find_video(history)
                if v:
                    comfy.download_video(v, raw_seg)
                    if mux_video_tts(raw_seg, wav, final_seg):
                        raw_seg.unlink(missing_ok=True)
                        log(f"镜 {i:03d} 完成（视频+TTS铺轨）")
                        success = True
                    break
            log(f"镜 {i:03d} attempt {attempt + 1} 失败，等 20s 重试")
            time.sleep(20)
        if not success:
            log(f"镜 {i:03d} 三次失败，跳过（--start {i} 补跑）")


def stage_final(shots: list) -> None:
    segs = [SEGS_DIR / f"seg_{i:03d}.mp4" for i in range(len(shots))]
    have = [(i, p) for i, p in enumerate(segs) if p.exists()]
    if not have:
        log("没有任何段，无法拼接")
        return
    lines = []
    cursor = 0.0
    for n, (i, p) in enumerate(have):
        r = subprocess.run([FFMPEG, "-i", str(p)], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 8.0
        lines.append(f"{n + 1}\n{_ts(cursor)} --> {_ts(cursor + min(d, len(shots[i]) / 4.5 + 0.8))}\n{shots[i]}\n")
        cursor += d
    SUBS.write_text("\n".join(lines), encoding="utf-8")
    list_file = OUTPUT / "_kangbo3_concat.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for _, p in have), encoding="utf-8")
    concat = OUTPUT / "kangbo_v3_concat.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(concat)],
                   capture_output=True, timeout=1800)
    vf_script = OUTPUT / "_vf3.txt"
    vf_script.write_text(
        f"subtitles=filename={SUBS.name}:force_style='FontName=Microsoft YaHei,FontSize=9,Outline=1,MarginV=22'",
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
    log(f"演讲切镜：{len(shots)} 段（每段 ≤{MAX_CHARS} 字 / 8s 长镜）")
    (OUTPUT / "_kangbo3_shots.json").write_text(json.dumps(shots, ensure_ascii=False, indent=1), encoding="utf-8")
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
