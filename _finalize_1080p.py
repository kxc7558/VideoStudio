# -*- coding: utf-8 -*-
"""把 24 段交叉溶解拼接，并升采样到 1080p + 锐化 + 高清编码，产出最终成片。

画质优化要点：
  1. 一步到位：交叉溶解 → 升采样 1080p → 锐化 → 高清编码，只重编码一次（避免二次损失）；
  2. 升采样用 lanczos + 保持比例覆盖式放大（force_original_aspect_ratio=increase + 居中裁剪），
     不拉伸变形；
  3. 编码 CRF 18 + preset slow（近无损），锐化轻度（0.5）提边缘、不增噪点。
"""
import re
import subprocess
import sys
from pathlib import Path

TASK = "1e9d3c87cc3b"
OUTPUT = Path(r"D:\VideoStudio\output")
FFMPEG = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
TOTAL = 24
TRANSITION = 0.9
CRF = 18
W, H = 1920, 1080


def _duration(path: Path) -> float:
    try:
        r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or r.stdout or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def main():
    paths = [OUTPUT / f"{TASK}_s{i}.mp4" for i in range(TOTAL)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        print(f"还缺 {len(missing)} 段，先等生成完成：{missing}")
        sys.exit(1)

    durs = [_duration(p) for p in paths]
    if any(d <= TRANSITION for d in durs):
        print("有段时长太短，无法交叉溶解，中止")
        sys.exit(1)

    cmd = [FFMPEG, "-y"]
    for p in paths:
        cmd += ["-i", str(p)]

    parts = []
    acc = durs[0]
    prev_label = "[0:v]"
    for i in range(1, TOTAL):
        offset = acc - TRANSITION
        label = f"[v{i}]"
        parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:duration={TRANSITION:.3f}:offset={offset:.3f}{label}"
        )
        prev_label = label
        acc = acc + durs[i] - TRANSITION

    # 升采样 1080p（保持比例覆盖式放大 + 居中裁剪）+ 轻度锐化
    parts.append(
        f"{prev_label}scale={W}:{H}:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},unsharp=5:5:0.5:5:5:0.0[vout]"
    )

    out = OUTPUT / f"{TASK}_1080p.mp4"
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
        "-c:v", "libx264", "-crf", str(CRF), "-preset", "slow",
        "-pix_fmt", "yuv420p", "-an", str(out),
    ]

    print("开始拼接 + 1080p 优化（约 5~15 分钟）…")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        print("拼接失败，ffmpeg 报错：")
        print((r.stderr or "")[-2500:])
        sys.exit(1)
    print(f"完成：{out}")


if __name__ == "__main__":
    main()
