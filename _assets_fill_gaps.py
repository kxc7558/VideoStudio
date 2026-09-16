# -*- coding: utf-8 -*-
"""补抽缺失资产：男性角色（主人）+ 场景卡多候选。

- 男性角色用 1boy 系 tag（现有模板是 1girl，需分性别）
- 场景用 no humans + 不同机位/光线做微调
产出到 output/_assets_juqing/characters|scenes/
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

BASE = Path("output/_assets_juqing")
BIBLE_FULL = Path("output/_director_juqing/bible_full.json")

# 男性角色：质量底座 + 暴露/饰品的对应写法
MALE_QUALITY = ("masterpiece, best quality, amazing quality, very aesthetic, absurdres, "
                "very detailed, intricate details, sharp focus, solo, 1boy, male focus, "
                "completely nude, muscular, defined abs")
MALE_OUTFITS = [
    "bare body, no clothes, small gold arm ring",
    "bare body, thin silk loincloth barely covering, gold collar",
    "bare body, sheer transparent drape over one shoulder, wrist bands",
    "bare body, torn cloth wrap on waist, ankle rings",
    "bare body, minimal gold body chain across chest",
    "bare body, see-through mesh cloth on hips only, arm bands",
]
MALE_POSES = [
    "standing straight, front view, arms at sides, plain white background",
    "standing, side view, looking away, muscular back visible, plain background",
    "sitting on stone edge, leaning forward, elbows on knees, plain background",
    "standing, one hand on hip, other clenched, plain white background",
    "kneeling, hands on thighs, looking forward, dark gradient background",
    "standing, arms crossed, back view with face turned, plain background",
]

# 场景：不同机位/光线微调
SCENE_QUALITY = ("masterpiece, best quality, amazing quality, very aesthetic, absurdres, "
                 "very detailed, intricate details, no humans, scenery, cinematic lighting")
SCENE_VARIANTS = [
    "wide shot, ancient stone chamber interior, rough megalith walls, ritual magic circle glowing on floor",
    "medium shot, magic circle center with intricate runes, purple-red glow, floating dust motes",
    "low angle, towering stone pillars, dim misty atmosphere, faint magic light from below",
    "overhead shot, circular summoning circle patterns, candles and worn flagstones",
    "close-up detail, carved runes on stone floor, glowing purple light seeping through cracks",
    "wide shot at night, stone chamber with arched openings, moonlight and magic glow mixing",
]


def run_batch(kind: str, items: list, out_sub: str, count: int, name_filter: str = "") -> None:
    """kind: character|scene。逐张生成并落盘（已存在则跳过）。"""
    out_dir = BASE / out_sub
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, base_tags, variants in items:
        if name_filter and name != name_filter:
            continue
        for i in range(count):
            dest = out_dir / f"{name}_{i + 1:02d}.png"
            if dest.exists():
                continue
            variant = variants[i % len(variants)]
            prompt = f"{base_tags}, {variant}"
            wf = build_card_workflow(prompt, seed=880000 + i * 271, batch=1)
            t0 = time.time()
            try:
                pid = comfy.submit(wf)
                ok, hist = comfy.wait_done(pid, timeout=1200)
            except Exception as e:  # noqa: BLE001
                print(f"[{name} #{i+1}] 提交失败 {type(e).__name__}", flush=True)
                continue
            if not ok:
                print(f"[{name} #{i+1}] 生成失败", flush=True)
                continue
            imgs = [v for _n, o in hist.get("outputs", {}).items() for v in o.get("images", [])]
            if imgs and comfy.download_binary(imgs[0], dest):
                print(f"[{name} #{i+1}/{count}] ok {time.time()-t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=8, help="每个资产抽几张")
    ap.add_argument("--only", default="", help="只抽指定名字")
    args = ap.parse_args()

    full = json.loads(BIBLE_FULL.read_text(encoding="utf-8"))
    chars, scenes = [], []
    for c in full.get("characters", []):
        name = c.get("name", "")
        app = str(c.get("appearance", ""))
        # 男性角色（主人）走 1boy 模板；女性走通用模板
        is_male = name in ("主人", "男主") or "健硕" in app or "肌肉" in app
        tags = A.to_english_tags(A.char_prompt_from_bible(c))
        if is_male:
            tags = f"{MALE_QUALITY}, {tags}"
            chars.append((name, tags, MALE_OUTFITS))
        else:
            chars.append((name, f"{A._LOCAL_CHAR_SUFFIX}, {tags}", None))

    for s in full.get("scenes", []):
        name = s.get("name", "")
        desc = A.scene_prompt_from_bible(s)
        scenes.append((name, f"{SCENE_QUALITY}, {A.to_english_tags(desc)}", SCENE_VARIANTS))

    # 女性角色走 _assets_batch 的衣着变体；这里只补男性与场景
    male_items = [(n, t, v) for n, t, v in chars if v is not None]
    if male_items:
        run_batch("character", male_items, "characters", args.count, args.only)
    scene_items = [(n, t, v) for n, t, v in scenes if not args.only or n == args.only]
    if scene_items:
        run_batch("scene", scene_items, "scenes", max(4, args.count // 2), args.only)


if __name__ == "__main__":
    main()
