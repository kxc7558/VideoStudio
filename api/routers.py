# -*- coding: utf-8 -*-
"""接口层：全部 HTTP 路由。只做参数校验 + 调 service，不写业务规则。"""
import json
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import feedback_store

import ai
import comfy
import storyboard
from db import profiles_store, tasks_store
from db.tasks_store import tasks, _lock, update as _update, task_or_none as _task_or_none, meta_dict as _meta_dict
from shared import ffmpeg_tools
from shared.ffmpeg_tools import concat_videos, extract_last_frame
from shared.paths import BASE, JUBEN, OUTPUT, UPLOADS, WEB, FFMPEG
from service.workflows import _build_workflow, RESOLUTIONS, DURATIONS, DURATIONS_H3, MODELS, MAX_STEPS, DEFAULT_PROMPT, MODEL_VARIANT
from service.generation import _run_task, _run_long_task, _generate_single, _finish_all_shots
from service.oneclick import _oc_stage2, _generate_shot_previews, _pick_best_character_card, _run_oneclick_task
from service import character as _char
from service.review import _rebuild_and_concat

_profiles = profiles_store.profiles
_save_profiles = profiles_store.save_profiles

router = APIRouter()

# ---- 由门面注入的 service 函数（app.py 启动时 set_gateway 绑定，避免循环 import） ----
_G = {}


def set_gateway(**fns):
    _G.update(fns)

@router.get("/api/juben")
def list_juben():
    """列出 juben/ 目录下的剧本文件名（供一键成片选择；不返回内容）。"""
    d = BASE / "juben"
    if not d.exists():
        return {"files": []}
    return {"files": sorted(f.name for f in d.glob("*.txt") if f.is_file())}


@router.post("/api/oneclick")
async def oneclick(
    idea: str = Form(""),
    style: str = Form("anime"),
    resolution: str = Form("竖屏高清 9:16"),
    seed: int = Form(-1),
    script_file: str = Form(""),
    character_image: str = Form(""),
):
    """一键成片：一个输入框 + 一个按钮的极简入口，其余全自动。

    script_file：juben/ 目录下的剧本文件名（可选）。给了剧本就跳过 AI 编剧直接拆镜，
    剧本内容只在本地流转（读文件 → 本地模型），不打印、不落日志。
    character_image：角色定妆图文件名（可选，来源：直接上传的 /uploads 文件名，
    或角色库 output/_character_cards/ 下的文件名）。给了就跳过自动抽卡。
    """
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if not comfy.t2v_ready():
        return JSONResponse({"error": "文生视频模型还没就绪"}, 503)
    if not ai.uncensored_ready():
        return JSONResponse({"error": "本地编剧模型（Ollama qwen3.8）未就绪"}, 503)

    script_text = ""
    if script_file.strip():
        sf = (BASE / "juben" / Path(script_file.strip()).name)
        if not sf.exists():
            return JSONResponse({"error": f"juben/ 下没有 {script_file}"}, 400)
        script_text = sf.read_text(encoding="utf-8", errors="ignore")
    if not script_text and not idea.strip():
        return JSONResponse({"error": "请先写一句你的想法，或指定剧本文件"}, 400)
    if style not in ("anime", "real"):
        style = "real"
    # 角色图合法性预检：文件必须真实存在（在 uploads/ 或角色卡目录），否则报错而不是静默回退
    if character_image.strip():
        cname = Path(character_image.strip()).name
        if not (UPLOADS / cname).exists() and not (OUTPUT / "_character_cards" / cname).exists():
            return JSONResponse({"error": f"角色图 {cname} 不存在，请重新上传或从角色库选择"}, 400)

    width, height = RESOLUTIONS.get(resolution, (480, 832))
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)
    task_id = uuid.uuid4().hex[:12]
    _update(
        task_id, state="queued", msg="排队中…", mode="oneclick", model="wan",
        prompt=(script_file.strip() and f"剧本：{Path(script_file).name}") or idea[:80],
        seed=seed, resolution=resolution, duration="约1.5分钟",
        style=style, character_image=character_image.strip(),
        created=int(time.time()),
    )
    threading.Thread(
        target=_run_oneclick_task,
        args=(task_id, idea.strip(), style, width, height, 81, 20, seed, script_text, character_image.strip()),
        daemon=True,
    ).start()
    return {"task_id": task_id, "seed": seed}


# ---- 一键成片的人工审查端点（分镜意见改写 / 单镜重抽 / 换锚 / 成片检查修复 / 通过） ----


# ---- 角色卡：描述生成（文生图）→ 选定 → 存角色库 ----


@router.post("/api/character/generate")
def character_generate(
    desc: str = Form(...),
    style: str = Form("anime"),
):
    """角色描述 → 文生图出 4 张候选（NoobAI-XL）。异步提交，返回 prompt_id 供轮询。"""
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if not desc.strip():
        return JSONResponse({"error": "请先写一句角色描述"}, 400)
    wf = _char.build_card_workflow_from_desc(desc.strip(), style)
    pid = comfy.submit(wf)
    return {"prompt_id": pid, "batch": _char.CANDIDATES}


@router.get("/api/character/candidates/{prompt_id}")
def character_candidates(prompt_id: str):
    """轮询某次角色生成的结果：未完返回 running，完了返回候选图 URL 列表。"""
    ok, history = comfy.peek_done(prompt_id)
    if not ok:
        q = comfy.queue_position(prompt_id)
        return {"status": "running", "queue": q}
    images = _char.collect_images(history)
    if not images:
        return {"status": "failed", "images": []}
    saved = _char.store_candidates(prompt_id, images)
    return {"status": "done", "images": [f"/api/character/file/{Path(p).name}" for p in saved]}


@router.get("/api/character/file/{name}")
def character_file(name: str):
    """候选/定妆图静态服务（白名单目录，防路径穿越）。"""
    safe = Path(name).name
    for d in (OUTPUT / "_character_cards",):
        f = d / safe
        if f.exists():
            return FileResponse(f)
    return JSONResponse({"error": "文件不存在"}, 404)


@router.post("/api/character/save")
def character_save(
    image: str = Form(...),
    name: str = Form(""),
    desc: str = Form(""),
):
    """把选中的候选图（或上传的图）存进角色库，供后续一键成片选用。"""
    result = _char.save_character_image(image, name, desc)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, 400)
    return result


@router.post("/api/character/upload")
async def character_upload(image: UploadFile = File(...)):
    """上传角色定妆图 → 存角色库。与生成的角色同渠道供一键成片选用。"""
    raw = await image.read()
    ext = Path(image.filename or "x.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return JSONResponse({"error": "请上传 png / jpg / webp 图片"}, 400)
    result = _char.save_uploaded_character(raw, ext)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, 400)
    return result


@router.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "web" / "index.html").read_text(encoding="utf-8")


@router.get("/api/health")
def health():
    return {
        "comfy": comfy.is_ready(),
        "t2v_ready": comfy.t2v_ready(),
        "h3_ready": comfy.h3_ready(),
        "uncensored_ready": ai.uncensored_ready(),
    }


@router.get("/api/creative-profiles")
def get_creative_profiles():
    """读取场景库与人物库，供本机制片台复用。"""
    return _profiles()


@router.post("/api/creative-profiles")
async def save_creative_profiles(payload: dict):
    """保存制片资料；只接受白名单字段，避免把浏览器任意数据写入磁盘。"""
    try:
        return _save_profiles(payload)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"保存制片资料失败：{e}"}, 500)


@router.get("/api/tasks")
def list_tasks():
    """列出所有任务：从磁盘档案读（重启不丢），合并内存里进行中的最新状态。"""
    result = {}
    for f in OUTPUT.glob("*.json"):
        if f.name.startswith("_"):  # 跳过管线数据文件，只认任务档案
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("task_id"):
                result[meta["task_id"]] = meta
        except Exception:
            continue
    with _lock:
        for tid, t in tasks.items():
            result[tid] = _meta_dict(tid, t)
    items = list(result.values())
    # 进行中的排最前，其余按更新时间倒序（新在前）
    items.sort(key=lambda x: (0 if x.get("state") in ("queued", "running") else 1,
                              -(x.get("updated") or 0)))
    return {"tasks": items}


@router.post("/api/generate")
async def generate(
    model: str = Form("wan"),
    mode: str = Form(...),
    prompt: str = Form(""),
    resolution: str = Form("方屏 1:1"),
    duration: str = Form("长 · 约5秒"),
    seed: int = Form(-1),
    steps: int = Form(20),
    segments: int = Form(1),
    scene_id: str = Form(""),
    character_ids: str = Form(""),
    shot_note: str = Form(""),
    nsfw: str = Form("0"),
    image: UploadFile = File(None),
    last_image: UploadFile = File(None),
):
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if model not in MODELS:
        return JSONResponse({"error": "未知模型"}, 400)
    if mode not in ("i2v", "t2v"):
        return JSONResponse({"error": "未知模式"}, 400)
    if model == "h3":
        if not comfy.h3_ready():
            return JSONResponse({"error": "MiniMax H3 模型还没就绪（可能还在下载，或引擎需重启）"}, 503)
    elif mode == "t2v" and not comfy.t2v_ready():
        return JSONResponse({"error": "文生视频模型还在下载中，暂不可用"}, 503)

    nsfw_flag = nsfw == "1"
    # 无审查 H3 已支持（NaughtyTimes LoRA + 未剪枝底模），不再强制回退 Wan。

    width, height = RESOLUTIONS.get(resolution, (640, 640))
    length = (DURATIONS_H3 if model == "h3" else DURATIONS).get(duration, 124 if model == "h3" else 81)
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)

    # 步数：Wan 文生视频要双专家对半切（保证偶数），其余直接夹到 [4, MAX_STEPS]。
    steps = max(4, min(int(steps), MAX_STEPS))
    if model == "wan" and mode == "t2v" and steps % 2:
        steps += 1

    task_id = uuid.uuid4().hex[:12]

    image_name = None
    first_local = None
    if image is not None:
        raw = await image.read()
        ext = Path(image.filename or "x.png").suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return JSONResponse({"error": "请上传 png / jpg / webp 图片"}, 400)
        first_local = UPLOADS / f"{task_id}{ext}"
        first_local.write_bytes(raw)
        image_name = comfy.upload_image(first_local)

    # 尾帧（可选）：指定结束画面，H3 FL2VA 会生成「首帧→尾帧」的过渡。仅 H3 支持。
    last_image_name = None
    last_local = None
    if last_image is not None:
        if model != "h3":
            return JSONResponse({"error": "指定尾帧目前只有 MiniMax H3 支持，请把模型切到 MiniMax H3"}, 400)
        if mode != "i2v":
            return JSONResponse({"error": "指定尾帧需要同时上传首帧（图生视频模式）"}, 400)
        raw = await last_image.read()
        ext = Path(last_image.filename or "x.png").suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return JSONResponse({"error": "尾帧请上传 png / jpg / webp 图片"}, 400)
        last_local = UPLOADS / f"{task_id}_last{ext}"
        last_local.write_bytes(raw)
        last_image_name = comfy.upload_image(last_local)

    if mode == "i2v" and not image_name:
        return JSONResponse({"error": "图生视频需要先上传一张图片"}, 400)
    if not prompt.strip():
        prompt = DEFAULT_PROMPT[mode]
    creative = _resolve_creative(scene_id, character_ids)
    effective_prompt = _apply_creative(prompt, creative, shot_note)

    _update(
        task_id,
        state="queued", msg="排队中…",
        mode=mode, model=model, seed=seed, steps=steps,
        prompt=prompt, creative=creative, resolution=resolution, duration=duration,
        created=int(time.time()),
    )
    segments = max(1, min(int(segments), 6))
    if last_image_name:
        segments = 1  # 指定尾帧是「单条首尾帧过渡」，不参与多段接续
    if segments > 1:
        # 长视频：第一段按 mode，后续段「尾帧→下一段首帧」接续（i2v）
        segs = [{"mode": mode, "image": image_name, "prompt": effective_prompt}]
        for _ in range(segments - 1):
            segs.append({"mode": "i2v", "image": None, "prompt": effective_prompt})
        threading.Thread(
            target=_run_long_task,
            args=(task_id, model, segs, width, height, length, steps, seed),
            kwargs={"nsfw": nsfw_flag},
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_run_task,
            args=(task_id, model, mode, image_name, effective_prompt, seed, width, height, length, steps,
                  last_image_name, first_local, last_local),
            kwargs={"nsfw": nsfw_flag},
            daemon=True,
        ).start()
    return {"task_id": task_id, "seed": seed}


@router.post("/api/storyboard")
def storyboard_split(
    story: str = Form(...), n_shots: int = Form(6),
    scene_id: str = Form(""), character_ids: str = Form(""),
    nsfw: str = Form("0"),
):
    """把一段故事拆成分镜列表。nsfw=True 时走本地 uncensored 拆剧本（不碰云端）。

    用普通 def（非 async）：本地拆剧本要跑 3-4 分钟，同步 httpx 调用放线程池里，
    否则会卡死事件循环，整个后端（含进度轮询）一起挂起。
    """
    n_shots = max(1, min(int(n_shots), 20))  # 任意数量，但限制在合理范围
    try:
        creative = _resolve_creative(scene_id, character_ids)
        brief = _creative_brief(creative)
        context = ("\n\n全片制片设定如下。每个镜头都要遵守，人物外貌、衣着和场景基调不能漂移：\n" + brief) if brief else ""
        shots = storyboard.split_story(story, n_shots, context, local=(nsfw == "1"))
        return {"shots": shots}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"拆解失败：{e}"}, 500)


@router.post("/api/story-long")
async def story_long(
    model: str = Form("wan"),
    prompts: str = Form(""),
    resolution: str = Form("方屏 1:1"),
    duration: str = Form("长 · 约5秒"),
    seed: int = Form(-1),
    steps: int = Form(20),
    scene_id: str = Form(""),
    character_ids: str = Form(""),
    nsfw: str = Form("0"),
    nsfw_local: str = Form("1"),
):
    """故事模式长视频：把若干镜头按「首尾帧」接续，生成一条连续长视频。

    nsfw=1 走无审查（Wan + LoRA）；此时剧情接续强制本地 uncensored（DeepSeek 会拒），
    nsfw_local=0 表示用户关闭了本地接续 → 直接不做 AI 接续（bridge=False），段间只抽尾帧。
    """
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if model not in MODELS:
        return JSONResponse({"error": "未知模型"}, 400)
    if model == "h3":
        if not comfy.h3_ready():
            return JSONResponse({"error": "MiniMax H3 模型还没就绪"}, 503)
    elif not comfy.t2v_ready():
        return JSONResponse({"error": "文生视频模型还在下载中，暂不可用"}, 503)

    nsfw_flag = nsfw == "1"
    # 无审查 H3 已支持（NaughtyTimes LoRA + 未剪枝底模），不再强制回退 Wan。

    # 剧情接续走哪个文本模型：无审查内容必须本地（DeepSeek 会拒绝重写这类内容）。
    # 用户关掉「本地接续」开关（nsfw_local=0）→ 无审查成片不做 AI 接续，段间只抽尾帧接画面。
    # 普通故事模式恒定做 AI 接续（沿用云端 DeepSeek）。
    bridge = True
    if nsfw_flag and nsfw_local != "1":
        bridge = False

    creative = _resolve_creative(scene_id, character_ids)
    raw_prompts = [p.strip() for p in prompts.split("\n") if p.strip()]
    shot_prompts = [_apply_creative(p, creative) for p in raw_prompts]
    if len(shot_prompts) < 2:
        return JSONResponse({"error": "至少需要 2 个镜头才能做连续长视频"}, 400)

    width, height = RESOLUTIONS.get(resolution, (640, 640))
    length = (DURATIONS_H3 if model == "h3" else DURATIONS).get(duration, 124 if model == "h3" else 81)
    steps = max(4, min(int(steps), MAX_STEPS))
    if model == "wan" and steps % 2:
        steps += 1
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)

    task_id = uuid.uuid4().hex[:12]
    # 第一段 t2v，后续段用上一段尾帧做首帧（i2v）
    segs = [{"mode": "t2v", "image": None, "prompt": shot_prompts[0]}]
    for p in shot_prompts[1:]:
        segs.append({"mode": "i2v", "image": None, "prompt": p})

    _update(
        task_id,
        state="queued", msg="排队中…",
        mode="story", model=model, seed=seed, steps=steps,
        prompt="、".join(raw_prompts[:3]) + ("…" if len(raw_prompts) > 3 else ""),
        creative=creative,
        resolution=resolution, duration=duration,
        created=int(time.time()),
    )
    threading.Thread(
        target=_run_long_task,
        args=(task_id, model, segs, width, height, length, steps, seed, bridge),
        kwargs={"nsfw": nsfw_flag},
        daemon=True,
    ).start()
    return {"task_id": task_id, "seed": seed}


@router.post("/api/concat")
async def concat(task_ids: str = Form(...)):
    """把若干已生成的片段按顺序拼接成完整视频，返回新任务 id。"""
    ids = [t.strip() for t in task_ids.split(",") if t.strip()]
    paths = [OUTPUT / f"{tid}.mp4" for tid in ids if (OUTPUT / f"{tid}.mp4").exists()]
    if len(paths) < 2:
        return JSONResponse({"error": "至少需要 2 个已生成的片段才能拼接"}, 400)
    out_id = uuid.uuid4().hex[:12]
    out = OUTPUT / f"{out_id}.mp4"
    try:
        concat_videos(paths, out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"拼接失败：{e}"}, 500)
    _update(out_id, state="done", msg="拼接完成", video=out.name, mode="concat",
            created=int(time.time()))
    return {"task_id": out_id}


@router.get("/api/status/{task_id}")
def status(task_id: str):
    t = tasks.get(task_id)
    if not t:
        # 内存没有则回退磁盘（oneclick 审查中的任务由独立管道进程管理，后端重启不丢）
        f = OUTPUT / f"{task_id}.json"
        if f.exists():
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                t = None
        if not t:
            return JSONResponse({"error": "任务不存在"}, 404)
    d = dict(t)
    state = t.get("state")
    pid = t.get("prompt_id")
    if pid and state in ("queued", "running"):
        qs, pos = comfy.queue_status(pid)
        d["queue_state"] = qs
        d["queue_pos"] = pos
        d["progress"] = round(comfy.get_progress(pid) * 100, 1)
    elif state == "done":
        d["queue_state"] = "done"
        d["progress"] = 100.0
    else:
        d["queue_state"] = state
        d["progress"] = 0.0
    return d


@router.post("/api/cancel/{task_id}")
def cancel_task(task_id: str):
    """取消一个排队中或正在生成的任务。"""
    t = tasks.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, 404)
    if t.get("state") not in ("queued", "running"):
        return JSONResponse({"error": "任务已结束，无需取消"}, 400)
    # 先打标记，让后台线程尽快停下来（哪怕还没提交到引擎）
    t["cancelled"] = True
    _update(task_id, state="cancelled", msg="已取消")
    pid = t.get("prompt_id")
    if pid:
        comfy.cancel(pid)
    return {"task_id": task_id}


@router.get("/api/video/{task_id}")
def video(task_id: str):
    t = tasks.get(task_id)
    if t and t.get("state") == "done":
        return FileResponse(OUTPUT / t["video"], media_type="video/mp4")
    # 回退到磁盘：重启后内存清空，但视频文件仍在
    f = OUTPUT / f"{task_id}.mp4"
    if f.exists():
        return FileResponse(f, media_type="video/mp4")
    return JSONResponse({"error": "视频还没生成好"}, 404)


@router.get("/api/segment-video/{task_id}/{seg_index}")
def segment_video(task_id: str, seg_index: int):
    """审查用：返回单个镜头的段视频。"""
    f = OUTPUT / f"{task_id}_s{seg_index}.mp4"
    if f.exists():
        return FileResponse(f, media_type="video/mp4")
    return JSONResponse({"error": "段视频不存在"}, 404)


@router.get("/api/shot-preview/{task_id}/{shot_index}")
def shot_preview(task_id: str, shot_index: int):
    """分镜审查用：返回该镜的预览图（后台生成中则 404，前端降级显示占位）。"""
    f = OUTPUT / f"{task_id}_prev{shot_index}.png"
    if f.exists():
        return FileResponse(f, media_type="image/png")
    return JSONResponse({"error": "预览图还没生成"}, 404)


def _resolve_creative(scene_id: str, character_ids: str) -> dict:
    data = _profiles()
    scene = next((x for x in data["scenes"] if x["id"] == scene_id), None)
    wanted = {x for x in character_ids.split(",") if x}
    characters = [x for x in data["characters"] if x["id"] in wanted]
    return {"scene": scene, "characters": characters}


def _creative_brief(creative: dict) -> str:
    """把制片设定写成模型能理解、且可跨镜头重复使用的提示词前置。"""
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

# 拼接视频用的 ffmpeg 路径已统一在 shared.paths.FFMPEG（import 进来）




# ---- 审查路由（从 service/review 移入：HTTP 端点归接口层） ----
@router.post("/api/review/storyboard")
def review_storyboard(
    task_id: str = Form(...),
    feedback: str = Form(""),
    approve: str = Form("0"),
):
    """分镜审查：approve=1 直接放行；否则 feedback（"镜号: 意见" 每行一条）交给 qwen 改写对应分镜后返回新分镜。

    改写走本地 uncensored 模型。approve 与 feedback 可同发：先改写再放行。
    """
    t = _task_or_none(task_id)
    if not t or t.get("review_stage") != "storyboard":
        return JSONResponse({"error": "任务不在分镜审查阶段"}, 400)
    shots = t.get("shots", [])
    if not shots:
        return JSONResponse({"error": "任务没有分镜数据"}, 400)

    changed = False
    if feedback.strip() and approve != "1":
        # 解析意见："3: 她应该坐下" / "3：xxx"（每行一条）
        notes = {}
        for line in feedback.strip().splitlines():
            m = re.match(r"^\s*(\d+)\s*[:：]\s*(.+)$", line.strip())
            if m:
                notes[int(m.group(1))] = m.group(2).strip()
        if not notes:
            return JSONResponse({"error": "意见格式：镜号: 意见（每行一条），例如「3: 她应该坐下」"}, 400)
        for idx, note in notes.items():
            if not (1 <= idx <= len(shots)):
                continue
            s = shots[idx - 1]
            rewritten = ai.uncensored_text(
                "你是短剧分镜师。按审查意见改写这个镜头的英文视频生成提示词（60~90 词，"
                "含主体动作/镜头/光影，动作慢速），只输出新提示词。\n"
                f"原提示词：{s.get('prompt')}\n审查意见：{note}",
                "改写。",
            )
            if rewritten:
                s["prompt"] = rewritten.strip()
                s["review_note"] = note
                changed = True
        if changed:
            _update(task_id, shots=shots, msg="已按审查意见改写分镜")
            return {"status": "revised", "shots": shots}

    if approve == "1":
        _update(task_id, state="queued", review_stage=None, resampled=[],
                msg="审查通过，开始人物抽卡与生成…")
        threading.Thread(
            target=_oc_stage2,
            args=(task_id, shots, t.get("style", "real"), RESOLUTIONS.get(t.get("resolution", ""), (480, 832))[0],
                  RESOLUTIONS.get(t.get("resolution", ""), (480, 832))[1], 81, 20, t.get("seed", 42)),
            daemon=True,
        ).start()
        return {"status": "approved"}

    return JSONResponse({"error": "请提供 feedback（改写）或 approve=1（放行）"}, 400)


@router.post("/api/review/resample")
def review_resample(
    task_id: str = Form(...),
    shot_index: int = Form(...),
    new_prompt: str = Form(""),
):
    """单镜重抽：把该镜标记进 resampled（可选带新提示词），起线程重抽该镜并重拼全片。"""
    t = _task_or_none(task_id)
    if not t or t.get("review_stage") != "final":
        return JSONResponse({"error": "任务不在成片审查阶段"}, 400)
    shots = t.get("shots", [])
    if not (0 <= shot_index < len(shots)):
        return JSONResponse({"error": "镜号超出范围"}, 400)
    if new_prompt.strip():
        shots[shot_index]["prompt"] = new_prompt.strip()
        _update(task_id, shots=shots)
    with _lock:
        tt = tasks.setdefault(task_id, {})
        res = tt.setdefault("resampled", [])
        if shot_index not in res:
            res.append(shot_index)
    w, h = RESOLUTIONS.get(t.get("resolution", ""), (480, 832))
    _update(task_id, state="running", review_stage=None, msg=f"第 {shot_index + 1} 镜重抽中…")
    threading.Thread(
        target=_rebuild_and_concat,
        args=(task_id, w, h, 81, 20, t.get("seed", 42) + 777, t.get("style", "real"), shots,
              t.get("character_card")),
        daemon=True,
    ).start()
    return {"status": "resampling", "shot_index": shot_index}


@router.post("/api/review/fixcheck")
def review_fixcheck(task_id: str = Form(...)):
    """成片检查修复：视觉模型比对相邻段边界（前段尾帧 vs 后段首帧），断裂段自动标记重抽并重拼。"""
    t = _task_or_none(task_id)
    if not t or t.get("shots"):
        pass
    else:
        return JSONResponse({"error": "任务没有分镜数据"}, 400)
    shots = t.get("shots", [])
    broken = []
    for i in range(len(shots) - 1):
        prev_vid = OUTPUT / f"{task_id}_s{i}.mp4"
        next_vid = OUTPUT / f"{task_id}_s{i + 1}.mp4"
        if not (prev_vid.exists() and next_vid.exists()):
            broken.append(i + 1)
            continue
        f_prev = OUTPUT / f"{task_id}_s{i}_chk.png"
        f_next = OUTPUT / f"{task_id}_s{i + 1}_chk.png"
        extract_last_frame(prev_vid, f_prev)
        # 后段首帧用 ffmpeg 第一帧
        subprocess.run([FFMPEG, "-y", "-i", str(next_vid), "-frames:v", "1", str(f_next)],
                       capture_output=True, timeout=120)
        d1 = ai.describe_image(f_prev)
        d2 = ai.describe_image(f_next)
        f_prev.unlink(missing_ok=True)
        f_next.unlink(missing_ok=True)
        # 画面描述主体词完全不重叠视为断裂（宽松启发式：共享 ≥1 个 2 字以上中文词则连贯）
        words1 = {d1[j:j + 2] for j in range(len(d1) - 1)} if d1 else set()
        words2 = {d2[j:j + 2] for j in range(len(d2) - 1)} if d2 else set()
        overlap = len(words1 & words2)
        if not d1 or not d2 or overlap == 0:
            broken.append(i + 1)  # 后段（i+1）重抽：接不上前段
    if not broken:
        return {"status": "ok", "broken": []}
    with _lock:
        tt = tasks.setdefault(task_id, {})
        tt["resampled"] = sorted(set(broken))
    w, h = RESOLUTIONS.get(t.get("resolution", ""), (480, 832))
    _update(task_id, state="running", review_stage=None,
            msg=f"检查发现 {len(broken)} 处断裂（第 {'、'.join(str(b + 1) for b in broken)} 镜），自动重抽…")
    threading.Thread(
        target=_rebuild_and_concat,
        args=(task_id, w, h, 81, 20, t.get("seed", 42) + 888, t.get("style", "real"), shots,
              t.get("character_card")),
        daemon=True,
    ).start()
    return {"status": "fixing", "broken": broken}


@router.post("/api/review/approve")
def review_approve(task_id: str = Form(...)):
    """成片审查通过：清理段视频与抽卡残留，任务完成。"""
    t = _task_or_none(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, 404)
    for f in OUTPUT.glob(f"{task_id}_s*.mp4"):
        f.unlink(missing_ok=True)
    for k in ("resampled", "seg_meta", "review_stage"):
        with _lock:
            tasks.get(task_id, {}).pop(k, None)
    _update(task_id, state="done", review_stage=None, msg="审查通过，成片完成")
    return {"status": "done"}


@router.post("/api/review/recard")
def review_recard(task_id: str = Form(...)):
    """重跑人物抽卡：丢弃旧锚，用新种子重抽 4 候选 → 重选 → 重锚第 1 镜重抽 → 重拼全片。

    首尾帧重新抽卡的入口。第 1 镜换新锚后，后续镜仍保留旧画面（重抽单镜可叠加）。
    """
    t = _task_or_none(task_id)
    if not t or t.get("review_stage") != "final":
        return JSONResponse({"error": "任务不在成片审查阶段"}, 400)
    shots = t.get("shots", [])
    if not shots:
        return JSONResponse({"error": "任务没有分镜数据"}, 400)
    # 丢弃旧锚文件，强制阶段2重抽卡
    old_card = t.get("character_card")
    if old_card:
        (OUTPUT / f"{old_card}.mp4").unlink(missing_ok=True)
        (OUTPUT / f"{old_card}_anchor.png").unlink(missing_ok=True)
    w, h = RESOLUTIONS.get(t.get("resolution", ""), (480, 832))
    new_seed = (t.get("seed", 42) or 42) + random.randint(100, 999)
    with _lock:
        tt = tasks.setdefault(task_id, {})
        tt["character_card"] = None
        tt.setdefault("resampled", [])
        if 0 not in tt["resampled"]:
            tt["resampled"].append(0)  # 第 1 镜必须用新锚重抽
    _update(task_id, state="running", review_stage=None, msg="重跑人物抽卡（4 候选）+ 重锚第 1 镜…")
    threading.Thread(
        target=_oc_stage2,
        args=(task_id, shots, t.get("style", "real"), w, h, 81, 20, new_seed),
        daemon=True,
    ).start()
    return {"status": "recarding", "new_seed": new_seed}


# ---- 逐镜审查端点：每镜生成完暂停，通过/重抽，全部通过后拼片 ----


@router.post("/api/review/shot_next")
def review_shot_next(task_id: str = Form(...)):
    """逐镜审查：通过当前镜 → 生成下一镜。全部镜通过时自动拼片进成片审查。"""
    t = _task_or_none(task_id)
    if not t or t.get("state") != "awaiting_shot":
        return JSONResponse({"error": "任务不在逐镜审查阶段"}, 400)
    shots = t.get("shots", [])
    with _lock:
        tt = tasks.setdefault(task_id, {})
        cur = tt.get("cur_shot", 0)
        tt["cur_shot"] = cur + 1
        tt.setdefault("approved_shots", []).append(cur)
    if cur + 1 >= len(shots):
        _update(task_id, state="running", msg="全部镜头通过，拼接成片…")
        threading.Thread(
            target=_finish_all_shots,
            args=(task_id, shots),
            daemon=True,
        ).start()
        return {"status": "concatenating"}
    _update(task_id, state="running", msg=f"第 {cur + 2}/{len(shots)} 镜准备中…")
    w, h = RESOLUTIONS.get(t.get("resolution", ""), (480, 832))
    threading.Thread(
        target=_oc_stage2,
        args=(task_id, shots, t.get("style", "real"), w, h, 81, 20, t.get("seed", 42) or 42),
        daemon=True,
    ).start()
    return {"status": "next_shot", "next": cur + 1}


@router.post("/api/review/shot_resample")
def review_shot_resample(task_id: str = Form(...), new_prompt: str = Form("")):
    """逐镜审查：重抽当前镜（可选换提示词）。"""
    t = _task_or_none(task_id)
    if not t or t.get("state") != "awaiting_shot":
        return JSONResponse({"error": "任务不在逐镜审查阶段"}, 400)
    shots = t.get("shots", [])
    with _lock:
        tt = tasks.setdefault(task_id, {})
        cur = tt.get("cur_shot", 0)
    if new_prompt.strip():
        shots[cur]["prompt"] = new_prompt.strip()
        _update(task_id, shots=shots)
    _update(task_id, state="running", msg=f"第 {cur + 1} 镜重抽中…")
    w, h = RESOLUTIONS.get(t.get("resolution", ""), (480, 832))
    threading.Thread(
        target=_oc_stage2,
        args=(task_id, shots, t.get("style", "real"), w, h, 81, 20, (t.get("seed", 42) or 42) + random.randint(1, 9999)),
        daemon=True,
    ).start()
    return {"status": "resampling", "shot": cur}


@router.post("/api/review/shot_finish")
def review_shot_finish(task_id: str = Form(...)):
    """逐镜审查：跳过剩余镜直接拼片（用户不想逐镜看完时用）。"""
    t = _task_or_none(task_id)
    if not t or t.get("state") != "awaiting_shot":
        return JSONResponse({"error": "任务不在逐镜审查阶段"}, 400)
    shots = t.get("shots", [])
    _update(task_id, state="running", msg="拼接成片…")
    threading.Thread(
        target=_finish_all_shots,
        args=(task_id, shots),
        daemon=True,
    ).start()
    return {"status": "concatenating"}


# ---- 意见箱：用户一键吐槽 + AI 处理后回信（自我迭代的原料入口） ----


@router.post("/api/feedback")
async def add_feedback(text: str = Form(...), page: str = Form("")):
    """新增一条吐槽。文字必填，超长截断；不弹窗打断用户。"""
    if not text.strip():
        return JSONResponse({"error": "吐槽内容不能为空"}, 400)
    item = feedback_store.add_feedback(text.strip(), page)
    return {"ok": True, "id": item["id"]}


@router.get("/api/feedback")
def list_feedback():
    """意见列表：未处理在前。AI 处理意见箱时也走这里（AI 第一性：双入口）。"""
    return {"items": feedback_store.list_feedback()}


@router.get("/api/feedback/changelog")
def feedback_changelog():
    """成长日志原文（AI 第一性：不开界面也能读应用学会了什么）。"""
    f = feedback_store.FEEDBACK / "changelog.md"
    if not f.exists():
        return {"text": "（暂无成长日志）"}
    return {"text": f.read_text(encoding="utf-8", errors="ignore")}


