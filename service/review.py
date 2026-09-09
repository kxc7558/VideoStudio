# -*- coding: utf-8 -*-
"""审查业务（业务层）：分镜意见改写/重抽/换锚/检查修复的编排。"""
import json
import random
import threading
from pathlib import Path

import ai
import comfy
import storyboard
from db import tasks_store
from shared import ffmpeg_tools
from shared.paths import OUTPUT
from service.generation import _generate_single, _finish_all_shots
from service.oneclick import _oc_stage2
from service.workflows import _build_workflow
def _rebuild_and_concat(task_id, width, height, length, steps, seed, style, shots, card_seg_id):
    """用当前 shots 重抽标记的镜（换 seed）→ 全部段重拼成片。供重抽/检查修复复用。"""
    resampled = tasks.get(task_id, {}).get("resampled", [])
    segs = []
    for i, s in enumerate(shots):
        p = str(s.get("prompt", "")).strip()
        prev_seg = OUTPUT / f"{task_id}_s{i}.mp4"
        if i in resampled or not prev_seg.exists():
            if i == 0 and card_seg_id and (OUTPUT / f"{card_seg_id}_anchor.png").exists():
                segs.append({"mode": "i2v", "image": comfy.upload_image(OUTPUT / f"{card_seg_id}_anchor.png"), "prompt": p})
            else:
                segs.append({"mode": "i2v" if i > 0 else "t2v", "image": None, "prompt": p})
        else:
            segs.append({"mode": "keep", "image": None, "prompt": p, "keep_video": prev_seg.name})
    _run_long_task(task_id, "wan", segs, width, height, length, steps, seed,
                   bridge=False, nsfw=True, style=style, review_each=True, keep_existing=True)
