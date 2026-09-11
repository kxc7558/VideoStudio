# -*- coding: utf-8 -*-
"""康波演讲片 · Ref2VA 全流程产线（夜间无人值守版）。

12 镜 × Ref2VA（124帧≈5.17s，含原生音频，音画同步）→ 拼接 → 烧字幕。
幂等断点续跑；每镜约 25 分钟，12 镜 ≈ 5 小时。
控制：output/_control.txt = RUN / PAUSE / STOP（沿用批量产线约定）。

用法：cd /d/VideoStudio && venv/Scripts/python.exe _kangbo_ref2va_pipeline.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"d:\VideoStudio")
sys.stdout.reconfigure(encoding="utf-8")
import httpx

import comfy
from shared import ffmpeg_tools
from shared.paths import FFMPEG, OUTPUT

SHOTS = json.loads((OUTPUT / "_kangbo_shots.json").read_text(encoding="utf-8"))
CONTROL = OUTPUT / "_control.txt"
SEGS_DIR = OUTPUT / "_kangbo_ref2va_segs"
FINAL = OUTPUT / "kangbo_final.mp4"
SEED_BASE = 20260912
LENGTH = 124          # 124帧@24fps ≈ 5.17s/镜
STEPS = 20
W, H = 640, 832
SUBS = OUTPUT / "_kangbo subtitles.srt"
SUBTITLE_FONT = "Microsoft YaHei"

PROMPT_TMPL = (
    "Live-action, cinematic. {action} "
    "The economist from <Picture 1> stays exactly consistent with <Picture 1> throughout — "
    "same face, same round glasses, same black suit, same grey-streaked hair. "
    "overall_soundscape: {soundscape} "
    "non_diegetic_music: {music}. "
    "The camera holds nearly still or moves with small amplitude at slow speed. Sharp focus, fine detail."
)

# 每镜动作 + 音景 + 配乐（金句字幕在 stage_final 烧录）
SHOT_ACTIONS = [
    ("stands at a wooden podium on a dim conference stage, a giant glowing blue stock-wave chart on the big screen behind him, he points at the chart while speaking",
     "quiet room tone, his clear steady male voice, the soft hum of the projection screen", "N/A"),
    ("on a skyscraper rooftop at night overlooking a glittering financial skyline, wind moving his coat as he gazes over the city",
     "city night ambience, distant traffic hum, soft wind, his calm narration", "soft piano, sparse notes"),
    ("at a vast open-pit coal mine at golden hour wearing a white safety helmet over his suit, excavators working behind",
     "heavy machinery rumble, dust in the air, his voice carrying over the noise", "N/A"),
    ("in a surreal dark desert before three glowing doors under a starry sky, reaching toward the brightest door",
     "deep silence, faint wind, his measured voice echoing slightly", "low ambient drone"),
    ("beside a giant ancient mechanical clock with four season quadrants, warm steampunk light on his face",
     "mechanical gears ticking, clockwork whirring, his voice steady", "N/A"),
    ("on a frozen dark shore before a colossal wave frozen mid-crash under storm clouds, solemn expression",
     "icy wind, distant cracking ice, his solemn voice", "low strings, tense"),
    ("at the edge of a deep foggy canyon, a thin glowing red line plunging into the mist below, he looks down",
     "deep canyon wind, echoing silence, his warning voice", "N/A"),
    ("seated at a split desk with golden coins rising like stairs on one side and cold paperwork on the other, comparing both",
     "quiet office room tone, coins softly clinking, paper shifting, his even voice", "N/A"),
    ("on a balcony overlooking dense residential towers at dusk, hundreds of lit windows like a grid of stars",
     "dusk city ambience, distant traffic, his reflective voice", "gentle guitar"),
    ("holding a golden lifebuoy while standing on calm dark water, moonlight path on the ripples, calm determined expression",
     "calm water lapping, night crickets, his reassuring voice", "warm ambient pad"),
    ("walking through a vast warehouse with towering wooden crates, a shaft of light crossing the dusty floor",
     "echoing footsteps on concrete, his explanatory voice", "N/A"),
    ("walking along a glowing spiral galaxy path on dark sand dunes under swirling stars, footprints trailing behind",
     "vast desert night wind, awe-filled silence, his closing words landing slowly", "soft uplifting orchestral swell"),
]


def control() -> str:
    try:
        return CONTROL.read_text(encoding="utf-8").strip() or "RUN"
    except Exception:
        return "RUN"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_ref2va_workflow(prompt: str, seed: int) -> dict:
    wf = json.loads((Path(r"d:\VideoStudio") / "workflows" / "h3_ref2va_api.json").read_text(encoding="utf-8"))
    wf["5"]["inputs"]["image"] = REF_NAME
    wf["6"]["inputs"]["prompt"] = prompt
    wf["7"]["inputs"]["noise_seed"] = seed
    wf["6"]["inputs"]["length"] = LENGTH
    wf["15"]["inputs"]["filename_prefix"] = "kangbo_ref2va/seg"
    return wf


def stage_videos() -> bool:
    SEGS_DIR.mkdir(exist_ok=True)
    # 上传三视图参考图
    src = Path(r"D:\ComfyUI_Wan\output\kangbo\three_view_v2_00001_.png")
    up = httpx.post(
        "http://127.0.0.1:8188/upload/image",
        files={"image": (src.name, src.read_bytes(), "image/png")},
        data={"overwrite": "true", "type": "input"},
        timeout=60,
    )
    up.raise_for_status()
    global REF_NAME
    REF_NAME = up.json()["name"]
    log(f"参考图已上传: {REF_NAME}")

    pending = [s for s in SHOTS if not (SEGS_DIR / f"seg_{s['id']:02d}.mp4").exists()]
    if not pending:
        log("全部段已存在，跳过生成")
        return True
    for s, (action, soundscape, music) in zip(pending, [SHOT_ACTIONS[s['id'] - 1] for s in pending]):
        while True:
            if control() == "STOP":
                log("控制位 STOP，产线退出（已完成段保留）")
                return False
            if control() != "PAUSE":
                break
            log("暂停中（PAUSE）…")
            time.sleep(60)
        prompt = PROMPT_TMPL.format(action=action, soundscape=soundscape, music=music)
        wf = build_ref2va_workflow(prompt, SEED_BASE + s["id"])
        pid = comfy.submit(wf)
        log(f"段 {s['id']:02d} 提交（Ref2VA 124帧 约25分钟）…")
        ok, history = comfy.wait_done(pid, timeout=5400)
        if not ok:
            log(f"段 {s['id']:02d} 失败，跳过（可重跑补齐）")
            continue
        v = comfy.find_video(history)
        if v:
            comfy.download_video(v, SEGS_DIR / f"seg_{s['id']:02d}.mp4")
            log(f"段 {s['id']:02d} 完成")
    return all((SEGS_DIR / f"seg_{s['id']:02d}.mp4").exists() for s in SHOTS)


def stage_final() -> bool:
    segs = [SEGS_DIR / f"seg_{s['id']:02d}.mp4" for s in SHOTS]
    missing = [s["id"] for s, p in zip(SHOTS, segs) if not p.exists()]
    if missing:
        log(f"段缺失：{missing}，先补齐再拼片")
        return False
    # 生成 SRT：每镜 5.17s，顺序金句
    lines = []
    seg_dur = LENGTH / 24.0
    for i, s in enumerate(SHOTS):
        start = i * seg_dur
        end = start + seg_dur - 0.15
        lines.append(f"{i + 1}\n{_ts(start)} --> {_ts(end)}\n{s['quote']}\n")
    SUBS.write_text("\n".join(lines), encoding="utf-8")
    # 无损拼接（同参数段，concat demuxer 不重编码；段含音轨一并接）
    list_file = OUTPUT / "_kangbo_concat.txt"
    list_file.write_text("\n".join(f"file '{Path(p).as_posix()}'" for p in segs), encoding="utf-8")
    concat = OUTPUT / "kangbo_concat.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(concat)],
        capture_output=True, timeout=1800,
    )
    if not concat.exists():
        log("concat 失败，回退 hard_concat 重编码")
        ffmpeg_tools.concat_videos(segs, concat)
    # 烧字幕（subtitles filter 对 Windows 路径转义敏感，走 filter_complex_script + cwd 规避）
    log("烧录字幕…")
    vf_script = OUTPUT / "_vf.txt"
    vf_script.write_text(
        f"subtitles=filename={SUBS.name}:force_style='FontName={SUBTITLE_FONT},FontSize=9,Outline=1,MarginV=22'",
        encoding="utf-8",
    )
    subprocess.run(
        [FFMPEG, "-y", "-i", str(concat), "-filter_complex_script", str(vf_script),
         "-c:a", "copy", str(FINAL)],
        capture_output=True, timeout=1800, cwd=str(OUTPUT),
    )
    if FINAL.exists():
        log(f"成片完成 -> {FINAL}")
    else:
        log(f"字幕烧录失败，无字幕版在 {concat}")
    return FINAL.exists()


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def main() -> None:
    if not comfy.is_ready():
        log("引擎未启动，退出")
        return
    ok = stage_videos()
    if ok:
        stage_final()


if __name__ == "__main__":
    main()
