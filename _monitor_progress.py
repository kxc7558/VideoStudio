# -*- coding: utf-8 -*-
"""视频批量生成进度监控：周期打印"第几段 / 采样步骤 / 完成数 / 预计剩余时间"。

依赖 ComfyUI 的 /history 与 /queue，以及采样日志里的进度条。
用法：python _monitor_progress.py [间隔秒]，默认 60 秒打印一次。
"""
import re
import sys
import time
from pathlib import Path

import httpx

# 强制 UTF-8 输出，避免 Windows 控制台 GBK 乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

COMFY = "http://127.0.0.1:8188"
OUTPUT_DIR = Path(r"D:\VideoStudio\output")
LOG = Path(r"C:/Users/32492/AppData/Local/Temp/comfyui_regenerate.log")
SEED_BASE = 999000000


def seg_jobs():
    """根据已下载的 highres_segNN.mp4 判断完成到哪一段。"""
    done = []
    for i in range(1, 11):
        if (OUTPUT_DIR / f"highres_seg{i:02d}.mp4").exists():
            done.append(i)
    return set(done)


def gpu_load():
    """读取采样日志里最近一条进度条，返回相对当前分段 0~1 的采样进度与 s/it。"""
    try:
        log = LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)
    frames = [f for f in log.split("\r") if "it]" in f]
    if not frames:
        return None
    last = frames[-1]
    m = re.search(r"(\d+)%\|+.+\|\s+(\d+)/(\d+)\s*\[\d+:(\d+)<.*?(\d+(?:\.\d+)?)s/it", last)
    if not m:
        m = re.search(r"(\d+)/(\d+)\s*\[\d+:(\d+)<.*?(\d+(?:\.\d+)?)s/it", last)
        if m:
            return {"v": int(m.group(1)), "total": int(m.group(2)),
                    "s_it": float(m.group(4)), "frac": float(m.group(1)) / float(m.group(2))}
    return int(m.group(1)) / 100.0  # percent -> frac


def queue_state():
    try:
        q = httpx.get(COMFY + "/queue", timeout=5).json()
        return len(q.get("queue_running", [])), len(q.get("queue_pending", []))
    except Exception:
        return None, None


def print_status():
    done = seg_jobs()
    n_done = len(done)
    running, pending = queue_state()

    # 当前段 / 剩余
    all_done = n_done >= 10
    print(f"\n[视频批量] 已完成 {n_done}/10 段" + ("  ✅ 全部完成!" if all_done else ""))
    if not all_done:
        # 找当前在跑的小段：done 里没有的最小序号（从分段2起；分段1已完成）
        cur = next((i for i in range(1, 11) if i not in done), None)
        print(f"  当前测试段：分段 {cur}  {'(队列运行中)' if running else '(等待队列)'}")
    else:
        return

    frac = gpu_load()
    if frac is not None:
        if isinstance(frac, dict):
            pct = frac["frac"] * 100
            print(f"  采样进度：{pct:.0f}%  约 {frac['s_it']:.0f} 秒/步")
        else:
            print(f"  采样进度：{frac*100:.0f}%")
    else:
        print("  采样进度：正在加载模型/等待…")

    # 剩余时间粗估：每段约 55 分钟；当前段按 (1 - 当前采样进度) 剩余
    per_seg_min = 55
    if isinstance(frac, dict) and frac["s_it"] and frac["v"]:
        rem_cur_min = (frac["total"] - frac["v"]) * frac["s_it"] / 60
    else:
        rem_cur_min = per_seg_min
    rem_total_min = rem_cur_min + (10 - n_done - 1) * per_seg_min
    print(f"  预计还需：约 {rem_total_min:.0f} 分钟（{rem_total_min/60:.1f} 小时）")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    once = "--once" in sys.argv
    interval = int(args[0]) if args else 60
    if once:
        print_status()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[-1].startswith("--for"):
        total = int(sys.argv[-1].split("=")[1]) if "=" in sys.argv[-1] else 3600 * 4
        end = time.time() + total
    else:
        end = None
    print(f"开始监控（每 {interval} 秒打印一次）…")
    while True:
        print_status()
        time.sleep(interval)
        if end and time.time() > end:
            print("\n监控时间到，结束。")
            break