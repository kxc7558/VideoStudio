# -*- coding: utf-8 -*-
"""AI 辅助：本地视觉模型（Ollama qwen2.5vl）看图 + DeepSeek 写叙事提示词。

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


def _deepseek(system: str, user: str) -> str:
    r = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        json={
            "model": DS_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def bridge_next_prompt(prev_desc: str, next_goal: str) -> str:
    """根据上一段实际结尾画面，重写下一段的视频提示词；失败返回空串。"""
    try:
        return _deepseek(
            "你是资深视频导演，负责让多个镜头自然接续。",
            f"上一段视频实际结束的画面是：「{prev_desc}」。\n"
            f"下一段镜头的剧情目标是：「{next_goal}」。\n"
            "请写一句英文视频生成提示词，让画面从上一段实际结尾自然过渡到下一段剧情目标，"
            "主体一致、动作连贯、镜头运动自然。只输出提示词本身，不要解释、不要引号。",
        )
    except Exception:
        return ""


def transition_prompt(first_desc: str, last_desc: str, base_prompt: str) -> str:
    """给「指定首尾帧」写过渡提示词；失败返回空串。"""
    try:
        return _deepseek(
            "你是资深视频导演，擅长设计首尾帧过渡。",
            f"开头画面是：「{first_desc}」。\n"
            f"结尾画面是：「{last_desc}」。\n"
            f"用户对中间过程的想法是：「{base_prompt}」。\n"
            "请写一句英文视频生成提示词，描述从开头到结尾的过渡过程，"
            "动作连贯、镜头运动自然、画面流畅。只输出提示词本身，不要解释、不要引号。",
        )
    except Exception:
        return ""
