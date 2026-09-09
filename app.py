# -*- coding: utf-8 -*-
"""出片台入口（兼容门面）：组装分层结构，并 re-export 旧符号保证既有脚本/测试零改动。

分层结构：
    api/routers.py   接口层（HTTP 路由）
    service/         业务层（workflows 生成规则 / generation 生成执行 / oneclick 一键成片 / review 审查）
    db/              数据层（tasks_store 任务档案 / profiles_store 制片资料）
    shared/          公共层（paths 路径 / ffmpeg_tools 工具 / comfy 引擎客户端 / ai 本地模型 / storyboard 拆镜）

调用铁律：api → service → db；shared 人人可用不依赖他人。
app.py 保留旧符号（_run_oneclick_task 等）作为兼容门面：管线脚本与测试不必改 import。
"""
import json
import random
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from shared.paths import BASE, WEB
import ai
import comfy
import storyboard
from db import tasks_store, profiles_store
from db.tasks_store import tasks, _lock  # 兼容旧引用（tasks dict 全局共享）
from shared import ffmpeg_tools
from service import workflows as _wf
from service import generation as _gen
from service import oneclick as _oc
from service import review as _rv
from api import routers

app = FastAPI(title="出片台")
app.include_router(routers.router)
app.mount("/web", StaticFiles(directory=WEB), name="web")

comfy.start_progress_listener()

tasks_store.reconcile_stale_tasks()

# ---- 常量与常量门面（旧代码引用名） ----
RESOLUTIONS = {
    "竖屏 9:16": (480, 832),
    "竖屏高清 9:16": (576, 1024),
    "方屏 1:1": (640, 640),
    "横屏 16:9": (832, 480),
}
DURATIONS = {"短 · 约2秒": 33, "中 · 约3秒": 49, "长 · 约5秒": 81}
DURATIONS_H3 = {"短 · 约2秒": 56, "中 · 约3秒": 73, "长 · 约5秒": 124}
MODELS = ("wan", "h3")
MAX_STEPS = 50
DEFAULT_PROMPT = {
    "i2v": "画面自然流畅地动起来，动作连贯，镜头缓慢推进，高画质，细节丰富",
    "t2v": "电影级画质，细节丰富，画面自然流畅，镜头缓慢推进",
}
NSFW_LORA = _wf.NSFW_LORA
ANIME_LORA = _wf.ANIME_LORA
ENHANCER_LORA = _wf.ENHANCER_LORA
MODEL_VARIANT = _wf.MODEL_VARIANT

# ---- 兼容门面：旧符号 → 新层 ----
_apply_wan_nsfw_lora = _wf._apply_wan_nsfw_lora
_build_wan_workflow = _wf._build_wan_workflow
_build_h3_workflow = _wf._build_h3_workflow
_build_workflow = _wf._build_workflow

_cancelled = tasks_store.cancelled
_meta_dict = tasks_store.meta_dict
_write_meta = tasks_store.write_meta
_update = tasks_store.update
_task_or_none = tasks_store.task_or_none
_reconcile_stale_tasks = tasks_store.reconcile_stale_tasks

_video_duration = ffmpeg_tools.video_duration
_hard_concat = ffmpeg_tools.hard_concat
concat_videos = ffmpeg_tools.concat_videos
extract_last_frame = ffmpeg_tools.extract_last_frame

_profiles = profiles_store.profiles
_clean_profiles = profiles_store.clean_profiles
_save_profiles = profiles_store.save_profiles


def _resolve_creative(scene_id: str, character_ids: str) -> dict:
    data = _profiles()
    scene = next((x for x in data["scenes"] if x["id"] == scene_id), None)
    wanted = {x for x in character_ids.split(",") if x}
    characters = [x for x in data["characters"] if x["id"] in wanted]
    return {"scene": scene, "characters": characters}


def _creative_brief(creative: dict) -> str:
    scene = creative.get("scene")
    characters = creative.get("characters", [])
    parts = []
    if scene:
        details = "；".join(f"{k}：{scene[k]}" for k in ("place", "era", "atmosphere", "lighting", "palette", "camera") if scene.get(k))
        if details:
            parts.append(f"场景圣经（全片保持一致）：{scene.get('name') or '未命名场景'}，{details}。")
    for character in characters:
        details = "；".join(f"{k}：{character[k]}" for k in ("identity", "appearance", "wardrobe", "behavior") if character.get(k))
        if details:
            parts.append(f"人物设定（全片保持身份、脸部特征、发型和服装一致）：{character.get('name') or '未命名角色'}，{details}。")
    return "\n".join(parts)


def _apply_creative(prompt: str, creative: dict, shot_note: str = "") -> str:
    brief = _creative_brief(creative)
    pieces = [p for p in (brief, shot_note.strip(), prompt.strip()) if p]
    return "\n".join(pieces)


# service 函数门面（管线脚本/测试引用）
_run_task = _gen._run_task
_run_long_task = _gen._run_long_task
_generate_single = _gen._generate_single
_finish_all_shots = _gen._finish_all_shots
_pick_best_character_card = _oc._pick_best_character_card
_run_oneclick_task = _oc._run_oneclick_task
_generate_shot_previews = _oc._generate_shot_previews
_oc_stage2 = _oc._oc_stage2
_rebuild_and_concat = _rv._rebuild_and_concat

# api 层路由函数经由 service 门面调用（set_gateway 注入解耦）
routers.set_gateway(
    _resolve_creative=_resolve_creative,
    _creative_brief=_creative_brief,
    _apply_creative=_apply_creative,
    _run_task=_run_task,
    _run_long_task=_run_long_task,
    _oc_stage2=_oc_stage2,
    _finish_all_shots=_finish_all_shots,
    _generate_shot_previews=_generate_shot_previews,
    _task_or_none=_task_or_none,
)
