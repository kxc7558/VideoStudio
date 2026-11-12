# -*- coding: utf-8 -*-
"""用 Real-ESRGAN 对成片做 AI 超分辨率增强（免费本地方案）。

工作流：
  1. 把视频拆成帧序列；
  2. Real-ESRGAN 逐帧处理（4070 跑 4K 约 1 帧/秒）；
  3. 帧序列重组成视频（保持 24fps）。

时间：1 分 43 秒（2478 帧）× 1 秒/帧 ≈ 41 分钟。
"""
import subprocess
import sys
from pathlib import Path

VIDEO = Path(r"D:\VideoStudio\output\1e9d3c87cc3b.mp4")  # 480p 原版
FFMPEG = Path(r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")
ESRGAN = Path(r"D:\Real-ESRGAN-ncnn\realesrgan-ncnn-vulkan.exe")
WORK = Path(r"D:\VideoStudio\upscale_work")
OUT = VIDEO.with_stem(VIDEO.stem + "_esrgan_4k")

WORK.mkdir(exist_ok=True)
FRAMES_IN = WORK / "frames_in"
FRAMES_OUT = WORK / "frames_out"
FRAMES_IN.mkdir(exist_ok=True)
FRAMES_OUT.mkdir(exist_ok=True)


def log(msg: str):
    print(msg, flush=True)


def extract_frames():
    """拆帧：视频 → 帧序列（PNG）。"""
    log("拆帧中（约 30 秒）…")
    subprocess.run(
        [str(FFMPEG), "-i", str(VIDEO), "-q:v", "1",
         str(FRAMES_IN / "frame_%05d.png")],
        capture_output=True, timeout=300,
    )
    count = len(list(FRAMES_IN.glob("*.png")))
    log(f"已拆出 {count} 帧")
    return count


def upscale_frames(count: int):
    """Real-ESRGAN 批处理（逐帧增强到 4K）。"""
    out_count = len(list(FRAMES_OUT.glob("*.png")))
    if out_count >= count:
        log(f"已完成全部 {out_count} 帧，跳过处理")
        return out_count
    log(f"继续 AI 增强（已完成 {out_count}/{count}，预计剩余 {(count - out_count) // 60} 分钟）…")
    subprocess.run(
        [str(ESRGAN),
         "-i", str(FRAMES_IN),
         "-o", str(FRAMES_OUT),
         "-n", "realesrgan-x4plus",
         "-g", "0",  # GPU 0
         "-f", "png"],
        timeout=14400,  # 4 小时
    )
    out_count = len(list(FRAMES_OUT.glob("*.png")))
    log(f"完成 {out_count}/{count} 帧")
    return out_count


def merge_frames():
    """帧序列 → 视频（24fps，高清编码）。"""
    log("重组成视频（约 2 分钟）…")
    subprocess.run(
        [str(FFMPEG), "-y",
         "-framerate", "24",
         "-i", str(FRAMES_OUT / "frame_%05d.png"),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-an", str(OUT)],
        timeout=600,
    )
    log(f"完成：{OUT}")


def main():
    if not VIDEO.exists():
        log(f"找不到原片：{VIDEO}")
        sys.exit(1)
    count = extract_frames()
    upscale_frames(count)
    merge_frames()


if __name__ == "__main__":
    main()
