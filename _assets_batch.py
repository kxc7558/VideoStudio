# -*- coding: utf-8 -*-
"""资产批量抽卡 v2：每人物多张候选，姿势/衣着/饰品做风格微调。

提示词要求（按用户指定）：全裸露出 + 衣物为片缕/透明丝物/装饰性饰品（脚环腿环等）。
人物身份特征（发色/瞳色/角/体型）从故事圣经读，保证同一角色跨图一致。

用法：venv/Scripts/python.exe _assets_batch.py --per-char 12 [--only 莉莉丝]
产出：output/_assets_juqing/characters/<角色>_NN.png + 对比图
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import comfy
from service import assets as A
from service.character import build_card_workflow

OUT_DIR = Path("output/_assets_juqing/characters")
BIBLE = Path("output/_director_juqing/bible.json")

# 质量底座（Illustrious 系）
QUALITY = ("masterpiece, best quality, amazing quality, very aesthetic, absurdres, "
           "very detailed, intricate details, sharp focus, solo, 1girl, completely nude, nipples, navel")

# 衣着 / 饰品微调（片缕、透明丝物、装饰性饰品）
OUTFITS = [
    "bare body, no clothes, delicate gold anklet, thigh ring",
    "bare body, see-through silk slip covering nothing, thin chain necklace",
    "bare body, sheer transparent veil draped over shoulders, silver leg rings",
    "bare body, tiny scraps of torn cloth barely covering, gold body chain",
    "bare body, transparent lace robe fully open, ankle bracelets",
    "bare body, thin silk ribbon wrapped around waist, thigh bands",
    "bare body, see-through mesh bodystocking torn at hips, gold anklets",
    "bare body, jeweled waist chain and thigh rings only",
    "bare body, translucent chiffon shawl slipping off, silver anklet",
    "bare body, minimal ceremonial gold ornaments, arm rings and leg rings",
    "bare body, wet transparent fabric clinging, thin chain belt",
    "bare body, sheer ribbon bindings on thighs and wrists, small gold rings",
]

# 姿势 / 构图 / 背景微调
POSES = [
    "standing straight, front view, looking at viewer, plain white background",
    "standing, slight side view, hand on hip, soft gradient background",
    "standing, contrapposto, hair flowing, plain light background",
    "sitting on edge, legs crossed, looking at viewer, plain white background",
    "kneeling, hands on thighs, looking up, dark gradient background",
    "lying on side, propped on elbow, looking at viewer, plain background",
    "standing, back view with face turned to viewer, wings spread, plain background",
    "standing, one leg raised, foot on stool, plain white background",
    "crouching, arms resting on knees, looking at viewer, plain background",
    "standing, arms raised behind head, stretched pose, plain white background",
    "leaning against wall, weight on one hip, soft shadow, plain background",
    "standing, hands behind back, chin lifted, plain white background",
]


def variant_prompt(base_tags: str, i: int) -> str:
    """第 i 张的提示词：同一身份 + 第 i 组衣着/姿势微调。"""
    outfit = OUTFITS[i % len(OUTFITS)]
    pose = POSES[(i * 5 + 3) % len(POSES)]      # 错位组合，避免衣着和姿势同步循环
    return f"{QUALITY}, {base_tags}, {outfit}, {pose}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-char", type=int, default=12, help="每人物抽几张")
    ap.add_argument("--only", default="", help="只抽指定角色（逗号分隔）")
    ap.add_argument("--start", type=int, default=0, help="从第 N 张续跑")
    args = ap.parse_args()

    bible = json.loads(BIBLE.read_text(encoding="utf-8"))
    chars = bible.get("characters", [])
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        chars = [c for c in chars if c.get("name") in want]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for c in chars:
        name = c.get("name", "")
        base_tags = A.to_english_tags(A.char_prompt_from_bible(c))
        if not base_tags:
            continue
        made = 0
        for i in range(args.start, args.per_char):
            dest = OUT_DIR / f"{name}_{i + 1:02d}.png"
            if dest.exists():
                continue
            prompt = variant_prompt(base_tags, i)
            wf = build_card_workflow(prompt, seed=770000 + i * 137, batch=1)
            t0 = time.time()
            try:
                pid = comfy.submit(wf)
                ok, hist = comfy.wait_done(pid, timeout=1200)
            except Exception as e:
                print(f"[{name} #{i+1}] 提交失败 {type(e).__name__}", flush=True)
                continue
            if not ok:
                print(f"[{name} #{i+1}] 生成失败", flush=True)
                continue
            imgs = [v for _n, o in hist.get("outputs", {}).items() for v in o.get("images", [])]
            if imgs and comfy.download_binary(imgs[0], dest):
                made += 1
                print(f"[{name} #{i+1}/{args.per_char}] ok {(time.time()-t0):.0f}s "
                      f"({OUTFITS[i % len(OUTFITS)][:38]}…)", flush=True)
        print(f"=== {name}: 本轮新增 {made} 张 ===", flush=True)


if __name__ == "__main__":
    main()
