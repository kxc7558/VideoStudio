# -*- coding: utf-8 -*-
"""无审查出片管道（内容零接触）：
提示词文件 → 本地 qwen3.8 写成人故事 → 本地拆剧本 → nsfw 连续成片。

约束：全程不打印任何文本内容（提示词/故事/分镜），日志只输出字数与状态。
文本只在本地流转：桌面提示词文件 → Ollama 27B → output 下的数据文件 → 出片引擎。
"""
import functools
import json
import random
import sys
import threading
import time
import uuid
from pathlib import Path

print = functools.partial(print, flush=True)
import httpx

BASE = Path(r"D:\VideoStudio")
sys.path.insert(0, str(BASE))
PROMPT_FILE = Path(r"C:\Users\32492\Desktop\tishici.txt")
STORY_FILE = BASE / "output" / "_nsfw_story.txt"
SHOTS_FILE = BASE / "output" / "_nsfw_shots.json"
N_SHOTS = 4          # 分镜数（每镜 5 秒）
WIDTH, HEIGHT = 480, 832   # 竖屏 9:16（短剧规格）
LENGTH = 81          # 每镜 5 秒（Wan 4n+1）

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

# ---- 阶段 1：本地 qwen3.8 依据角色设定写故事（提示词原文直传，不改一字） ----
if STORY_FILE.exists():
    story = STORY_FILE.read_text(encoding="utf-8")
    log(f"[1/3] 复用已有故事（{len(story)} 字）")
else:
    text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    payload = {
        "model": "qwen3.8-uncensored-fast:latest",
        "system": "根据用户给出的角色设定创作一个完整连贯的成人故事，中文，1500 字以上。直接输出故事正文，不要任何解释或元评论。",
        "prompt": text,          # 提示词原文原样进模型
        "stream": False,
        "think": False,
        "temperature": 0.8,
        "options": {"num_predict": 4096},
    }
    log("[1/3] 本地 qwen3.8 正在写故事（27B，冷启动可能要几分钟）…")
    r = httpx.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=1800)
    r.raise_for_status()
    story = r.json().get("response", "").strip()
    STORY_FILE.write_text(story, encoding="utf-8")
    log(f"[1/3] 故事完成（{len(story)} 字），已存盘")

# ---- 阶段 2：本地拆剧本（无审查模型，不走云端） ----
if SHOTS_FILE.exists():
    shots = json.loads(SHOTS_FILE.read_text(encoding="utf-8"))
    log(f"[2/3] 复用已有分镜（{len(shots)} 镜）")
else:
    import storyboard
    log("[2/3] 本地拆剧本中（约 3-8 分钟）…")
    shots = storyboard.split_story(story, N_SHOTS, local=True)
    SHOTS_FILE.write_text(json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2/3] 拆出 {len(shots)} 镜")

prompts = [s["prompt"].strip() for s in shots if str(s.get("prompt", "")).strip()]
if len(prompts) < 2:
    log("[FAIL] 分镜不足 2 个，无法连续成片")
    sys.exit(1)

# ---- 阶段 3：nsfw 连续成片（Wan2.2 + 无审查 LoRA，接续重写也走本地模型） ----
import app as vs
import comfy

if not comfy.is_ready():
    log("[FAIL] ComfyUI 未启动")
    sys.exit(1)

task_id = uuid.uuid4().hex[:12]
segs = [{"mode": "t2v", "image": None, "prompt": prompts[0]}]
for p in prompts[1:]:
    segs.append({"mode": "i2v", "image": None, "prompt": p})
seed = random.randint(1, 2**31 - 1)
vs._update(
    task_id, state="queued", msg="排队中…", mode="story", model="wan",
    prompt="(本地无审查管道，内容见 _nsfw_shots.json)", seed=seed,
    resolution="竖屏 9:16", duration="长 · 约5秒",
    created=int(time.time()),
)
# 非守护线程：脚本进程陪跑到生成结束，防止中途退出
t = threading.Thread(
    target=vs._run_long_task,
    args=(task_id, "wan", segs, WIDTH, HEIGHT, LENGTH, 20, seed, True),
    kwargs={"nsfw": True},
    daemon=False,
)
t.start()
log(f"[3/3] 已提交连续成片任务 {task_id}（{len(prompts)} 镜 × 5 秒，竖屏 480×832）")
t.join()
meta = vs.tasks.get(task_id, {})
log(f"[DONE] 任务 {task_id} 状态：{meta.get('state')}  成片：output/{task_id}.mp4")
