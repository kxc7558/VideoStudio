# -*- coding: utf-8 -*-
"""出片台 —— 面向小白的一键出视频客户端后端，封装 ComfyUI 的 I2V/T2V。"""
import json
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import ai
import comfy
import storyboard

BASE = Path(__file__).parent
WORKFLOWS = BASE / "workflows"
OUTPUT = BASE / "output"
UPLOADS = BASE / "uploads"
CREATIVE_PROFILES = OUTPUT / "creative_profiles.json"
OUTPUT.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="出片台")
app.mount("/web", StaticFiles(directory=BASE / "web"), name="web")

# 启动后连 ComfyUI 的 WebSocket，实时收采样进度（引擎没起时会自动反复重连，无副作用）
comfy.start_progress_listener()

tasks = {}
_lock = threading.Lock()


def _reconcile_stale_tasks():
    """重启后把上次遗留的 queued/running 任务标记为中断（避免一直显示「进行中」）。

    oneclick 模式的任务由独立管道进程管理（后端重启不影响它），跳过不标。
    """
    for f in OUTPUT.glob("*.json"):
        if f.name.startswith("_"):  # 下划线开头是管线数据文件（如 _nsfw_shots.json），不是任务档案
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("mode") == "oneclick":
            continue  # 独立管道任务：后端重启与它无关，别误杀
        if meta.get("state") in ("queued", "running"):
            meta["state"] = "error"
            meta["msg"] = "上次运行被重启打断"
            try:
                f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


_reconcile_stale_tasks()

# 分辨率预设（中文标签 -> (宽, 高)）
RESOLUTIONS = {
    "竖屏 9:16": (480, 832),
    "方屏 1:1": (640, 640),
    "横屏 16:9": (832, 480),
}
# 时长预设（中文标签 -> 帧数 length，Wan 要求 4n+1）
DURATIONS = {
    "短 · 约2秒": 33,
    "中 · 约3秒": 49,
    "长 · 约5秒": 81,
}
# MiniMax H3 的时长帧数（24fps，17k+5 网格：56/73/124 分别约 2.3/3.0/5.2 秒）
DURATIONS_H3 = {
    "短 · 约2秒": 56,
    "中 · 约3秒": 73,
    "长 · 约5秒": 124,
}
# 可切换的模型：wan（Wan 2.2 双专家）/ h3（MiniMax H3 单模型）
MODELS = ("wan", "h3")
# 文生视频步数档位（前端直接传步数数字）。图生视频固定 4 步（LightX2V 蒸馏锁死），不接受自选。
MAX_STEPS = 50

DEFAULT_PROMPT = {
    "i2v": "画面自然流畅地动起来，动作连贯，镜头缓慢推进，高画质，细节丰富",
    "t2v": "电影级画质，细节丰富，画面自然流畅，镜头缓慢推进",
}

_PROFILE_FIELDS = {
    "scenes": ("id", "name", "place", "era", "atmosphere", "lighting", "palette", "camera"),
    "characters": ("id", "name", "identity", "appearance", "wardrobe", "behavior"),
}


def _profiles() -> dict:
    """读取本地制片资料；损坏文件时回退为空，不能影响出片。"""
    try:
        data = json.loads(CREATIVE_PROFILES.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {kind: data.get(kind, []) for kind in _PROFILE_FIELDS}
    except Exception:
        pass
    return {kind: [] for kind in _PROFILE_FIELDS}


def _clean_profiles(data: dict) -> dict:
    cleaned = {}
    for kind, fields in _PROFILE_FIELDS.items():
        items = data.get(kind, []) if isinstance(data, dict) else []
        cleaned[kind] = [
            {field: str(item.get(field, ""))[:800] for field in fields}
            for item in items[:30] if isinstance(item, dict) and str(item.get("id", ""))
        ]
    return cleaned


def _save_profiles(data: dict) -> dict:
    cleaned = _clean_profiles(data)
    CREATIVE_PROFILES.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned


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

# 拼接视频用的 ffmpeg（复用 ComfyUI 自带的二进制，避免再下载）
FFMPEG = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"


def _video_duration(path: Path) -> float:
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


def _hard_concat(paths: list, out: Path) -> Path:
    """硬切拼接（兜底，重新编码保证参数一致）。"""
    list_file = out.with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in paths), encoding="utf-8"
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


def concat_videos(paths: list, out: Path, transition: float = 0.9) -> Path:
    """按顺序把多个 mp4 用「交叉溶解」拼成一条（两段之间叠化过渡，避免硬切跳变）。

    任一视频时长读不出来或太短时，退回硬切拼接兜底，保证一定出片。
    """
    if len(paths) < 2:
        subprocess.run(
            [FFMPEG, "-y", "-i", str(paths[0]),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)],
            capture_output=True, timeout=1800,
        )
        return out
    durs = [_video_duration(p) for p in paths]
    if any(d <= transition for d in durs):
        return _hard_concat(paths, out)
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


def extract_last_frame(video_path: Path, out_png: Path) -> Path:
    """用 ffmpeg 抽视频最后一帧存成 png，供下一段当首帧（首尾帧接续）。"""
    subprocess.run(
        [FFMPEG, "-y", "-sseof", "-0.2", "-i", str(video_path),
         "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=300,
    )
    return out_png


def _meta_dict(task_id: str, t: dict) -> dict:
    """把内存任务转成要落盘的档案。"""
    return {
        "task_id": task_id,
        "mode": t.get("mode", ""),
        "model": t.get("model", ""),
        "prompt": t.get("prompt", ""),
        "creative": t.get("creative", {}),
        "ai_prompts": t.get("ai_prompts", []),
        "resolution": t.get("resolution", ""),
        "duration": t.get("duration", ""),
        "steps": t.get("steps"),
        "seed": t.get("seed"),
        "style": t.get("style", ""),
        "state": t.get("state", ""),
        "msg": t.get("msg", ""),
        "video": t.get("video", ""),
        "review_stage": t.get("review_stage"),
        "shots": t.get("shots", []),
        "resampled": t.get("resampled", []),
        "seg_meta": t.get("seg_meta", {}),
        "character_card": t.get("character_card"),
        "cur_shot": t.get("cur_shot"),
        "approved_shots": t.get("approved_shots", []),
        "created": t.get("created", 0),
        "updated": t.get("updated", 0),
    }


def _write_meta(task_id: str, t: dict):
    """把任务档案写到磁盘（重启不丢）；失败不抛出，避免拖垮生成流程。"""
    try:
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / f"{task_id}.json").write_text(
            json.dumps(_meta_dict(task_id, t), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _update(task_id: str, **kw):
    with _lock:
        t = tasks.setdefault(task_id, {})
        # 跨进程安全：内存没有这个任务时，先把磁盘档案并进来，避免空壳覆盖完整数据
        if not t:
            f = OUTPUT / f"{task_id}.json"
            if f.exists():
                try:
                    disk = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(disk, dict) and disk.get("task_id"):
                        tasks[task_id] = disk
                        t = tasks[task_id]
                except Exception:
                    pass
        t.update(kw)
        t["updated"] = int(time.time())
        t.setdefault("task_id", task_id)
        _write_meta(task_id, t)


# 可选的无审查 LoRA（Wan 2.2 LightX2V 4-step 去审查对）：
# 高/低噪声各一个，挂到对应专家的 ModelSamplingSD3 上游。
# 节点 id 用独立大号（"200"/"201" 字符串，ComfyUI prompt 键一律是字符串），避免和原工作流冲突。
NSFW_LORA = {
    "high": "Wan2.2_LightX2V_high_n54vv.safetensors",
    "low": "Wan2.2_LightX2V_low_n54vv.safetensors",
}

# 动漫风格 LoRA（Civitai「Anime Style [WAN 2.2 I2V]」v2，9971dl/330赞）。
# 触发词 An1meStyl3 须出现在正向提示词里才激活；叠加在无审查 LoRA 之后。
ANIME_LORA = {
    "high": "wan2.2_i2v_animestyle_v2_high.safetensors",
    "low": "wan2.2_i2v_animestyle_v2_low.safetensors",
    "trigger": "An1meStyl3",
}

# NSFW 增强 LoRA（rzgar uncensored-base 仓库的 CubeyAI-GeneralN）。
# 作者建议外来 LoRA 以 0.55~0.65 强度叠加；接在动漫 LoRA 之后（链尾）。
ENHANCER_LORA = {
    "high": "CubeyAI-GeneralN-High.safetensors",
    "low": "CubeyAI-GeneralN-Low.safetensors",
    "strength": 0.6,
}


def _apply_wan_nsfw_lora(wf, high_node="8", low_node="9", anime=False, enhancer=False):
    """在已加载的 Wan 2.2 工作流上，给高/低噪声专家注入 LoRA 链。

    链路：原 GGUF → 无审查 LoRA(1.0) → [动漫 LoRA(1.0) →] [NSFW 增强(0.6) →] ModelSamplingSD3。
    同侧（high/low）逐级串接：上一级 LoRA 节点的 model 输入取自该侧当前上游，输出回填。
    节点 id 一律字符串："200"/"201" 无审查对，"210"/"211" 动漫对，"220"/"221" 增强对。
    i2v 与 t2v 的 ModelSamplingSD3 节点号不同（i2v 是 8/9，t2v 是 7/8），由调用方传入。
    """
    sides = {
        "high": {"target": high_node, "next_id": "200"},
        "low": {"target": low_node, "next_id": "201"},
    }
    loras = {"high": [], "low": []}
    loras["high"].append((NSFW_LORA["high"], 1.0))
    loras["low"].append((NSFW_LORA["low"], 1.0))
    if anime:
        loras["high"].append((ANIME_LORA["high"], 1.0))
        loras["low"].append((ANIME_LORA["low"], 1.0))
    if enhancer:
        loras["high"].append((ENHANCER_LORA["high"], ENHANCER_LORA["strength"]))
        loras["low"].append((ENHANCER_LORA["low"], ENHANCER_LORA["strength"]))

    for side in ("high", "low"):
        cfg = sides[side]
        head = cfg["target"]          # 该侧当前链头（最初是 ModelSamplingSD3 节点）
        base_id = int(cfg["next_id"])
        for k, (lora_file, strength) in enumerate(loras[side]):
            node_id = str(base_id + k * 10)
            wf[node_id] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": wf[head]["inputs"]["model"],  # 继承链头当前的上游
                    "lora_name": lora_file,
                    "strength_model": strength,
                },
            }
            wf[head]["inputs"]["model"] = [node_id, 0]  # 链头改接本 LoRA
            head = node_id  # 本 LoRA 成为新链头，下一级插在它前面
    return wf


def _build_wan_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, use_lora=True, style="real"):
    """Wan 2.2 工作流：i2v 用 LightX2V 4 步双专家，t2v 用双专家对半切步数。

    use_lora=True 时注入无审查 LoRA 链（nsfw 链路）；
    style="anime" 时叠加动漫 LoRA 对并在提示词前加触发词 An1meStyl3。
    t2v 挂 LoRA 时锁 4 步 cfg=1（蒸馏 LoRA 配方，原版 20 步会过采样糊掉）。
    """
    wf = json.loads((WORKFLOWS / f"{mode}_api.json").read_text(encoding="utf-8"))
    prefix = f"video/{task_id}"
    anime = use_lora and style == "anime"
    if anime:
        prompt = f"{ANIME_LORA['trigger']}, {prompt}"
    if mode == "i2v":
        if use_lora:
            wf = _apply_wan_nsfw_lora(wf, anime=anime)
        wf["1"]["inputs"]["image"] = image_name
        wf["3"]["inputs"]["text"] = prompt
        wf["10"]["inputs"].update({"width": width, "height": height, "length": length})
        wf["11"]["inputs"]["noise_seed"] = seed
        wf["12"]["inputs"]["noise_seed"] = seed
        wf["15"]["inputs"]["filename_prefix"] = prefix
    else:  # t2v
        if use_lora:
            wf = _apply_wan_nsfw_lora(wf, high_node="7", low_node="8", anime=anime)
            # 蒸馏 LoRA 采样配方：4 步、cfg=1、对半切 0-2/2-4
            wf["10"]["inputs"].update({"noise_seed": seed, "steps": 4, "cfg": 1.0, "start_at_step": 0, "end_at_step": 2})
            wf["11"]["inputs"].update({"noise_seed": seed, "steps": 4, "cfg": 1.0, "start_at_step": 2, "end_at_step": 4})
        else:
            # 双专家：高噪声专家跑前半段、低噪声专家跑后半段，步数对半切。
            half = steps // 2
            wf["10"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": 0, "end_at_step": half})
            wf["11"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": half, "end_at_step": steps})
        wf["2"]["inputs"]["text"] = prompt
        wf["9"]["inputs"].update({"width": width, "height": height, "length": length})
        wf["14"]["inputs"]["filename_prefix"] = prefix
    return wf
    return wf


def _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, last_frame_name=None):
    """MiniMax H3 工作流：单模型（FL2VA）同时支持 t2v 与 i2v（首帧）；可选指定尾帧做「首尾帧过渡」。"""
    wf = json.loads((WORKFLOWS / f"h3_{mode}_api.json").read_text(encoding="utf-8"))
    wf["4"]["inputs"].update({"prompt": prompt, "width": width, "height": height, "length": length})
    wf["5"]["inputs"]["noise_seed"] = seed
    wf["8"]["inputs"]["steps"] = steps
    wf["12"]["inputs"]["filename_prefix"] = f"video/{task_id}"
    if mode == "i2v":
        wf["0"]["inputs"]["image"] = image_name
        if last_frame_name:
            wf["13"] = {"class_type": "LoadImage", "inputs": {"image": last_frame_name}}
            wf["4"]["inputs"]["last_frame"] = ["13", 0]
    return wf


def _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps=20, last_frame_name=None, use_lora=True, style="real"):
    if model == "h3":
        return _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, last_frame_name)
    return _build_wan_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, use_lora, style)


def _cancelled(task_id: str) -> bool:
    return bool(tasks.get(task_id, {}).get("cancelled"))


def _run_task(task_id, model, mode, image_name, prompt, seed, width, height, length, steps, last_image_name=None, first_local_path=None, last_local_path=None, nsfw=False, style="real"):
    try:
        if _cancelled(task_id):
            return
        _update(task_id, state="running", msg="正在生成，请稍候…")
        seconds = length / 24.0 if model == "h3" else 0.0
        # 指定首尾帧：先让本地视觉模型看首尾两帧，再写过渡提示词（H3 FL2VA 官方格式，尽力而为）
        # nsfw=True 时走本地 uncensored 模型（无审查出片不碰云端 DeepSeek）。
        if last_image_name and first_local_path and last_local_path:
            _update(task_id, msg="正在分析首尾帧，编写过渡提示词…")
            first_desc = ai.describe_image(first_local_path)
            last_desc = ai.describe_image(last_local_path)
            if first_desc and last_desc:
                t = ai.transition_prompt(first_desc, last_desc, prompt, seconds, local=nsfw)
                if t:
                    _update(task_id, ai_prompts=[{"segment": 1, "original": prompt, "rewritten": t}])
                    prompt = t
        elif model == "h3" and not nsfw:
            # 单段 H3：把普通提示词按 H3 官方格式改写（尽力而为，失败沿用原提示词）
            _update(task_id, msg="正在按 MiniMax H3 官方格式改写提示词…")
            t = ai.h3_prompt(prompt, mode, seconds)
            if t:
                _update(task_id, ai_prompts=[{"segment": 1, "original": prompt, "rewritten": t}])
                prompt = t
        wf = _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps, last_image_name, use_lora=(nsfw and model == "wan"), style=style)
        prompt_id = comfy.submit(wf)
        if _cancelled(task_id):
            comfy.cancel(prompt_id)
            return
        _update(task_id, prompt_id=prompt_id, msg="已提交到生成队列…")
        ok, history = comfy.wait_done(prompt_id, should_cancel=lambda: _cancelled(task_id))
        if _cancelled(task_id):
            return
        if not ok:
            _update(task_id, state="error", msg="生成失败或超时，请重试")
            return
        video = comfy.find_video(history)
        if not video:
            _update(task_id, state="error", msg="未找到生成的视频")
            return
        dest = OUTPUT / f"{task_id}.mp4"
        comfy.download_video(video, dest)
        _update(task_id, state="done", msg="完成", video=dest.name)
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


def _run_long_task(task_id, model, segments, width, height, length, steps, seed, bridge=False, nsfw=False, style="real", review_each=False, keep_existing=False):
    """长视频：按 segments 逐段生成，段间「尾帧→下一段首帧」接续，最后拼接成一条。

    bridge=True 时（故事模式），每段完成后用本地视觉模型回看实际尾帧，再重写下一段提示词，
    让剧情踩在真实画面上发展、不漂移。
    nsfw=True 时：重写走本地 uncensored 模型（不碰云端），且 workflow 注入无审查 LoRA。
    review_each=True：每镜完成后记录段产物（审查面板展示），支持 resampled 标记换 seed 重抽；
    拼接后不直接完成，进入成片审查（awaiting_review/final）。
    keep_existing=True：mode="keep" 的段跳过生成，直接用磁盘上的已有段视频（审查重拼用）。
    """
    seg_videos = []
    ai_prompts = []
    seconds = length / 24.0 if model == "h3" else 0.0
    # H3 官方格式：第一段（首镜头）先改写成官方格式；后续段在 bridge 里改写（尽力而为）。
    # nsfw 无审查只走 Wan，H3 改写在此跳过。
    if model == "h3" and not nsfw and segments:
        orig0 = segments[0]["prompt"]
        t0 = ai.h3_prompt(orig0, segments[0]["mode"], seconds)
        if t0:
            segments[0]["prompt"] = t0
            ai_prompts.append({"segment": 1, "original": orig0, "rewritten": t0})
            _update(task_id, ai_prompts=ai_prompts)
    try:
        for i, seg in enumerate(segments):
            if _cancelled(task_id):
                return
            seg_id = f"{task_id}_s{i}"
            # 审查重拼：mode="keep" 的段直接复用磁盘上的旧视频
            if keep_existing and seg.get("mode") == "keep":
                existing = OUTPUT / seg.get("keep_video", f"{seg_id}.mp4")
                if existing.exists():
                    seg_videos.append(existing)
                    continue
                # 旧段丢了就退回正常生成（t2v 兜底）
                seg = {"mode": "t2v", "image": None, "prompt": seg.get("prompt", "")}
            # 用户审查标记的重抽：换 seed 重试（标记由 /api/review/resample 写入 resampled 列表）
            cur_seed = seed + i
            resampled = tasks.get(task_id, {}).get("resampled", [])
            if review_each and i in resampled:
                cur_seed = seed + i + 10000 * (len(resampled) + 1)  # 换种子重抽
                _update(task_id, msg=f"第 {i + 1} 镜按审查意见重抽…")
            attempt = 0
            while True:
                wf = _build_workflow(model, seg["mode"], seg.get("image"), seg["prompt"],
                                     cur_seed, width, height, length, seg_id, steps,
                                     use_lora=(nsfw and model == "wan"), style=style)
                pid = comfy.submit(wf)
                _update(task_id, state="running", prompt_id=pid,
                        msg=f"第 {i + 1}/{len(segments)} 段生成中…")
                if _cancelled(task_id):
                    comfy.cancel(pid)
                    return
                ok, history = comfy.wait_done(pid, should_cancel=lambda: _cancelled(task_id))
                if _cancelled(task_id):
                    return
                if ok and comfy.find_video(history):
                    break
                attempt += 1
                if attempt >= 2:
                    _update(task_id, state="error", msg=f"第 {i + 1} 段生成失败或超时")
                    return
                cur_seed += 1  # 自动重试换 seed
            video = comfy.find_video(history)
            if not video:
                _update(task_id, state="error", msg=f"第 {i + 1} 段未找到视频")
                return
            seg_dest = OUTPUT / f"{seg_id}.mp4"
            comfy.download_video(video, seg_dest)
            seg_videos.append(seg_dest)
            # 段产物记录（审查面板展示每镜视频 + 重抽/换锚入口）
            if review_each:
                with _lock:
                    t = tasks.setdefault(task_id, {})
                    seg_meta = t.setdefault("seg_meta", {})
                    seg_meta[str(i)] = {"video": seg_dest.name, "prompt": seg["prompt"]}
                    _write_meta(task_id, t)
            # 抽尾帧，作为下一段首帧（i2v 接续）
            if i < len(segments) - 1:
                frame_png = OUTPUT / f"{seg_id}_last.png"
                extract_last_frame(seg_dest, frame_png)
                segments[i + 1]["mode"] = "i2v"
                segments[i + 1]["image"] = comfy.upload_image(frame_png)
                # 尾帧回看：看实际结尾 → 重写下一段提示词（尽力而为，失败沿用原提示词）
                if bridge:
                    _update(task_id, msg=f"第 {i + 1}/{len(segments)} 段完成，正在分析结尾画面、接续下一段…")
                    prev_desc = ai.describe_image(frame_png)
                    original = segments[i + 1]["prompt"]
                    if model == "h3":
                        # H3：优先「回看尾帧→按官方格式接续」，失败退到纯官方格式改写（都输出官方格式）
                        bridged = ai.bridge_next_prompt(prev_desc, original, "h3", seconds, local=nsfw) if prev_desc else ""
                        if not bridged:
                            bridged = ai.h3_prompt(original, "i2v", seconds)
                    else:
                        bridged = ai.bridge_next_prompt(prev_desc, original, local=nsfw) if prev_desc else ""
                    if bridged:
                        segments[i + 1]["prompt"] = bridged
                        ai_prompts.append({"segment": i + 2, "original": original, "rewritten": bridged})
                        _update(task_id, ai_prompts=ai_prompts)
                # 尾帧只是段间临时素材，用完删掉，避免 output 里越攒越多
                frame_png.unlink(missing_ok=True)
        # 拼接所有段成一条长视频
        dest = OUTPUT / f"{task_id}.mp4"
        concat_videos(seg_videos, dest)
        if not review_each:
            for v in seg_videos:
                v.unlink(missing_ok=True)
        # review_each 模式：段视频保留（供单镜重抽/换锚），进入成片审查等待
        if review_each:
            _update(task_id, state="awaiting_review", review_stage="final",
                    msg="成片已出，请审查；可单镜重抽/换锚/自动检查修复，或点「通过」完成")
        else:
            _update(task_id, state="done", msg="完成", video=dest.name)
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


# ---- 一键成片：故事 → 本地写剧本 → 拆镜 → 人物抽卡锚定 → 逐镜生成 → 拼片 ----
# 极简 UI 背后的全自动管道。用户只给一句话/一段故事，其余全自动，内容只在本地流转。

def _generate_single(task_id, mode, image_name, prompt, seed, width, height, length, steps, nsfw, style="real"):
    """生成单个片段并下载到 output/{task_id}.mp4，返回 (成功?, 视频路径)。供抽卡复用。"""
    wf = _build_workflow("wan", mode, image_name, prompt, seed, width, height, length, task_id, steps, use_lora=nsfw, style=style)
    pid = comfy.submit(wf)
    ok, history = comfy.wait_done(pid, should_cancel=lambda: _cancelled(task_id))
    if not ok:
        return False, None
    video = comfy.find_video(history)
    if not video:
        return False, None
    dest = OUTPUT / f"{task_id}.mp4"
    comfy.download_video(video, dest)
    return True, dest


def _pick_best_character_card(candidates):
    """人物抽卡自动选：用本地视觉模型描述候选首帧，选描述信息最全（人物最清晰）的一张。

    candidates: [(seg_id, video_path)]。返回选中的 seg_id；全失败返回 None。
    """
    best_id, best_score = None, -1
    for seg_id, video_path in candidates:
        frame_png = OUTPUT / f"{seg_id}_card.png"
        extract_last_frame(video_path, frame_png)
        desc = ai.describe_image(frame_png)
        score = len(desc)
        if score > best_score:
            best_score, best_id = score, seg_id
        frame_png.unlink(missing_ok=True)
    return best_id


def _run_oneclick_task(task_id, idea, style, width, height, length, steps, seed, script_text=""):
    """一键成片阶段1：本地编剧写故事（或用自带剧本）→ 拆镜 → 落盘分镜 → 暂停等分镜审查。

    script_text 非空时跳过写故事（用户自带剧本，长剧本走进度窗口拆镜控上下文）。
    审查通过后由 /api/review/storyboard 启动阶段2（_oc_stage2）。
    """
    try:
        NSFW_BATCH = 6        # 每批拆 6 镜
        NSFW_TARGET = 24      # 24 镜 ≈ 100 秒净长
        style_brief = {
            "anime": "全片 2D Japanese anime style，角色为动漫人物，画面是动漫赛璐璐质感",
            "real": "全片写实 live-action cinematic style，角色为真人",
        }.get(style, "写实 live-action cinematic style")

        # ① 本地写故事（提示词不外发）；有自带剧本则直接用
        if script_text.strip():
            story = script_text.strip()
            _update(task_id, state="running", msg=f"① 使用自带剧本（{len(story)} 字）")
        else:
            _update(task_id, state="running", msg="① 本地编剧正在把想法写成故事…")
            story = ai.uncensored_text(
                "你是短剧编剧。根据用户的想法创作一个完整连贯的成人向短剧故事，中文，1500 字以上，"
                f"分多个场景推进。风格要求：{style_brief}。直接输出故事正文，不要解释。",
                idea,
            )
        if not story:
            _update(task_id, state="error", msg="本地编剧失败（Ollama qwen3.8 未就绪？）")
            return

        # ② 分批拆镜（本地，不走云端）。长剧本用「进度窗口」切段落发，控 qwen3.8 上下文
        shots = []
        import storyboard as sb
        story_len = len(story)
        window = max(3000, story_len // 8)  # 每批发 ~1/8 剧本，足够拆一批镜且有前后文
        while len(shots) < NSFW_TARGET:
            if _cancelled(task_id):
                return
            done_ratio = min(0.92, len(shots) / NSFW_TARGET)
            start = int(story_len * done_ratio)
            end = min(story_len, start + window)
            if end <= start:
                break
            chunk = story[start:end]
            _update(task_id, msg=f"② 拆分镜 {len(shots)}/{NSFW_TARGET}（剧本进度 {start * 100 // max(1, story_len)}%~{end * 100 // max(1, story_len)}%）…")
            brief = (f"\n\n全片风格：{style_brief}。这是一部长剧本的连续拆镜任务，全片目标 {NSFW_TARGET} 镜，"
                     f"本批从剧本片段里拆出 {NSFW_BATCH} 个镜头，剧情按片段顺序推进，不要回头重复。")
            try:
                batch = sb.split_story(chunk, NSFW_BATCH, brief, local=True)
            except Exception:  # noqa: BLE001
                batch = []
            if not batch:
                break
            for s in batch:
                s["id"] = len(shots) + 1
                shots.append(s)
        if len(shots) < 3:
            _update(task_id, state="error", msg="拆镜失败（本地模型未就绪或输出异常）")
            return

        # ③ 暂停：分镜审查（用户可对任意镜提意见 → qwen 按意见改写后继续）
        _update(task_id, state="awaiting_review", review_stage="storyboard",
                shots=shots, story_len=len(story),
                msg=f"分镜已拆好（{len(shots)} 镜），请审查：可对任意镜填写修改意见，或直接通过")
        # ④ 后台生成每镜预览图（迷你视频抽首帧），审查面板边生成边可看
        threading.Thread(
            target=_generate_shot_previews,
            args=(task_id, shots, style, width, height, seed),
            daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


def _generate_shot_previews(task_id, shots, style, width, height, seed):
    """分镜审查阶段的预览图后台生成：每镜 17 帧迷你视频 → 抽首帧存 {task_id}_prev{i}.png。

    只为审查提供画面参考，与正片生成无关（正片在审查通过后从头生成）。
    """
    for i, s in enumerate(shots):
        if _cancelled(task_id):
            return
        prev_png = OUTPUT / f"{task_id}_prev{i}.png"
        if prev_png.exists():
            continue
        p = str(s.get("prompt", "")).strip()
        prev_id = f"{task_id}_prev{i}"
        _update(task_id, msg=f"预览图生成中 {i + 1}/{len(shots)}（不影响审查操作）…")
        try:
            wf = _build_workflow("wan", "t2v", None, p, seed + 500 + i, 240, 416, 17, prev_id, 4, use_lora=True, style=style)
            pid = comfy.submit(wf)
            ok, history = comfy.wait_done(pid, timeout=600)
            if not ok:
                continue
            vinfo = comfy.find_video(history)
            if not vinfo:
                continue
            tmp = OUTPUT / f"{prev_id}.mp4"
            comfy.download_video(vinfo, tmp)
            subprocess.run([FFMPEG, "-y", "-i", str(tmp), "-frames:v", "1", str(prev_png)],
                           capture_output=True, timeout=120)
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    _update(task_id, msg="预览图已全部生成，审查面板可直接看画面")


def _oc_stage2(task_id, shots, style, width, height, length, steps, seed, force_recard=False):
    """一键成片阶段2：人物抽卡(如无锚) → 生成「当前镜」→ 停在逐镜审查（awaiting_shot）。

    逐镜审查流：每镜生成完暂停，用户通过才生成下一镜；全部通过后拼片进成片审查。
    当前镜号存任务 meta 的 cur_shot（0 起）。
    """
    try:
        with _lock:
            tt = tasks.setdefault(task_id, {})
            cur = tt.setdefault("cur_shot", 0)
        # 人物抽卡：只在还没有锚时做（首镜前）
        card_seg_id = tasks.get(task_id, {}).get("character_card")
        if not card_seg_id:
            candidates = []
            for k in range(4):
                if _cancelled(task_id):
                    return
                _update(task_id, msg=f"③ 人物抽卡 {k + 1}/4…")
                cand_id = f"{task_id}_card{k}"
                ok, vid = _generate_single(cand_id, "t2v", None, shots[0]["prompt"], seed + 1000 + k, width, height, 33, steps, True, style)
                if ok:
                    candidates.append((cand_id, vid))
            if candidates:
                card_seg_id = _pick_best_character_card(candidates)
                _update(task_id, character_card=card_seg_id)
                for cand_id, _ in candidates:
                    if cand_id != card_seg_id:
                        (OUTPUT / f"{cand_id}.mp4").unlink(missing_ok=True)
            if not card_seg_id:
                _update(task_id, state="error", msg="人物抽卡全部失败，请重试")
                return

        # 生成「当前镜」
        if cur >= len(shots):
            _finish_all_shots(task_id, shots)
            return
        s = shots[cur]
        p = str(s.get("prompt", "")).strip()
        _update(task_id, state="running", msg=f"生成第 {cur + 1}/{len(shots)} 镜…")
        if cur == 0:
            anchor = OUTPUT / f"{card_seg_id}_anchor.png"
            if not anchor.exists():
                extract_last_frame(OUTPUT / f"{card_seg_id}.mp4", anchor)
            ok, _ = _generate_single(f"{task_id}_s0", "i2v", comfy.upload_image(anchor), p, seed + cur, width, height, length, steps, True, style)
        else:
            # 上一镜尾帧做首帧（接续）
            prev_seg = OUTPUT / f"{task_id}_s{cur - 1}.mp4"
            bridge_png = OUTPUT / f"{task_id}_bridge{cur}.png"
            extract_last_frame(prev_seg, bridge_png)
            ok, _ = _generate_single(f"{task_id}_s{cur}", "i2v", comfy.upload_image(bridge_png), p, seed + cur, width, height, length, steps, True, style)
            bridge_png.unlink(missing_ok=True)
        if not ok:
            _update(task_id, state="awaiting_shot", cur_shot=cur,
                    msg=f"第 {cur + 1} 镜生成失败，可重抽或跳过")
            return
        # 停在逐镜审查
        _update(task_id, state="awaiting_shot", cur_shot=cur,
                msg=f"第 {cur + 1}/{len(shots)} 镜已生成，请审查：通过→下一镜；重抽→换一版")
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


def _finish_all_shots(task_id, shots):
    """全部镜头通过后：把各段按顺序拼接成片，进成片审查（awaiting_review/final）。"""
    seg_videos = [OUTPUT / f"{task_id}_s{i}.mp4" for i in range(len(shots))]
    missing = [i for i, v in enumerate(seg_videos) if not v.exists()]
    if missing:
        _update(task_id, state="error", msg=f"第 {[m + 1 for m in missing]} 镜缺失，无法拼接")
        return
    dest = OUTPUT / f"{task_id}.mp4"
    concat_videos(seg_videos, dest)
    _update(task_id, state="awaiting_review", review_stage="final",
            msg="全镜通过，已拼片。成片审查：重抽/重跑抽卡/自动检查修复，或通过完成")


@app.get("/api/juben")
def list_juben():
    """列出 juben/ 目录下的剧本文件名（供一键成片选择；不返回内容）。"""
    d = BASE / "juben"
    if not d.exists():
        return {"files": []}
    return {"files": sorted(f.name for f in d.glob("*.txt") if f.is_file())}


@app.post("/api/oneclick")
async def oneclick(
    idea: str = Form(""),
    style: str = Form("anime"),
    resolution: str = Form("竖屏 9:16"),
    seed: int = Form(-1),
    script_file: str = Form(""),
):
    """一键成片：一个输入框 + 一个按钮的极简入口，其余全自动。

    script_file：juben/ 目录下的剧本文件名（可选）。给了剧本就跳过 AI 编剧直接拆镜，
    剧本内容只在本地流转（读文件 → 本地模型），不打印、不落日志。
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

    width, height = RESOLUTIONS.get(resolution, (480, 832))
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)
    task_id = uuid.uuid4().hex[:12]
    _update(
        task_id, state="queued", msg="排队中…", mode="oneclick", model="wan",
        prompt=(script_file.strip() and f"剧本：{Path(script_file).name}") or idea[:80],
        seed=seed, resolution=resolution, duration="约1.5分钟",
        style=style,
        created=int(time.time()),
    )
    threading.Thread(
        target=_run_oneclick_task,
        args=(task_id, idea.strip(), style, width, height, 81, 20, seed, script_text),
        daemon=True,
    ).start()
    return {"task_id": task_id, "seed": seed}


# ---- 一键成片的人工审查端点（分镜意见改写 / 单镜重抽 / 换锚 / 成片检查修复 / 通过） ----

def _task_or_none(task_id: str):
    t = tasks.get(task_id)
    if not t and (OUTPUT / f"{task_id}.json").exists():
        try:
            t = json.loads((OUTPUT / f"{task_id}.json").read_text(encoding="utf-8"))
            tasks[task_id] = t
        except Exception:
            return None
    return t


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


@app.post("/api/review/storyboard")
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


@app.post("/api/review/resample")
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


@app.post("/api/review/fixcheck")
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


@app.post("/api/review/approve")
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


@app.post("/api/review/recard")
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

@app.post("/api/review/shot_next")
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


@app.post("/api/review/shot_resample")
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


@app.post("/api/review/shot_finish")
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


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {
        "comfy": comfy.is_ready(),
        "t2v_ready": comfy.t2v_ready(),
        "h3_ready": comfy.h3_ready(),
        "uncensored_ready": ai.uncensored_ready(),
    }


@app.get("/api/creative-profiles")
def get_creative_profiles():
    """读取场景库与人物库，供本机制片台复用。"""
    return _profiles()


@app.post("/api/creative-profiles")
async def save_creative_profiles(payload: dict):
    """保存制片资料；只接受白名单字段，避免把浏览器任意数据写入磁盘。"""
    try:
        return _save_profiles(payload)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"保存制片资料失败：{e}"}, 500)


@app.get("/api/tasks")
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


@app.post("/api/generate")
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
    # 无审查出片只用 Wan 2.2（只有它有无审查 LoRA）；H3 不支持无审查，强制回退 Wan。
    if nsfw_flag and model == "h3":
        model = "wan"
        if mode == "t2v" and not comfy.t2v_ready():
            return JSONResponse({"error": "无审查只支持 Wan 2.2；文生视频模型还在下载中"}, 503)

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


@app.post("/api/storyboard")
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


@app.post("/api/story-long")
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
    # 无审查出片只用 Wan 2.2（只有它有无审查 LoRA）；H3 不支持无审查，强制回退 Wan。
    if nsfw_flag and model == "h3":
        model = "wan"
        if not comfy.t2v_ready():
            return JSONResponse({"error": "无审查只支持 Wan 2.2；文生视频模型还在下载中"}, 503)

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


@app.post("/api/concat")
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


@app.get("/api/status/{task_id}")
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


@app.post("/api/cancel/{task_id}")
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


@app.get("/api/video/{task_id}")
def video(task_id: str):
    t = tasks.get(task_id)
    if t and t.get("state") == "done":
        return FileResponse(OUTPUT / t["video"], media_type="video/mp4")
    # 回退到磁盘：重启后内存清空，但视频文件仍在
    f = OUTPUT / f"{task_id}.mp4"
    if f.exists():
        return FileResponse(f, media_type="video/mp4")
    return JSONResponse({"error": "视频还没生成好"}, 404)


@app.get("/api/segment-video/{task_id}/{seg_index}")
def segment_video(task_id: str, seg_index: int):
    """审查用：返回单个镜头的段视频。"""
    f = OUTPUT / f"{task_id}_s{seg_index}.mp4"
    if f.exists():
        return FileResponse(f, media_type="video/mp4")
    return JSONResponse({"error": "段视频不存在"}, 404)


@app.get("/api/shot-preview/{task_id}/{shot_index}")
def shot_preview(task_id: str, shot_index: int):
    """分镜审查用：返回该镜的预览图（后台生成中则 404，前端降级显示占位）。"""
    f = OUTPUT / f"{task_id}_prev{shot_index}.png"
    if f.exists():
        return FileResponse(f, media_type="image/png")
    return JSONResponse({"error": "预览图还没生成"}, 404)
