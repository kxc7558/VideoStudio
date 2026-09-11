# -*- coding: utf-8 -*-
"""角色卡生成（业务层）：描述 → NoobAI 文生图出候选 → 选定后存角色库。

三来源之一（另两个：不填走抽卡 / 直接上传图）。工作流复用 anchor_t2i_api.json。
"""
import json
import random
import shutil
import uuid
from pathlib import Path

import comfy
from db import profiles_store
from shared.paths import OUTPUT, UPLOADS, WORKFLOWS

# NoobAI-XL 是 SDXL 系动漫模型：竖版角色卡用 832×1216，符合其训练分布
CARD_WIDTH = 832
CARD_HEIGHT = 1216
CANDIDATES = 4
STEPS = 28
CFG = 5.0
SAMPLER = "euler_ancestral"
SCHEDULER = "normal"
CARD_DIR = OUTPUT / "_character_cards"

_QUALITY = "masterpiece, best quality, very aesthetic, absurdres, solo, 1girl, detailed face, looking at viewer, upper body"
_ANTI_REAL = "realistic, photorealistic, 3d, worst quality, low quality, bad anatomy, bad hands, watermark, signature, multiple views, chibi"


def build_card_workflow(prompt: str, seed: int, batch: int = CANDIDATES, width: int = CARD_WIDTH, height: int = CARD_HEIGHT) -> dict:
    """构建文生图工作流：一次出 batch 张候选。"""
    wf = json.loads((WORKFLOWS / "anchor_t2i_api.json").read_text(encoding="utf-8"))
    wf["2"]["inputs"]["text"] = prompt
    wf["3"]["inputs"]["text"] = _ANTI_REAL
    wf["4"]["inputs"].update({"width": width, "height": height, "batch_size": batch})
    wf["5"]["inputs"].update({"seed": seed, "steps": STEPS, "cfg": CFG, "sampler_name": SAMPLER, "scheduler": SCHEDULER})
    wf["7"]["inputs"]["filename_prefix"] = "character_card/gen"
    return wf


def build_card_workflow_from_desc(desc: str, style: str = "anime") -> dict:
    """用户角色描述 → 成品工作流。补质量词与单人/定妆约束；真人描述时去 1girl 换通用词。"""
    seed = random.randint(1, 2**31 - 1)
    # 用户描述里带了性别/人数（1boy/2girls 等）时去掉默认 1girl，避免冲突
    quality = _QUALITY
    lowered = desc.lower()
    if any(t in lowered for t in ("1boy", "2girls", "1girl", "male", "man", "boy")):
        quality = quality.replace("1girl, ", "")
    return build_card_workflow(f"{quality}, {desc.strip()}", seed)


def generate_candidates(desc: str, style: str = "anime") -> dict:
    """同步版：描述 → batch 张候选图并落盘（供脚本调用；Web 端走 generate+collect 两步）。"""
    wf = build_card_workflow_from_desc(desc, style)
    pid = comfy.submit(wf)
    ok, history = comfy.wait_done(pid, timeout=900)
    if not ok:
        return {"error": "角色图生成失败（引擎未就绪或超时）"}
    images = collect_images(history)
    if not images:
        return {"error": "未找到生成的角色图"}
    saved = store_candidates(pid.replace("-", "")[:12], images)
    return {"gen_id": pid, "images": [p.name for p in saved]}


def collect_images(history: dict) -> list:
    """从 history 里收集 SaveImage 输出（character_card 子目录下的 png）。"""
    outs = []
    for _node, o in (history or {}).get("outputs", {}).items():
        for key, items in o.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and str(it.get("filename", "")).lower().endswith(".png"):
                    outs.append(it)
    return outs


def store_candidates(prompt_id: str, images: list) -> list:
    """把 ComfyUI 产出的候选图下载落盘到 output/_character_cards/，返回本地路径列表。"""
    CARD_DIR.mkdir(exist_ok=True)
    gen_id = "".join(ch for ch in prompt_id if ch.isalnum())[:12]
    saved = []
    for i, info in enumerate(images):
        dest = CARD_DIR / f"{gen_id}_c{i}.png"
        if comfy.download_binary(info, dest) and dest not in saved:
            saved.append(dest)
    return saved


def list_candidates(gen_id: str) -> list:
    """列出某次生成的候选图文件名。"""
    if not CARD_DIR.exists():
        return []
    return sorted(p.name for p in CARD_DIR.glob(f"{gen_id}_c*.png"))


def save_character_image(image_name: str, name: str, desc: str) -> dict:
    """把选中的候选图登记进角色库（characters）。image 是 _character_cards 下的文件名。"""
    src = CARD_DIR / Path(image_name).name
    if not src.exists():
        return {"error": "候选图不存在"}
    card_id = f"char_{uuid.uuid4().hex[:8]}"
    dest = CARD_DIR / f"{card_id}.png"
    shutil.copyfile(src, dest)
    _register(card_id, name, desc)
    return {"character_id": card_id, "image": dest.name, "name": (name or "未命名角色").strip()}


def save_uploaded_character(raw: bytes, ext: str) -> dict:
    """上传的角色图 → 存角色库。返回 character_id 供一键成片引用。"""
    CARD_DIR.mkdir(exist_ok=True)
    card_id = f"char_{uuid.uuid4().hex[:8]}"
    dest = CARD_DIR / f"{card_id}{ext}"
    dest.write_bytes(raw)
    _register(card_id, "", "")
    return {"character_id": card_id, "image": dest.name}


def _register(card_id: str, name: str, desc: str) -> None:
    """把角色登记进 profiles_store 的 characters 列表（图片与描述分离存储）。"""
    data = profiles_store.profiles()
    characters = data.get("characters", [])
    characters.append({
        "id": card_id,
        "name": (name or "未命名角色").strip()[:40],
        "identity": "",
        "appearance": (desc or "").strip()[:800],
        "wardrobe": "",
        "behavior": "",
    })
    profiles_store.save_profiles({"characters": characters, "scenes": data.get("scenes", [])})
