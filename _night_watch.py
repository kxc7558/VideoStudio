# -*- coding: utf-8 -*-
"""夜间出片监听器：轮询成片段落，把进度与动态度写进状态文件。

用户睡觉期间无人值守，所以：
- 不阻塞会话，独立进程跑，状态落 output/_night_watch.json
- 动态度只用**数值指标**（帧间差），不把无审查画面交给视觉模型判读
- 全 12 段齐了就退出（退出即触发通知），或到 MAX_HOURS 上限

用法：python _night_watch.py [期望段数=12]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from _motion_verify import motion_score

WORK = Path("output/_director_juqing")
STATE = Path("output/_night_watch.json")
LOG = Path("output/_night_run.log")
EXPECT = int(sys.argv[1]) if len(sys.argv) > 1 else 12
POLL_S = 60
MAX_HOURS = 14


def write_state(**kw) -> None:
    cur = {}
    if STATE.exists():
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update(kw)
    cur["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def log_tail(n: int = 6) -> list:
    try:
        return LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def main() -> None:
    t0 = time.time()
    scored: dict = {}
    write_state(expected=EXPECT, segs=[], note="监听启动")

    while (time.time() - t0) < MAX_HOURS * 3600:
        segs = sorted(WORK.glob("seg_*.mp4"))
        done = [s.name for s in segs]

        # 新出现的段落算一次动态度（只算一次，避免反复解码）
        for s in segs:
            key = s.name
            if key in scored:
                continue
            try:
                if s.stat().st_size > 50_000 and time.time() - s.stat().st_mtime > 30:
                    scored[key] = round(motion_score(s), 2)
            except Exception:
                pass

        write_state(
            segs=done, n=len(done), expected=EXPECT,
            motion=scored,
            elapsed_min=round((time.time() - t0) / 60, 1),
            log=log_tail(),
        )
        print(f"[{time.strftime('%H:%M:%S')}] {len(done)}/{EXPECT} 段 | 帧间差 {scored}", flush=True)

        if len(done) >= EXPECT:
            write_state(note=f"全部 {EXPECT} 段完成")
            print("=== 全部完成", flush=True)
            return
        time.sleep(POLL_S)

    write_state(note=f"到达 {MAX_HOURS} 小时上限，已出 {len(done)} 段")
    print(f"=== 超时退出，已出 {len(done)} 段", flush=True)


if __name__ == "__main__":
    main()
