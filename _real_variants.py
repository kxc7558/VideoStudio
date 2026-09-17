# -*- coding: utf-8 -*-
"""真人风格角色卡批量抽卡：12 种长相/光线/构图变体，供肉眼挑选。

要点（实测）：
- 必须显式写东亚特征，否则出欧美脸
- 奇幻设定（紫红发/金角/金瞳）套写实风格会像廉价 cosplay → 变体里收敛成"可选的挑染/小角"
- 写实向的"美"来自：柔和光质 + 干净构图 + 细腻皮肤，而非堆砌形容词

用法：python _real_variants.py [角色名]
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import comfy
from service.character import STYLES, build_card_workflow

BIBLE = ROOT / "output/_director_juqing/bible.json"
OUT = ROOT / "output/_assets_juqing/_real_variants"

QUALITY = ("RAW photo, photorealistic, 8k uhd, high quality, film grain, "
           "natural skin texture, visible pores, detailed face, sharp focus, 35mm photograph")
ASIAN = ("east asian woman, chinese, korean idol beauty, asian facial features, "
         "almond shaped eyes, small straight nose, soft rounded face contour, warm ivory asian skin")
BODY = "slender figure, natural body proportions, soft curves"

# 12 组：长相 × 光线 × 构图（衣着保持"片缕/透明丝物 + 饰品"）
VARIANTS = [
    ("黑长直·窗光半身", "long straight black hair, delicate beautiful face, soft window light from the left, half body, looking at viewer",
     "sheer transparent silk slip barely covering, thin gold necklace"),
    ("深棕波浪·影棚柔光", "dark brown wavy hair, gentle smile, studio softbox lighting, half body, slight side angle",
     "bare body, gold body chain, thigh ring"),
    ("黑发及腰·暖灯全身", "waist-length black hair, calm expression, warm bedside lamp lighting, full body standing",
     "sheer chiffon robe fully open, gold anklet"),
    ("短发清爽·日光", "short bob black hair, bright eyes, natural daylight, close-up portrait",
     "bare shoulders, delicate lace on hips only"),
    ("黑长直·逆光轮廓", "long black hair, backlit rim light, silhouette soft edges, half body",
     "bare body, thin silk ribbon around waist"),
    ("深棕盘发·烛光", "dark brown updo hair, elegant neck, candlelight warm glow, half body, three-quarter view",
     "transparent mesh bodysuit torn at hip, gold arm ring"),
    ("黑发马尾·晨光", "high ponytail black hair, fresh morning light through curtains, full body sitting on edge",
     "bare body, see-through silk draped on shoulders, ankle bracelet"),
    ("柔和卷发·柔光特写", "soft curly black hair, dewy skin, diffused beauty light, close-up face and shoulders",
     "bare shoulders, jeweled waist chain"),
    ("黑长直·冷调时尚", "long straight black hair, cool fashion editorial lighting, full body contrapposto",
     "bare body, minimal gold ornaments, leg rings"),
    ("深棕长发·电影感", "dark brown long hair, cinematic side lighting, half body, looking away",
     "translucent chiffon shawl slipping off, silver anklet"),
    ("黑发微卷·自然光", "slightly wavy black hair, natural window daylight, full body standing straight, front view",
     "bare body, tiny scraps of silk on hips"),
    ("亚麻棕·甜美", "ash brown hair with soft bangs, sweet expression, soft warm light, half body",
     "bare body, delicate gold anklet and thigh ring"),
]

# 可选：小恶魔角（真人风格下收敛成"小巧装饰角"）——一半变体带，一半不带
HORN = "small delicate black decorative horns on forehead"


def main() -> None:
    char_name = sys.argv[1] if len(sys.argv) > 1 else "莉莉丝"
    bible = json.loads(BIBLE.read_text(encoding="utf-8"))
    char = next((c for c in bible.get("characters", []) if c.get("name") == char_name), None)
    if not char:
        print("角色不存在:", char_name)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    made = 0
    for i, (tag, look, outfit) in enumerate(VARIANTS):
        dest = OUT / f"{char_name}_{i + 1:02d}_{tag.split('·')[0]}.png"
        if dest.exists():
            continue
        horns = f", {HORN}" if i % 2 == 0 else ""
        prompt = f"{QUALITY}, {ASIAN}, {look}{horns}, {BODY}, {outfit}, plain studio background"
        wf = build_card_workflow(prompt, seed=880000 + i * 613, batch=1, style="real")
        wf["1"]["inputs"]["ckpt_name"] = "RealVisXL_V5.0_fp16.safetensors"
        wf["7"]["inputs"]["filename_prefix"] = f"real_var/{char_name}_{i+1:02d}"
        t0 = time.time()
        try:
            pid = comfy.submit(wf)
            ok, hist = comfy.wait_done(pid, timeout=1200)
        except Exception as e:  # noqa: BLE001
            print(f"[{i+1}/12] 提交失败 {type(e).__name__}", flush=True)
            continue
        if not ok:
            print(f"[{i+1}/12] 生成失败", flush=True)
            continue
        imgs = [v for _n, o in hist.get("outputs", {}).items() for v in o.get("images", [])]
        if imgs and comfy.download_binary(imgs[0], dest):
            made += 1
            print(f"[{i+1}/12] {tag}  用时 {time.time()-t0:.0f}s", flush=True)
    print(f"=== 完成 {made} 张 → {OUT}")


if __name__ == "__main__":
    main()
