# -*- coding: utf-8 -*-
"""无审查长片管道（3 分钟+，内容零接触）：
提示词文件 → 本地 qwen3.8 写长故事 → 分 6 批本地拆 48 镜 → nsfw 连续成片（段间本地接戏）。

约束：全程不打印任何文本内容，日志只有字数/进度/状态。
可断点续跑：story.txt / shots_batch*.json / shots_all.json 存在即复用。
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
WORK = BASE / "output" / "_nsfw_long"
WORK.mkdir(exist_ok=True)
PROMPT_FILE = Path(r"C:\Users\32492\Desktop\tishici.txt")
STORY_FILE = WORK / "story.txt"
SHOTS_ALL = WORK / "shots_all.json"

N_BATCHES = 6
SHOTS_PER_BATCH = 8          # 6×8=48 镜 → 净长 ≈ 200s ≈ 3min20s
TARGET_SHOTS = 45            # 净长 ≥185s 的最低镜数（含拼接损耗）
WIDTH, HEIGHT = 480, 832     # 竖屏 9:16
LENGTH = 81                  # 每镜 81 帧 ≈ 5.06s @16fps
SEG_SECONDS = LENGTH / 16.0
XFADE = 0.9


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def net_duration(n: int) -> float:
    return n * SEG_SECONDS - XFADE * max(0, n - 1)


# ---- 阶段 1：本地写长故事（提示词原文直传，不读不改） ----
if STORY_FILE.exists():
    story = STORY_FILE.read_text(encoding="utf-8")
    log(f"[1/3] 复用已有长故事（{len(story)} 字）")
else:
    text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    payload = {
        "model": "qwen3.8-uncensored-fast:latest",
        "system": "根据用户给出的角色设定创作一个完整连贯的长篇成人故事，中文，3000 字以上，"
                  "情节分多个场景推进、有起承转合。直接输出故事正文，不要任何解释或元评论。",
        "prompt": text,
        "stream": False,
        "think": False,
        "temperature": 0.8,
        "options": {"num_predict": 8192},
    }
    log("[1/3] 本地 qwen3.8 正在写长故事（约 5-15 分钟）…")
    r = httpx.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=1800)
    r.raise_for_status()
    story = r.json().get("response", "").strip()
    STORY_FILE.write_text(story, encoding="utf-8")
    log(f"[1/3] 长故事完成（{len(story)} 字），已存盘")

# ---- 阶段 2：分批本地拆剧本（每批 8 镜，带进度与上批衔接） ----
import storyboard


def split_batch(batch_idx: int, prev_scene: str) -> list:
    brief = (
        f"\n\n这是把长篇故事拆成分镜的第 {batch_idx + 1}/{N_BATCHES} 批（每批 {SHOTS_PER_BATCH} 个镜头）。"
        f"本批覆盖故事进度约 {batch_idx * 100 // N_BATCHES}%~{(batch_idx + 1) * 100 // N_BATCHES}% 的段落，"
        f"从该处剧情继续往下拆，不要回头重复前面的内容。"
    )
    if prev_scene:
        brief += f"\n上一镜（第 {batch_idx * SHOTS_PER_BATCH} 镜）剧情是：「{prev_scene[:200]}」。本批第一镜要自然承接它。"
    shots = None
    for attempt in (1, 2):  # 失败重试一次
        try:
            shots = storyboard.split_story(story, SHOTS_PER_BATCH, brief, local=True)
            break
        except Exception as e:  # noqa: BLE001
            log(f"    批 {batch_idx + 1} 第 {attempt} 次拆解失败：{type(e).__name__}，重试…")
            time.sleep(5)
    return shots or []


if SHOTS_ALL.exists():
    shots_all = json.loads(SHOTS_ALL.read_text(encoding="utf-8"))
    log(f"[2/3] 复用已有分镜（{len(shots_all)} 镜）")
else:
    shots_all = []
    for b in range(N_BATCHES):
        prev_scene = shots_all[-1].get("scene", "") if shots_all else ""
        log(f"[2/3] 拆第 {b + 1}/{N_BATCHES} 批（累计 {len(shots_all)} 镜）…")
        batch = split_batch(b, prev_scene)
        for s in batch:
            s["id"] = len(shots_all) + 1  # 全局重编号
            shots_all.append(s)
        (WORK / f"shots_batch{b + 1}.json").write_text(
            json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"    批 {b + 1} 完成（+{len(batch)} 镜，累计 {len(shots_all)}）")
    # 镜数不够 3 分钟就补批（同一机制，续写后续剧情）
    extra = 0
    while len(shots_all) < TARGET_SHOTS and extra < 4:
        extra += 1
        prev_scene = shots_all[-1].get("scene", "")
        log(f"[2/3] 镜数不足（{len(shots_all)}/{TARGET_SHOTS}），补拆第 {extra} 批…")
        brief = (f"\n\n故事分镜已拆到第 {len(shots_all)} 镜，"
                 f"上一镜剧情是：「{prev_scene[:200]}」。请继续后续剧情，拆 {SHOTS_PER_BATCH} 个新镜头，不要重复。")
        batch = []
        for attempt in (1, 2):
            try:
                batch = storyboard.split_story(story, SHOTS_PER_BATCH, brief, local=True)
                break
            except Exception as e:  # noqa: BLE001
                log(f"    补批 {extra} 第 {attempt} 次失败：{type(e).__name__}")
                time.sleep(5)
        for s in batch:
            s["id"] = len(shots_all) + 1
            shots_all.append(s)
        log(f"    补批完成（+{len(batch)}，累计 {len(shots_all)}，预计净长 {net_duration(len(shots_all)):.0f}s）")
    SHOTS_ALL.write_text(json.dumps(shots_all, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2/3] 全部分镜完成：{len(shots_all)} 镜，预计净长 {net_duration(len(shots_all)):.0f} 秒")

prompts = [s["prompt"].strip() for s in shots_all if str(s.get("prompt", "")).strip()]
if len(prompts) < TARGET_SHOTS:
    log(f"[FAIL] 有效分镜 {len(prompts)} 不足 {TARGET_SHOTS}，请检查 shots_all.json")
    sys.exit(1)

# ---- 阶段 3：nsfw 连续成片（Wan2.2 + 无审查 LoRA，段间本地模型接戏） ----
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
    prompt=f"(本地无审查长片管道，{len(prompts)} 镜，分镜见 output/_nsfw_long/shots_all.json)",
    seed=seed, resolution="竖屏 9:16", duration=f"{len(prompts)}×5秒",
    created=int(time.time()),
)
t = threading.Thread(
    target=vs._run_long_task,
    args=(task_id, "wan", segs, WIDTH, HEIGHT, LENGTH, 20, seed, True),
    kwargs={"nsfw": True},
    daemon=False,
)
t.start()
log(f"[3/3] 已提交长片任务 {task_id}：{len(prompts)} 镜 × {SEG_SECONDS:.1f}s，"
    f"预计净长 {net_duration(len(prompts)) / 60:.1f} 分钟，生成预计 4-6 小时")

# 进度监控：每 10 分钟记一条（只有进度文案，无内容）
last_msg = ""
while t.is_alive():
    t.join(timeout=600)
    msg = vs.tasks.get(task_id, {}).get("msg", "")
    if msg and msg != last_msg:
        log(f"    … {msg}")
        last_msg = msg

meta = vs.tasks.get(task_id, {})
log(f"[DONE] 任务 {task_id} 状态：{meta.get('state')}  成片：output/{task_id}.mp4")
