# -*- coding: utf-8 -*-
"""故事 → 剧本/分镜：调用 DeepSeek 把一段故事拆成连续分镜。"""
import json
import os
import re

import httpx

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"

# API key 不写死在代码里：优先读本地 secrets.py（已 gitignore），否则读环境变量。
# 换 key 只改 secrets.py，不动代码；也避免把密钥提交到 GitHub。
try:
    from secrets import DEEPSEEK_KEY  # noqa: F401
except ImportError:
    DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")

SYSTEM = (
    "你是资深影视分镜师。把用户给的故事拆成连续分镜脚本。"
    "严格只输出 JSON，格式："
    '{"shots":[{"id":1,"scene":"场景简述(中文)",'
    '"prompt":"视频生成用画面提示词(英文,描述主体/动作/镜头运动/光影)",'
    '"narration":"旁白或台词(中文)","duration":秒数}]}。'
    "分镜数量约 %d 个。prompt 要具体、适合直接喂给视频生成模型。"
)


def _extract_json(text: str) -> str:
    """从模型返回里抠出 JSON 部分（容错：模型可能加前后缀或代码块）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text


def split_story(story: str, n_shots: int = 6) -> list:
    """把故事拆成分镜列表 [{id, scene, prompt, narration, duration}]。

    抛异常时由调用方兜底；正常返回 list（可能为空）。
    """
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM % n_shots},
            {"role": "user", "content": story},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.7,
    }
    r = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        json=payload,
        timeout=180,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    obj = json.loads(_extract_json(content))
    shots = obj.get("shots", [])
    # 归一化 id，确保是递增整数
    for i, s in enumerate(shots, 1):
        s["id"] = i
        s.setdefault("duration", 5)
    return shots
