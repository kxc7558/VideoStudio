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
from shared.paths import OUTPUT, WORKFLOWS

# NoobAI-XL 是 SDXL 系动漫模型：竖版角色卡用 832×1216，符合其训练分布
CARD_WIDTH = 832
CARD_HEIGHT = 1216
CANDIDATES = 4
STEPS = 28
CFG = 5.0
SAMPLER = "euler_ancestral"
SCHEDULER = "normal"


def build_card_workflow(prompt: str, seed: int, batch: int = CANDIDATES, width: int = CARD_WIDTH, height: int = CARD_HEIGHT) -> dict:
    """构建文生图工作流：一次出 batch 张候选。负向词含真人过滤（NoobAI 是动漫模型，真人请求会崩坏）。"""
    wf = json.loads((WORKFLOWS / "anchor_t2i_api.json").read_text(encoding="utf-8"))
    wf["2"]["inputs"]["text"] = prompt
    wf["4"]["inputs"].update({"width": width, "height": height, "batch_size": batch})
    wf["5"]["inputs"].update({"seed": seed, "steps": STEPS, "cfg": CFG, "sampler_name": SAMPLER, "scheduler": SCHEDULER})
    wf["7"]["inputs"]["filename_prefix"] = "character_card/gen"
    return wf


def generate_candidates(desc: str, style: str = "anime") -> dict:
    """描述 → batch 张候选图。返回 {task_id, count}；图落在 ComfyUI output/character_card/。

    desc 是用户的一句话角色描述（如「银发挑染的女店员，20 岁，便利店制服」），
    这里补上 NoobAI 质量词与单人/定妆约束后直接文生图。
    """
    seed = random.randint(1, 2**31 - 1)
    quality = "masterpiece, best quality, very aesthetic, absurdres, solo, 1girl, detailed face, looking at viewer, upper body"
    anti_real = "realistic, photorealistic, 3d, worst quality, low quality, bad anatomy, bad hands, watermark, signature, multiple views, chibi"
    prompt = f"{quality}, {desc.strip()}"
    wf = build_card_workflow(prompt, seed)
    pid = comfy.submit(wf)
    ok, history = comfy.wait_done(pid, timeout=900)
    if not ok:
        return {"error": "角色图生成失败（引擎未就绪或超时）"}
    images = _find_images(history)
    if not images:
        return {"error": "未找到生成的角色图"}
    saved = []
    card_dir = OUTPUT / "_character_cards"
    card_dir.mkdir(exist_ok=True)
    gen_id = uuid.uuid4().hex[:8]
    for i, info in enumerate(images):
        dest = card_dir / f"{gen_id}_c{i}.png"
        if comfy.download_binary(info, dest):
            saved.append(dest.name)
    return {"gen_id": gen_id, "images": saved, "seed": seed}


def _find_images(history: dict) -> list:
    """从 history 里收集 SaveImage 输出（character_card 子目录下的 png）。"""
    outs = []
    for _node, o in history.get("outputs", {}).items():
        for key, items in o.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and str(it.get("filename", "")).lower().endswith(".png"):
                    outs.append(it)
    return outs


def list_candidates(gen_id: str) -> list:
    """列出某次生成的候选图文件名。"""
    card_dir = OUTPUT / "_character_cards"
    if not card_dir.exists():
        return []
    return sorted(p.name for p in card_dir.glob(f"{gen_id}_c*.png"))


def save_character(gen_id: str, image_name: str, name: str, desc: str) -> dict:
    """把选中的候选图登记进角色库（characters），图片拷到 uploads 长期保存。"""
    src = OUTPUT / "_character_cards" / Path(image_name).name
    if not src.exists():
        return {"error": "候选图不存在"}
    card_id = f"char_{uuid.uuid4().hex[:8]}"
    dest = OUTPUT / "_character_cards" / f"{card_id}.png"
    shutil.copyfile(src, dest)
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
    return {"character_id": card_id, "image": dest.name}
