# -*- coding: utf-8 -*-
"""抽两帧交给本地 qwen3.8 判断运动性质（自然运动 / 形变拉扯 / 抖动）。"""
import base64
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

import ai

FF = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"

ASK = (
    "这是同一段视频的前后两帧。客观回答三点：\n"
    "1) 两帧之间主体发生了什么变化（位置移动 / 姿态变化 / 还是只是整体拉伸形变）？\n"
    "2) 画面清晰度和结构是否稳定（有没有糊、扭曲、崩坏）？\n"
    "3) 整体观感像【有生命的人在自然活动】，还是像【一张静止图被拉扯变形】？\n"
    "三句话直说，不要客套。"
)


def sample(video: Path, times=(0.2, 2.5)) -> list:
    d = Path(tempfile.mkdtemp())
    out = []
    for i, t in enumerate(times):
        p = d / f"f{i}.png"
        subprocess.run([FF, "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(p)],
                       capture_output=True, timeout=60)
        if p.exists():
            out.append(base64.b64encode(p.read_bytes()).decode())
    return out


def main() -> None:
    vid = Path(sys.argv[1] if len(sys.argv) > 1
               else r"D:\ComfyUI_Wan\output\video\director_juqing_00_00003_.mp4")
    if not vid.exists():
        print("视频不存在:", vid)
        return
    imgs = sample(vid)
    print(f"抽帧 {len(imgs)} 张 → qwen3.8 判读中…")
    payload = {"model": ai.UNCENSORED_MODEL, "prompt": ASK, "images": imgs, "stream": False,
               "think": False, "options": {"num_predict": 500, "num_ctx": 8192}}
    r = httpx.post(ai.OLLAMA_GEN, json=payload, timeout=900)
    print(r.json().get("response", "").strip()[:800])


if __name__ == "__main__":
    main()
