# -*- coding: utf-8 -*-
"""工作流构建（业务层）：LoRA 链注入与 Wan/H3 workflow 组装。规则在此，不碰文件存取。"""
import json
from pathlib import Path

from shared.paths import WORKFLOWS

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

# H3 无审查 LoRA（SexGod1979/NaughtyTimes-MiniMax-H3，rank64）。
# 作者说明：在**未剪枝** FL2VA 底模上训练（含 adaln modulation），挂剪枝底模效果大打折扣，
# 所以无审查 H3 固定用未剪枝底模 `minimax_h3_fl2va-Q4_K_M.gguf`（leejet 转档）。
# 强度 1.0，支持 t2v 和 i2v（50/50 训练配比）。
H3_NSFW_LORA = "NaughtyTimes_v3_rank64_unpruned.safetensors"
H3_NSFW_BASE = "minimax_h3_fl2va-Q4_K_M.gguf"


# i2v 模型变体：distill=LightX2V 蒸馏（快）/ original=原版 20 步（质量）
MODEL_VARIANT = {"i2v": "distill"}



# ---- 生成业务常量 ----
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

def _apply_wan_nsfw_lora(wf, high_node="8", low_node="9", anime=False, enhancer=False, skip_uncensored=False):
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
    if not skip_uncensored:
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
    # i2v 有两个 workflow：蒸馏 4/6 步（快）与原版 20 步（质量模式 model_variant="original"）
    variant = MODEL_VARIANT.get("i2v", "distill")
    wf_name = f"{mode}_orig" if (mode == "i2v" and variant == "original") else mode
    wf = json.loads((WORKFLOWS / f"{wf_name}_api.json").read_text(encoding="utf-8"))
    prefix = f"video/{task_id}"
    anime = use_lora and style == "anime"
    if anime:
        prompt = f"{ANIME_LORA['trigger']}, {prompt}"
    if mode == "i2v":
        if use_lora and variant != "original":
            wf = _apply_wan_nsfw_lora(wf, anime=anime)
        elif use_lora and variant == "original":
            # 原版模型：只挂动漫 LoRA（蒸馏/无审查 LoRA 是按蒸馏模型训练的，不挂）
            wf = _apply_wan_nsfw_lora(wf, anime=anime, skip_uncensored=True)
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


def _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, last_frame_name=None, use_lora=False, ref_image_name=None, ref_strength=None, ref_image_names=None):
    """MiniMax H3 工作流，支持三种模式：

    - t2v：纯文生视频
    - i2v：首帧图生（可选 last_frame_name 做首尾帧过渡）
    - ref2va：**参考图全程注意力**——参考图在每一帧都参与注意力，角色/场景不漂移
      （康波产线验证过的角色一致性最强方案）

    use_lora=True（无审查链路）时：DiT 换未剪枝 Q4_K_M GGUF + 注入 NaughtyTimes LoRA。
    BasicGuider 的节点号随 mode 不同（t2v=6 / ref2va=10），故动态定位。
    """
    # ref2va 单文件已指向 pruned 主力模型；i2v/t2v 用官方 fl2va 版
    wf_name = f"h3_{mode}_api.json"
    wf = json.loads((WORKFLOWS / wf_name).read_text(encoding="utf-8"))

    # 动态定位 BasicGuider（LoRA 要插在它上游）。
    # ref2va 的 base 是专属的 ref2va 模型，不能被换成未剪枝 FL2VA，故 LoRA 注入仅限 t2v/i2v。
    guider_id = next((k for k, v in wf.items() if v.get("class_type") == "BasicGuider"), None)
    if use_lora and guider_id and mode != "ref2va":
        wf["1"]["inputs"]["model_name"] = H3_NSFW_BASE
        wf["300"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": wf[guider_id]["inputs"]["model"],  # 上游 = H3ModelLoaderAny
                "lora_name": H3_NSFW_LORA,
                "strength_model": 1.0,
            },
        }
        wf[guider_id]["inputs"]["model"] = ["300", 0]

    # 按 class_type 动态定位参数节点（各 mode 节点号不同：t2v/i2v 是 4/5/8/12，
    # ref2va 是 6/7/9/15）——硬编码节点号必踩坑
    def _find(cls: str):
        return next((k for k, v in wf.items() if v.get("class_type") == cls), None)

    prompt_id = "6" if mode == "ref2va" else "4"
    wf[prompt_id]["inputs"].update({"prompt": prompt, "width": width, "height": height, "length": length})
    noise_id, sched_id, save_id = _find("RandomNoise"), _find("BasicScheduler"), _find("SaveVideo")
    if noise_id:
        wf[noise_id]["inputs"]["noise_seed"] = seed
    if sched_id:
        wf[sched_id]["inputs"]["steps"] = steps
    if save_id:
        wf[save_id]["inputs"]["filename_prefix"] = f"video/{task_id}"

    if mode == "ref2va":
        # 参考图（支持多张：ref_images.ref_image_N 是动态增长输入）
        # 多张参考图能同时锁定多个角色/场景，一致性显著强于单张
        refs = ref_image_names if isinstance(ref_image_names, list) else ([ref_image_names] if (ref_image_names or image_name) else [])
        refs = [r for r in refs if r]
        if refs:
            wf["5"]["inputs"]["image"] = refs[0]
            wf["6"]["inputs"]["ref_images.ref_image_0"] = ["5", 0]
            for i, name in enumerate(refs[1:], start=1):
                nid = str(20 + i)  # 21, 22, …（避开原工作流节点号）
                wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
                wf["6"]["inputs"][f"ref_images.ref_image_{i}"] = [nid, 0]
        if ref_strength and "ref_image_size" in wf["6"]["inputs"]:
            wf["6"]["inputs"]["ref_image_size"] = ref_strength
    elif mode == "i2v":
        wf["0"]["inputs"]["image"] = image_name
        if last_frame_name:
            wf["13"] = {"class_type": "LoadImage", "inputs": {"image": last_frame_name}}
            wf["4"]["inputs"]["last_frame"] = ["13", 0]
    return wf


def _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps=20, last_frame_name=None, use_lora=True, style="real", ref_image_name=None, ref_image_names=None):
    if model == "h3":
        return _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps,
                                  last_frame_name, use_lora=use_lora, ref_image_name=ref_image_name,
                                  ref_image_names=ref_image_names)
    return _build_wan_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps, use_lora, style)


