# -*- coding: utf-8 -*-
"""故事 → 剧本/分镜：调用 DeepSeek 把一段故事拆成连续分镜。"""
import json
import os
import re

import httpx

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"

# API key 不写死在代码里：优先读本地 local_config.py（已 gitignore），否则读环境变量。
# 换 key 只改 local_config.py，不动代码；也避免把密钥提交到 GitHub。
# 注意：不能用 `import secrets`（那会命中标准库 secrets，读不到 key）。
try:
    from local_config import DEEPSEEK_KEY  # noqa: F401
except ImportError:
    DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")

SYSTEM = (
    "你是资深影视分镜师。把用户给的故事拆成连续分镜脚本。"
    "严格只输出 JSON，格式："
    '{"shots":[{"id":1,"scene":"场景简述(中文)",'
    '"prompt":"视频生成用画面提示词(英文,描述主体/动作/镜头运动/光影)",'
    '"narration":"旁白或台词(中文)","duration":秒数}]}。'
    "分镜数量约 %d 个。\n"
    "每条 prompt 必须遵循 Wan2.2 官方提示词结构（主体/动作/环境/镜头五句式，全英文，60~90 词）：\n"
    "1. 风格定调 + 主体及其动作（Who does what）\n"
    "2. 主体的朝向、表情或衣着细节\n"
    "3. 背景环境分层描述\n"
    "4. 主体姿态与 1~2 个细微动作（衣角、发丝、呼吸）\n"
    "5. 景别与氛围收尾（close-up shot / wide shot / slow push in）\n"
    "硬性规则：动作一律小幅度慢速（slow, gentle motion），禁止 fast motion、rapid、"
    "frantic 等剧烈词；点名 1~2 处材质纹理（fabric texture, skin, hair strands）；"
    "以 sharp focus, fine detail, crisp texture 结尾。"
)


def _extract_json(text: str) -> str:
    """从模型返回里抠出 JSON 部分（容错：模型可能加前后缀、代码块或思考段）。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # 本地思考型模型可能带思考段
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 逐个候选起点尝试解析：防止思考段里出现花括号把起点带偏
    for m in re.finditer(r"\{", text):
        candidate = text[m.start():]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    # 兜底：首尾花括号截取（旧行为）
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text


def _uncensored_json(system: str, user: str) -> str:
    """无审查拆剧本：走本地 Ollama uncensored 模型，不碰云端 DeepSeek。

    强制关思考（think=False）：qwen 家族默认带 reasoning，开着会极慢且输出带思考前缀
    破坏 JSON 解析。关掉后响应快、直接给 JSON。
    模型冷启动（首次加载 14GB 进内存）可能超过 10 分钟，timeout 给足 1800s。
    """
    payload = {
        "model": "qwen3.8-uncensored-fast:latest",
        "system": system,
        "prompt": user,
        "stream": False,
        "temperature": 0.7,
        "think": False,
        "options": {"num_predict": 4096},
    }
    try:
        r = httpx.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=1800)
        r.raise_for_status()
    except httpx.ConnectError as e:
        raise RuntimeError("本地 Ollama 没启动（11434 端口连不上），无法本地拆剧本") from e
    except httpx.ReadTimeout as e:
        raise RuntimeError("本地模型响应超时（27B 冷启动可能要 10+ 分钟，再试一次应该就热了）") from e
    return r.json().get("response", "").strip()


def split_story(story: str, n_shots: int = 6, creative_brief: str = "", local: bool = False) -> list:
    """把故事拆成分镜列表 [{id, scene, prompt, narration, duration}]。

    local=True 且本地 uncensored 模型可用时，改用本地拆剧本（无审查出片用，不碰云端）。
    抛异常时由调用方兜底；正常返回 list（可能为空）。
    """
    system = (SYSTEM % n_shots) + creative_brief
    if local:
        raw = _uncensored_json(system, story)
    else:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
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
        raw = r.json()["choices"][0]["message"]["content"]
    obj = json.loads(_extract_json(raw))
    shots = obj.get("shots", [])
    # 归一化 id，确保是递增整数
    for i, s in enumerate(shots, 1):
        s["id"] = i
        s.setdefault("duration", 5)
    return shots
