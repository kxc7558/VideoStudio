# -*- coding: utf-8 -*-
"""AI 辅助：本地视觉模型（Ollama qwen2.5vl）看图 + 云端 DeepSeek / 本地 uncensored 写提示词。

文本生成用 `_writer(local)` 分派：普通出片走 DeepSeek（质量好）；无审查链路
local=True 时走本地 qwen3.8-uncensored（内容不外发，本地模型不可用就放弃改写）。
两个能力都做成「尽力而为」：失败时返回空串，由调用方回退到原提示词，绝不拖垮生成主流程。
"""
import base64
import os
from pathlib import Path

import httpx

# DeepSeek（云端文本，写叙事强）
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DS_MODEL = "deepseek-v4-flash"
# key 不在代码里写死：优先读 local_config.py（已 gitignore），否则读环境变量。
try:
    from local_config import DEEPSEEK_KEY
except ImportError:
    DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")

# Ollama（本地视觉）
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_GEN = "http://127.0.0.1:11434/api/generate"
VL_MODEL = "qwen2.5vl:7b"

# 本地无审查文本模型（写无审查/擦边剧情提示词，不走外部 API，不经过云端过滤）
UNCENSORED_MODEL = "qwen3.8-uncensored-fast:latest"


def uncensored_ready() -> bool:
    """本地无审查文本模型（Ollama）是否在线。"""
    try:
        r = httpx.get(OLLAMA_TAGS, timeout=3)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        return UNCENSORED_MODEL in names
    except Exception:
        return False


def uncensored_text(system: str, user: str) -> str:
    """用本地无审查模型写提示词；失败返回空串（调用方回退原提示词）。

    think=False 关思考：加速且避免输出带 reasoning 前缀冲淡结果。
    timeout 1800s：27B 冷启动（首次加载 14GB 进内存）可能 10+ 分钟。
    """
    try:
        payload = {
            "model": UNCENSORED_MODEL,
            "system": system,
            "prompt": user,
            "stream": False,
            "temperature": 0.7,
            "think": False,
            "options": {"num_predict": 2048},
        }
        r = httpx.post(OLLAMA_GEN, json=payload, timeout=1800)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception:
        return ""


def vision_ready() -> bool:
    """本地视觉模型（Ollama）是否在线。"""
    try:
        return httpx.get(OLLAMA_TAGS, timeout=3).status_code == 200
    except Exception:
        return False


def describe_image(image_path) -> str:
    """用本地 qwen2.5vl 描述一张图片，返回一句中文描述；失败返回空串。"""
    image_path = Path(image_path)
    if not image_path.exists():
        return ""
    try:
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        payload = {
            "model": VL_MODEL,
            "prompt": "用一句中文描述这张画面的内容（主体、动作、场景、光线），直接说，不要任何前缀。",
            "images": [b64],
            "stream": False,
        }
        r = httpx.post(OLLAMA_GEN, json=payload, timeout=180)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception:
        return ""


# MiniMax H3 官方提示词格式（源自官方 h3-prompt-writing skill，精简提炼）。
# 只用于 H3 模型；Wan 2.2 不吃这套，保持简短英文。
_H3_RULES = """用 MiniMax H3 官方视频提示词格式输出，正文全部英文。

【固定三段，严格按此顺序，段与段之间空一行】

第一行（对齐指令，仅 i2v / fl2v 写，t2v 不写）：
· i2v（首帧）：For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
· fl2v（首尾帧）：How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the S.SS-second mark of the target video.（S.SS 用给定的视频秒数，保留两位小数）

integrated_multimodal_description: 这是主体，逐镜头写画面。
overall_soundscape: 1~4 句英文概括全程环境音/动作音/非语言人声；全静音才写 N/A。
non_diegetic_music: 1~3 句英文描述只有观众能听见的背景音乐（乐器/速度/节奏/强弱变化）；没有就写 N/A。

【integrated_multimodal_description 写法】
· 以 [Shot 1] 开头，紧跟风格词 + 构图，例如 "Live-action, cinematic, a medium-wide shot frames ..."。风格词从 Cinematic / live-action / 2D-animated / 3D CG / claymation / watercolor 等里选。
· 逐镜头描述主体外貌/衣着/位置、场景、关键道具、动作与反应、镜头运动、光影；同一个人/物的外貌、衣服颜色、空间关系整段保持一致。
· 镜头运动写成自然英文动作句，别堆在句尾当标签。格式 = 运动类型 + 幅度 + 速度：类型用 Zoom In/Out、Push In/Pull Out、Pan Left/Right、Truck Left/Right、Tilt Up/Down、Pedestal Up/Down、Arc Shot、Tracking Shot、Static Shot、Shake Slightly/Strongly、POV；幅度用 with small/large amplitude，速度用 at slow/fast speed（按需加）。例：The camera pushes in with small amplitude at slow speed toward the folded letter.
· 多镜头：第一镜头不带时间戳；后续镜头写 [Shot N] At MM:SS.mmm, the camera cuts to...（切点严格递增且在视频时长内）。
· i2v：结构 = 首帧锚点 → 动作开始 → 持续发展 → 结果/反应，从首帧画面出发往前推。
· fl2v：倾向单镜头，结构 = 首帧状态 → 可观察的中间变化 → 逐步收窄差异 → 末帧状态，别只复述两张静图，要写出连接两帧的运动过程。"""


# 细节强化条款：治「画面糊、动态模糊、细节少」。H3 与 Wan 两条路都追加这段。
# 原理：视频模型只会画你写出来的东西——不点名纹理和光影，它就给一片平滑色块；
# 动作写得太猛，低步数采样跟不上就糊成一团，所以要求慢速、小幅度的运动。
_DETAIL_RULES = """【细节与清晰度要求（必须遵守）】
· 主体必须写出可见的材质纹理：布料织纹/磨损、皮肤与毛发质感、木石纹理、金属锈迹、纸张纤维等，至少点名 2 处具体纹理。
· 必须写光源方向与光质（侧逆光/顶光/低角度晨光；柔光或硬光），并写出它在主体上形成的高光和阴影落点。
· 加入 1~2 个细微动作（衣角轻摆、火星飘升、雾气缓慢流动、发丝被风吹动），让画面有活气但不剧烈。
· 动态要「慢」：镜头运动一律用 with small amplitude at slow speed；不要快速摇镜、不要剧烈晃动、不要瞬间转向——快动作在本地模型上会糊成一团。
· 主体动作幅度也要克制：走路而非奔跑，缓慢转身而非急转，抬手而非挥砍（战斗场面用慢镜头感描述）。
· 画质词写在风格词后面：sharp focus, fine detail, crisp texture, shallow depth of field。
· 禁止出现 motion blur、fast motion、rapid、frantic、chaotic 等会诱发模糊的词。"""


def _deepseek(system: str, user: str) -> str:
    """调用 DeepSeek API 写高质量视频提示词。"""
    if not DEEPSEEK_KEY:
        raise RuntimeError("DEEPSEEK_KEY not configured")
    r = httpx.post(
        DEEPSEEK_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DS_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def h3_prompt(plain: str, mode: str = "t2v", seconds: float = 5.0) -> str:
    """把普通提示词改写成 MiniMax H3 官方格式；失败返回空串（调用方回退原提示词）。"""
    try:
        return _deepseek(
            "你是资深视频提示词工程师，精通 MiniMax H3 官方提示词格式。\n\n"
            + _H3_RULES + "\n\n" + _DETAIL_RULES,
            f"要生成的模式是 {mode}（t2v=文生 / i2v=首帧图生 / fl2v=首尾帧），视频时长约 {seconds:.2f} 秒。\n"
            f"用户原始想法：「{plain}」。\n"
            "请严格按上述官方格式输出完整的 H3 提示词。只输出提示词本身，不要解释、不要引号。",
        )
    except Exception:
        return ""


def _writer(local: bool):
    """返回文本生成函数。local=True 时只允许本地 uncensored——本地模型不可用就
    返回空串（调用方回退原提示词），绝不悄悄回落云端：无审查内容不允许外发。"""
    if not local:
        return _deepseek
    if uncensored_ready():
        return uncensored_text
    return lambda _system, _user: ""


def bridge_next_prompt(prev_desc: str, next_goal: str, model: str = "wan", seconds: float = 5.0, local: bool = False) -> str:
    """根据上一段实际结尾画面，重写下一段的视频提示词；失败返回空串。

    model="h3" 时输出 MiniMax H3 官方格式（i2v 首帧对齐指令 + 三段）；否则输出简短英文。
    local=True 且本地 uncensored 就绪时走本地（无审查出片用，不碰云端）。
    """
    writer = _writer(local)
    try:
        if model == "h3":
            return writer(
                "你是资深视频导演，精通 MiniMax H3 官方提示词格式。\n\n"
                + _H3_RULES + "\n\n" + _DETAIL_RULES,
                f"这是连续视频的下一段，模式是 i2v（首帧 = 上一段结尾画面），视频时长约 {seconds:.2f} 秒。\n"
                f"上一段实际结束的画面是：「{prev_desc}」。\n"
                f"下一段镜头的剧情目标是：「{next_goal}」。\n"
                "请严格按官方格式输出完整 H3 提示词（i2v 首帧对齐指令 + 三段），"
                "让画面从上一段实际结尾自然过渡到下一段目标，主体一致、动作连贯；"
                "镜头运动要承接上一段的惯性——上一段若在移动，本段开头继续该方向或自然减速停下，不要突然静止或换向。只输出提示词本身。",
            )
        return writer(
            "你是资深视频导演，负责让多个镜头自然接续。\n\n" + _DETAIL_RULES,
            f"上一段视频实际结束的画面是：「{prev_desc}」。\n"
            f"下一段镜头的剧情目标是：「{next_goal}」。\n"
            "请写 2~4 句英文视频生成提示词，让画面从上一段实际结尾自然过渡到下一段剧情目标，"
            "主体一致、动作连贯；镜头运动承接上一段惯性，一律小幅度慢速。"
            "必须按上述细节要求写出材质纹理、光源方向与光质、1~2 个细微动作，并以 "
            "sharp focus, fine detail, crisp texture 结尾。只输出提示词本身，不要解释、不要引号。",
        )
    except Exception:
        return ""


def transition_prompt(first_desc: str, last_desc: str, base_prompt: str, seconds: float = 5.0, local: bool = False) -> str:
    """给「指定首尾帧」写过渡提示词（H3 FL2VA 官方格式）；失败返回空串。"""
    writer = _writer(local)
    try:
        return writer(
            "你是资深视频导演，精通 MiniMax H3 官方提示词格式。\n\n"
            + _H3_RULES + "\n\n" + _DETAIL_RULES,
            f"这是首尾帧过渡（fl2v），视频时长约 {seconds:.2f} 秒。\n"
            f"开头画面是：「{first_desc}」。\n"
            f"结尾画面是：「{last_desc}」。\n"
            f"用户对中间过程的想法是：「{base_prompt}」。\n"
            "请严格按官方格式输出完整 H3 提示词（fl2v 对齐指令 + 三段），"
            "描述从开头到结尾的连续过渡。只输出提示词本身。",
        )
    except Exception:
        return ""
