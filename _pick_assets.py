# -*- coding: utf-8 -*-
"""把人工选定的参考图写进 assets.json（资产选定关卡的「人挑 + 代码写」两端）。

为什么要有这个脚本
    ① 导演管线的选定关卡（director.py --pick）只认 assets.json 里 files 里登记过的路径，
       而新一批候选卡出在 _assets_juqing/_cards/ 下，不在列表里 → 必须先登记才能选。
    ② **真坑**：selected 一旦非空，管线对每个角色都只保留「在 selected 里的」文件——
       没被选中的角色会**静默地一张参考图都不挂**（`files` 过滤后为空，代码不报错），
       结果是整片十几小时算力全废。所以本脚本**强制要求人物 + 场景全部选齐**才写盘。

用法
    python _pick_assets.py --list                    # 只看池子里有什么（不写盘）
    python _pick_assets.py 莉莉丝=a12 爱丽丝=a07 主人=a03 古代石室=s02 召唤法阵中央=s05
    不写 .png 后缀、只给编号即可；写盘前自动备份 assets.json。
"""
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from shared.paths import OUTPUT  # noqa: E402

ASSET_DIR = OUTPUT / "_assets_juqing"
CARDS = ASSET_DIR / "_cards"
MANIFEST = ASSET_DIR / "assets.json"
CARD_RE = re.compile(r"^(?P<name>.+)_(?P<tag>[ars])(?P<num>\d{2})$")


def scan_cards() -> dict:
    """扫候选池 → {资产名: {编号: 路径}}。编号形如 a12 / s02。"""
    pool: dict = {}
    for p in sorted(CARDS.glob("*.png")):
        m = CARD_RE.match(p.stem)
        if not m:
            continue
        pool.setdefault(m["name"], {})[f"{m['tag']}{m['num']}"] = p
    return pool


def rel(p: Path) -> str:
    """写成与 assets.json 既有条目一致的、相对 output/ 的反斜杠路径。"""
    return str(p.relative_to(OUTPUT)).replace("/", "\\")


def main() -> None:
    args = sys.argv[1:]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    pool = scan_cards()

    if "--list" in args or not args:
        for c in manifest.get("characters", []):
            cands = pool.get(c.get("name", ""), {})
            print(f"人物「{c['name']}」候选：{', '.join(sorted(cands)) or '（池子里没有）'}")
        for s in manifest.get("scenes", []):
            cands = pool.get(s.get("name", ""), {})
            print(f"场景「{s['name']}」候选：{', '.join(sorted(cands)) or '（池子里没有）'}")
        print(f"\n当前 selected：{manifest.get('selected') or '（空，管线会停在选定关卡）'}")
        return

    picks = {}
    for a in args:
        if a.startswith("--"):
            continue
        if "=" not in a:
            print(f"✗ 参数格式应为 名字=编号，收到 {a}")
            return
        name, num = (x.strip() for x in a.split("=", 1))
        picks[name] = num.lstrip("_").split("_")[-1]        # 允许写 a12.png / _a12

    # ---- 校验 ①：选的东西真的在池子里 ----
    bad = []
    for name, num in picks.items():
        if num not in pool.get(name, {}):
            bad.append(f"{name}={num}（可选项：{', '.join(sorted(pool.get(name, {}))) or '无'}）")
    if bad:
        print("✗ 这些选的不在候选池里：")
        for b in bad:
            print("   ", b)
        return

    # ---- 校验 ②：人物和场景必须全部选齐（否则管线静默不挂参考图）----
    must = [c["name"] for c in manifest.get("characters", [])] + \
           [s["name"] for s in manifest.get("scenes", [])]
    missing = [n for n in must if n not in picks]
    if missing:
        print("✗ 还没选齐，拒绝写盘（selected 一旦不全，未选中的角色会被静默丢掉参考图）：")
        for n in missing:
            print(f"   缺：{n}（候选：{', '.join(sorted(pool.get(n, {}))) or '池子里没有'}）")
        return

    # ---- 登记新候选到 files（管线只认登记过的路径）----
    for c in manifest.get("characters", []):
        known = set(c.get("files") or [])
        new = [rel(p) for p in pool.get(c["name"], {}).values() if rel(p) not in known]
        c["files"] = list(c.get("files") or []) + sorted(new)
    for s in manifest.get("scenes", []):
        s["file"] = rel(pool[s["name"]][picks[s["name"]]])
        if not s.get("files"):                              # 场景无候选列表，补一份备查
            s["files"] = sorted(rel(p) for p in pool[s["name"]].values())

    manifest["selected"] = [rel(pool[c["name"]][picks[c["name"]]])
                            for c in manifest.get("characters", [])]
    manifest["selected"] += [rel(pool[s["name"]][picks[s["name"]]])
                             for s in manifest.get("scenes", [])]

    bak = MANIFEST.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(MANIFEST, bak)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 事后自证：每个文件都真的在盘上 ----
    print(f"✔ 已写盘（备份：{bak.name}）")
    for f in manifest["selected"]:
        ok = (OUTPUT / f).exists()
        print(f"   {'✓' if ok else '✗ 文件不存在！'} {f}")


if __name__ == "__main__":
    main()
