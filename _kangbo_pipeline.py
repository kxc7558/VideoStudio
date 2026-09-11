# -*- coding: utf-8 -*-
"""康波演讲片产线：定妆照 → Qwen-Image-Edit 批量出 12 关键帧 → Wan i2v 批量出 12 段 → 拼接。

幂等：每步产物存在即跳过（断点续跑）。控制：_control.txt（RUN/PAUSE/STOP，与批量产线同约定）。
用法：python _kangbo_pipeline.py [frames|videos|all]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"d:\VideoStudio")
sys.stdout.reconfigure(encoding="utf-8")
import comfy
from shared import ffmpeg_tools
from shared.paths import FFMPEG, OUTPUT

SHOTS = json.loads((OUTPUT / "_kangbo_shots.json").read_text(encoding="utf-8"))
CONTROL = OUTPUT / "_control.txt"
FRAMES_DIR = OUTPUT / "_kangbo_frames"
SEGS_DIR = OUTPUT / "_kangbo_segs"
ANCHOR_LOCAL = Path(r"D:\VideoStudio\output\_kangbo_frames\anchor_wide.png")
SEED_BASE = 20260000

# 关键帧输出尺寸：16:9 横屏（Qwen-Image-Edit 以参考图尺寸为准，先放 1024x576 档）
W, H = 1024, 576
VIDEO_STEPS = 20         # H3 euler+beta 20 步（Wan i2v 蒸馏 4 步会人物扭曲，用户叫停）
VIDEO_LENGTH = 56        # H3 17k+5 网格：56帧 ≈ 2.33 秒/镜 @24fps


def _video_engine() -> str:
    """视频段引擎选择：h3（默认，保人强）"""
    return "h3"
FINAL = OUTPUT / "kangbo_final.mp4"


def control() -> str:
    try:
        return CONTROL.read_text(encoding="utf-8").strip() or "RUN"
    except Exception:
        return "RUN"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- 阶段 1：关键帧（Qwen-Image-Edit） ----------

def build_edit_workflow(edit_prompt: str, neg: str, seed: int) -> dict:
    wf = json.loads((Path(r"d:\VideoStudio") / "workflows" / "qwen_edit_api.json").read_text(encoding="utf-8"))
    wf["5"]["inputs"]["image"] = "kangbo_anchor.png"   # 已 upload 到引擎 input
    wf["6"]["inputs"]["prompt"] = edit_prompt
    wf["7"]["inputs"]["text"] = neg
    wf["10"]["inputs"].update({"seed": seed, "steps": 20, "cfg": 2.5})
    wf["12"]["inputs"]["filename_prefix"] = "kangbo_frames/frame"
    return wf


def stage_frames() -> bool:
    FRAMES_DIR.mkdir(exist_ok=True)
    # 定妆照上传引擎（幂等）
    up = comfy.upload_image(ANCHOR_LOCAL)
    # 引擎侧名字会带子目录前缀，qwen_edit 工作流里用上传返回名
    log(f"定妆照已上传: {up}")
    global _ANCHOR_NAME
    _ANCHOR_NAME = up

    pending = []
    for s in SHOTS:
        f = FRAMES_DIR / f"frame_{s['id']:02d}.png"
        if f.exists():
            log(f"帧 {s['id']:02d} 已存在，跳过")
            continue
        pending.append(s)
    if not pending:
        return True
    if not comfy.is_ready():
        log("引擎未启动")
        return False
    for s in pending:
        if control() == "STOP":
            log("控制位 STOP，暂停产线")
            return False
        while control() == "PAUSE":
            log("暂停中（PAUSE）…")
            time.sleep(30)
        for attempt in range(2):
            wf = build_edit_workflow(s["edit_prompt"], s["neg"], SEED_BASE + s["id"] * 10 + attempt)
            wf["5"]["inputs"]["image"] = _ANCHOR_NAME
            pid = comfy.submit(wf)
            log(f"帧 {s['id']:02d} 生成中（attempt {attempt + 1}）…")
            ok, history = comfy.wait_done(pid, timeout=1800)
            if not ok:
                log(f"帧 {s['id']:02d} 失败，重试或跳过")
                continue
            info = None
            for _n, o in history.get("outputs", {}).items():
                for _k, items in o.items():
                    if isinstance(items, list):
                        for it in items:
                            if isinstance(it, dict) and str(it.get("filename", "")).endswith(".png"):
                                info = it
            if info:
                dest = FRAMES_DIR / f"frame_{s['id']:02d}.png"
                comfy.download_binary(info, dest)
                log(f"帧 {s['id']:02d} 完成 -> {dest.name}")
                break
    return all((FRAMES_DIR / f"frame_{s['id']:02d}.png").exists() for s in SHOTS)


# ---------- 阶段 2：视频段（H3 i2v，euler+beta 20 步——实测保人强，Wan 蒸馏扭曲已弃） ----------

def stage_videos() -> bool:
    SEGS_DIR.mkdir(exist_ok=True)
    from service.workflows import _build_h3_workflow
    motion = ("The economist character performs the scene naturally: subtle head turns, natural blinking, "
              "gentle hand gestures matching the scene, camera moves slowly with small amplitude, "
              "steady cinematic lighting, sharp focus, fine detail")
    pending = [s for s in SHOTS if not (SEGS_DIR / f"seg_{s['id']:02d}.mp4").exists()]
    if not pending:
        return True
    if not comfy.is_ready():
        log("引擎未启动")
        return False
    for s in pending:
        if control() == "STOP":
            log("控制位 STOP，暂停产线")
            return False
        while control() == "PAUSE":
            log("暂停中（PAUSE）…")
            time.sleep(30)
        frame = FRAMES_DIR / f"frame_{s['id']:02d}.png"
        if not frame.exists():
            log(f"帧 {s['id']:02d} 缺失，跳过该镜")
            continue
        img_name = comfy.upload_image(frame)
        # H3 i2v：竖版关键帧按 640x832 出片（H3 24fps，56帧 ≈ 2.33s）
        wf = _build_h3_workflow("i2v", img_name, motion, SEED_BASE + 500 + s["id"], 640, 832, VIDEO_LENGTH, f"kangbo_v{s['id']:02d}", VIDEO_STEPS)
        pid = comfy.submit(wf)
        log(f"段 {s['id']:02d} 视频生成中（H3 约 4-6 分钟）…")
        ok, history = comfy.wait_done(pid, timeout=2400)
        if not ok:
            log(f"段 {s['id']:02d} 失败")
            continue
        v = comfy.find_video(history)
        if v:
            comfy.download_video(v, SEGS_DIR / f"seg_{s['id']:02d}.mp4")
            log(f"段 {s['id']:02d} 完成")
    return all((SEGS_DIR / f"seg_{s['id']:02d}.mp4").exists() for s in SHOTS)


# ---------- 阶段 3：拼接 + 烧字幕 ----------

def stage_final() -> bool:
    segs = [SEGS_DIR / f"seg_{s['id']:02d}.mp4" for s in SHOTS]
    missing = [i for i, p in zip([s["id"] for s in SHOTS], segs) if not p.exists()]
    if missing:
        log(f"段缺失：{missing}")
        return False
    # 每镜 ~5s，金句字幕按 5s/镜排列
    subs = OUTPUT / "_kangbo_subs.srt"
    idx = 1
    lines = []
    for s in SHOTS:
        start = (idx - 1) * 5
        end = idx * 5 - 0.2
        lines.append(f"{idx}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{s['quote']}\n")
        idx += 1
    subs.write_text("\n".join(lines), encoding="utf-8")

    # 先无声拼接
    concat_target = OUTPUT / "kangbo_concat.mp4"
    ffmpeg_tools.concat_videos(segs, concat_target)
    # 再烧字幕（软烧录，libass，中文用系统字体）
    log("烧录字幕…")
    subprocess.run(
        [FFMPEG, "-y", "-i", str(concat_target), "-vf",
         f"subtitles={subs}:force_style='FontName=Microsoft YaHei,FontSize=10,Outline=1,MarginV=25'",
         "-c:a", "copy", str(FINAL)],
        capture_output=True, timeout=1800,
    )
    if FINAL.exists():
        log(f"成片完成 -> {FINAL}")
        concat_target.unlink(missing_ok=True)
        return True
    log(f"字幕烧录失败，保留无字幕版 {concat_target}")
    return False


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("frames", "all"):
        ok = stage_frames()
        if not ok and stage == "all":
            log("关键帧阶段未完成，停止")
            return
    if stage in ("videos", "all"):
        if not stage_videos():
            log("视频段阶段未完成，停止")
            return
    if stage in ("final", "all"):
        stage_final()


if __name__ == "__main__":
    main()
