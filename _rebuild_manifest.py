# -*- coding: utf-8 -*-
"""按磁盘实际文件重建 assets.json（含人工选定与参考图匹配标记）。

用法：python _rebuild_manifest.py --pick "莉莉丝_08.png,爱丽丝_10.png,主人_04.png,古代石室_04.png,召唤法阵中央_04.png"
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

ASSET_DIR = Path("output/_assets_juqing")
MANIFEST = ASSET_DIR / "assets.json"

# 每镜都带参考图的角色（本片两位主角贯穿全片）；未列出的按镜头文本匹配
ALWAYS_CHARS = {"莉莉丝", "爱丽丝"}
# 场景：本片全程同一地点，两张场景卡都全片带上
ALWAYS_SCENES = {"古代石室", "召唤法阵中央"}


def group_by_name(files: list) -> dict:
    groups: dict = {}
    for f in files:
        stem = f.stem
        name = stem.rsplit("_", 1)[0] if "_" in stem else stem
        groups.setdefault(name, []).append(f)
    return {k: sorted(v) for k, v in groups.items()}


def rel(p: Path) -> str:
    return str(p.relative_to(Path("output")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", required=True, help="选定的资产文件名（逗号分隔）")
    ap.add_argument("--no-clear", action="store_true", help="不清理旧产出（默认保留）")
    args = ap.parse_args()

    picked_names = {x.strip() for x in args.pick.split(",") if x.strip()}

    chars = group_by_name(sorted((ASSET_DIR / "characters").glob("*.png")))
    scenes = group_by_name(sorted((ASSET_DIR / "scenes").glob("*.png")))

    manifest = {
        "provider": "local",
        "characters": [],
        "scenes": [],
        "selected": [],
    }

    for name, files in chars.items():
        rels = [rel(f) for f in files]
        manifest["characters"].append({
            "name": name,
            "desc": "",
            "files": rels,
            "provider": "local",
            "always": name in ALWAYS_CHARS,
        })
        manifest["selected"] += [r for r in rels if Path(r).name in picked_names]

    for name, files in scenes.items():
        rels = [rel(f) for f in files]
        chosen = next((r for r in rels if Path(r).name in picked_names), None)
        if not chosen:
            continue        # 没被选中的场景不进清单
        manifest["scenes"].append({
            "name": name,
            "desc": "",
            "file": chosen,
            "provider": "local",
            "always": name in ALWAYS_SCENES,
        })
        manifest["selected"].append(chosen)

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("清单已重建：")
    for c in manifest["characters"]:
        mark = "★全片带" if c["always"] else "  按镜头匹配"
        print(f"  人物 {c['name']:6} {len(c['files']):>2} 张候选 {mark}")
    for s in manifest["scenes"]:
        mark = "★全片带" if s["always"] else "  按镜头匹配"
        print(f"  场景 {s['name']:10} 选定 {Path(s['file']).name} {mark}")
    print(f"  已选定 {len(manifest['selected'])} 张：{[Path(x).name for x in manifest['selected']]}")
    missing = picked_names - {Path(x).name for x in manifest["selected"]}
    if missing:
        print(f"  ⚠️ 未匹配到的点名：{missing}")


if __name__ == "__main__":
    main()
