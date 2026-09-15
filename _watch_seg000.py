# -*- coding: utf-8 -*-
"""后台监听 seg_000 重生成结果，写状态文件供查询（不阻塞会话）。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"d:\VideoStudio")
sys.stdout.reconfigure(encoding="utf-8")
import httpx

import comfy
from shared.paths import OUTPUT

SEG = OUTPUT / "_kangbo2_segs" / "seg_000.mp4"
STATUS = OUTPUT / "_watch_status.json"
PIPELINE_LOG = OUTPUT / "_v2fix_probe5.log"
DEADLINE = time.time() + 3600  # 最多盯 1 小时


def write_status(state: dict) -> None:
    STATUS.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    pid_seen = None
    while time.time() < DEADLINE:
        try:
            log_text = PIPELINE_LOG.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            log_text = ""
        if SEG.exists():
            write_status({"state": "done", "seg": str(SEG), "mtime": SEG.stat().st_mtime, "at": time.time()})
            return
        if "镜 000 失败" in log_text or "三次失败" in log_text:
            # 失败也在日志里留痕，等下一轮 attempt
            write_status({"state": "failed_round", "log_tail": log_text[-300:], "at": time.time()})
        elif "提交" in log_text:
            write_status({"state": "rendering", "log_tail": log_text[-200:], "at": time.time()})
        time.sleep(60)
    write_status({"state": "timeout", "at": time.time()})


if __name__ == "__main__":
    main()
