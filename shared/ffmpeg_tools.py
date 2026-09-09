# -*- coding: utf-8 -*-
"""ffmpeg 工具（公共层）：时长读取、拼接、抽帧。零业务规则。"""
import re
import subprocess

from shared.paths import FFMPEG


def video_duration(path) -> float:
    """用 ffmpeg 读视频时长（秒）；失败返回 0。"""
    try:
        r = subprocess.run(
            [FFMPEG, "-i", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or r.stdout or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def hard_concat(paths: list, out) -> object:
    """硬切拼接（兜底，重新编码保证参数一致）。"""
    list_file = Path(out).with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{Path(p).as_posix()}'" for p in paths), encoding="utf-8"
    )
    try:
        subprocess.run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)],
            capture_output=True, timeout=1800,
        )
    finally:
        list_file.unlink(missing_ok=True)
    return out


def concat_videos(paths: list, out, transition: float = 0.9) -> object:
    """按顺序把多个 mp4 用「交叉溶解」拼成一条。

    任一视频时长读不出来或太短时，退回硬切拼接兜底，保证一定出片。
    """
    from pathlib import Path as _P
    if len(paths) < 2:
        subprocess.run(
            [FFMPEG, "-y", "-i", str(paths[0]),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)],
            capture_output=True, timeout=1800,
        )
        return out
    durs = [video_duration(p) for p in paths]
    if any(d <= transition for d in durs):
        return hard_concat(paths, out)
    cmd = [FFMPEG, "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    parts = []
    acc = durs[0]
    prev_label = "[0:v]"
    for i in range(1, len(paths)):
        offset = acc - transition
        label = f"[v{i}]"
        parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}{label}"
        )
        prev_label = label
        acc = acc + durs[i] - transition
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", prev_label,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out),
    ]
    subprocess.run(cmd, capture_output=True, timeout=3600)
    return out


def extract_last_frame(video_path, out_png) -> object:
    """用 ffmpeg 抽视频最后一帧存成 png，供下一段当首帧（首尾帧接续）。"""
    subprocess.run(
        [FFMPEG, "-y", "-sseof", "-0.2", "-i", str(video_path),
         "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=300,
    )
    return out_png
