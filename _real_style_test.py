# -*- coding: utf-8 -*-
"""真人风格基线测试：用已在本机的 RealVisXL V5.0 出角色卡（人物描述走本地转换）。

用法：python _real_style_test.py [模型文件名] [角色名]
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import comfy
from service import assets as A
from service.character import STYLES, build_card_workflow

BIBLE = ROOT / "output/_director_juqing/bible.json"
OUT = ROOT / "output/_assets_juqing/_real_test"

# 写实向的裸露/饰品写法（同用户的"全裸 + 片缕/透明丝物 + 脚环腿环"要求）
REAL_OUTFIT = ("completely nude, bare breasts, visible nipples, navel, "
               "sheer transparent silk fabric draped loosely, gold anklet, thigh ring, "
               "wet skin, detailed skin texture")


def main() -> None:
    ckpt = sys.argv[1] if len(sys.argv) > 1 else "RealVisXL_V5.0_fp16.safetensors"
    only = sys.argv[2] if len(sys.argv) > 2 else "莉莉丝"

    bible = json.loads(BIBLE.read_text(encoding="utf-8"))
    char = next((c for c in bible.get("characters", []) if c.get("name") == only), None)
    if not char:
        print("角色不存在:", only)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    base = A.to_english_tags(A.char_prompt_from_bible(char))
    prompt = f"{STYLES['real']['suffix']}, {base}, {REAL_OUTFIT}"

    wf = build_card_workflow(prompt, seed=20260917, batch=2, style="real")
    wf["1"]["inputs"]["ckpt_name"] = ckpt          # 覆盖成指定底模
    wf["7"]["inputs"]["filename_prefix"] = "real_test/gen"

    print(f"底模: {ckpt}")
    print(f"提示词长度: {len(prompt)}")
    t0 = time.time()
    pid = comfy.submit(wf)
    ok, hist = comfy.wait_done(pid, timeout=1800)
    print(f"出图: {ok} | {time.time()-t0:.0f}s")
    if not ok:
        return
    imgs = [v for _n, o in hist.get("outputs", {}).items() for v in o.get("images", [])]
    for i, im in enumerate(imgs, 1):
        dest = OUT / f"{only}_real_{i:02d}.png"
        if comfy.download_binary(im, dest):
            print("  →", dest)


if __name__ == "__main__":
    main()
