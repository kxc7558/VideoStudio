# -*- coding: utf-8 -*-
"""分段 2-10 高清重跑 v2 —— 可控版本（供桌宠控制 暂停/继续/结束）。

和旧的差别：
 1. 幂等续跑：启动时吸收已产出的段（OUTPUT_DIR 与 ComfyUI output/video 都认），
    从空缺段继续，绝不重复提交。
 2. 控制文件：每段提交前轮询 OUTPUT_DIR/_control.txt:
       空 / RUN  -> 正常递交
       PAUSE     -> 等当前段完成后不再递交，原地等待「继续」（保住已算完的段）
       STOP      -> 立即退出，不再递交后续
   桌宠负责写入该文件；「结束」还可额外对 ComfyUI 发 /interrupt 中断当前段。
 3. 每段完成后下载到 OUTPUT_DIR/highres_segNN.mp4，然后下一段。
 4. 全部完成后 ffmpeg 拼接成 final_video_highres.mp4。

用法：
   python -u _run_batch_seg2to10.py 2>&1 >> batch.log &
控制文件（桌宠写）：
   "RUN" 正常 ；"PAUSE" 暂停；"STOP" 结束。
"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import comfy

WORKFLOWS = Path(r"D:\VideoStudio\workflows")
OUTPUT_DIR = Path(r"D:\VideoStudio\output")
COMFY_OUT = Path(r"D:\ComfyUI_Wan\output")
COMFY_VID = COMFY_OUT / "video"
STORY_FILE = OUTPUT_DIR / "1e9d3c87cc3b.json"
CONTROL = OUTPUT_DIR / "_control.txt"
FFMPEG = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"

WIDTH, HEIGHT, LENGTH, STEPS = 1024, 576, 81, 20
SEED_BASE = 999000000
TOTAL = 10


def load_story():
    story = json.loads(STORY_FILE.read_text(encoding="utf-8"))
    segs = [{"index": a["segment"], "en": a["rewritten"]}
            for a in story.get("ai_prompts", [])]
    segs.sort(key=lambda s: s["index"])
    return segs


def set_control(state: str):
    """写入控制文件的当前状态（供桌宠/日志判断）。"""
    CONTROL.write_text(state, encoding="utf-8")


def read_control() -> str:
    try:
        return CONTROL.read_text(encoding="utf-8").strip().upper()
    except Exception:
        return ""


def polish(clear: bool):
    """清掉控制文件标记。桌宠会写 RUN 恢复。"""
    set_control("")


def build_and_submit(seg):
    wf = json.loads((WORKFLOWS / "t2v_api.json").read_text(encoding="utf-8"))
    seed = SEED_BASE + seg["index"]
    half = STEPS // 2
    wf["2"]["inputs"]["text"] = seg["en"]
    wf["9"]["inputs"].update({"width": WIDTH, "height": HEIGHT, "length": LENGTH})
    wf["10"]["inputs"].update({"noise_seed": seed, "steps": STEPS,
                               "start_at_step": 0, "end_at_step": half})
    wf["11"]["inputs"].update({"noise_seed": seed, "steps": STEPS,
                               "start_at_step": half, "end_at_step": STEPS})
    wf["14"]["inputs"]["filename_prefix"] = f"video/highres_seg{seg['index']:02d}"
    return comfy.submit(wf)


def queue_nonempty() -> bool:
    try:
        import httpx
        q = httpx.get("http://127.0.0.1:8188/queue", timeout=5).json()
        return bool(q.get("queue_running") or q.get("queue_pending"))
    except Exception:
        return False


def absorb_products():
    """把 ComfyUI output/video 已有的 highres_segNN_*.mp4 认作产出并下载到 OUTPUT_DIR。
    返回已完成的段号集合。"""
    done = set()
    if OUTPUT_DIR.exists():
        for i in range(1, TOTAL + 1):
            if (OUTPUT_DIR / f"highres_seg{i:02d}.mp4").exists():
                done.add(i)
    if COMFY_VID.exists():
        for i in range(1, TOTAL + 1):
            if i in done:
                continue
            cands = list(COMFY_VID.glob(f"highres_seg{i:02d}_*.mp4"))
            if cands:
                shutil.copy2(cands[-1], OUTPUT_DIR / f"highres_seg{i:02d}.mp4")
                done.add(i)
                print(f"  吸收 ComfyUI 产物 → highres_seg{i:02d}.mp4")
    return done


def wait_queue_empty(interval=15, maxmin=45):
    """等 ComfyUI 队列清空（让正在跑的一段自然落盘），返回是否在限时内清空。"""
    waited = 0
    while queue_nonempty():
        time.sleep(interval)
        waited += interval
        if waited > maxmin * 60:
            return False
    time.sleep(5)  # 落盘缓冲
    return True


def fetch_seg(seg):
    """递交一段并等到完成、下载，返回 True/False。"""
    i = seg["index"]
    pid = build_and_submit(seg)
    print(f"[{i}/10] 已递交，任务 {pid}", flush=True)

    # 轮询 history，期间也响应控制 STOP（iscancel 中断）
    ok, entry = comfy.wait_done(pid, timeout=7200, poll=30,
                                should_cancel=lambda: read_control() == "STOP")
    if not ok:
        # 可能是被 STOP 中断（用户结束）
        return False
    v = comfy.find_video(entry)
    if not v:
        print(f"[{i}/10] 完成但无视频输出", flush=True)
        return False
    comfy.download_video(v, OUTPUT_DIR / f"highres_seg{i:02d}.mp4")
    # 把本段从 ComfyUI output 里也清理（避免下次吸收误判重复，可选）
    print(f"[{i}/10] 完成 → highres_seg{i:02d}.mp4", flush=True)
    return True


def wait_if_paused():
    """段间控制检查：STOP 返回 False（退出）；PAUSE 则原地等待 RUN。"""
    while True:
        c = read_control()
        if c == "STOP":
            return False
        if c == "PAUSE":
            print("  ⏸ 已暂停（等桌宠点「继续」）…", flush=True)
            time.sleep(3)
            continue
        return True


def concat():
    paths = [OUTPUT_DIR / f"highres_seg{i:02d}.mp4" for i in range(1, TOTAL + 1)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        print(f"还缺 {len(missing)} 段，暂不拼接：{missing}", flush=True)
        return False
    lst = OUTPUT_DIR / "_concat_highres.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in paths), encoding="utf-8")
    out = OUTPUT_DIR / "final_video_highres.mp4"
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", str(out)]
    print("拼接中…", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode == 0 and out.exists():
        print(f"拼接完成：{out} ({out.stat().st_size//1024//1024} MB)", flush=True)
        return True
    print("ffmpeg 失败：", (r.stderr or "")[-400:], flush=True)
    return False


def main():
    if not comfy.is_ready():
        print("ComfyUI 未运行。", flush=True)
        return
    CONTROL.parent.mkdir(parents=True, exist_ok=True)
    set_control("")

    print("启动：吸收已产出段…", flush=True)
    done = absorb_products()
    print(f"已完成 {len(done)} 段：{sorted(done)}", flush=True)

    # 若队列里还有段在跑（被旧脚本递交的 seg2），先等它落盘再扫描吸收
    if queue_nonempty():
        print("检测到队列在跑（旧脚本遗留），等待其自然完成并吸收…", flush=True)
        wait_queue_empty()
        done = absorb_products()

    segs = load_story()
    todo = [s for s in segs if s["index"] not in done]
    if not todo:
        print("所有段都已完成，直接拼接。", flush=True)
        print("COMPLETE_ALL" if concat() else "CONCAT_FAILED")
        return

    print(f"待跑：{sorted(s['index'] for s in todo)}（每个约55分钟）", flush=True)
    for seg in todo:
        if not wait_if_paused():
            print("收到 STOP，已停止。", flush=True)
            break
        i = seg["index"]
        print(f"\n=== 递交分段 {i}/10 ===", flush=True)
        done_before = i
        if not fetch_seg(seg):
            print(f"分段 {i} 未正常完成（可能被结束中断），停止。", flush=True)
            break
        time.sleep(2)

    print("\n=== 拼接检查 ===", flush=True)
    done = absorb_products()
    if len(done) >= TOTAL:
        ok = concat()
        print("COMPLETE_ALL" if ok else "CONCAT_FAILED", flush=True)
    else:
        print(f"未全部完成（{len(done)}/{TOTAL}），final 不拼接。", flush=True)
        print("INCOMPLETE", flush=True)


if __name__ == "__main__":
    main()