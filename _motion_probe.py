# -*- coding: utf-8 -*-
"""动态度探针：只出**一镜**，量帧间差，回答「主体到底动没动」。

为什么需要它（2026-09-18）：单镜 124 帧实测 4 小时，而「像 live2d」这个问题已经
失败过两次（改提示词规范、减参考图），不能每次都拿 4 小时去赌。先用最短帧数
验证方向对不对，方向对了再烧全量。

复用导演管线的真实逻辑（题材包 / 故事圣经 / 参考图选取 / H3 提示词），
不另起一套，避免「测的和跑的不是一回事」。

用法：
    python _motion_probe.py [镜号=0] [帧数=56] [标签=probe]
结果写 output/_probe_<标签>.json
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import ai
import comfy
from _motion_verify import motion_score
from service.generation import generate_h3_shot
from shared.paths import OUTPUT

WORK = OUTPUT / "_director_juqing"
ASSETS = OUTPUT / "_assets_juqing"


def load_director():
    """按路径加载导演脚本当模块用——它不在包路径里，只能这么引。"""
    spec = importlib.util.spec_from_file_location(
        "director_mod", ROOT / ".claude/skills/director/scripts/director.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    frames = int(sys.argv[2]) if len(sys.argv) > 2 else 56
    tag = sys.argv[3] if len(sys.argv) > 3 else "probe"
    result_file = OUTPUT / f"_probe_{tag}.json"

    d = load_director()
    shots = json.loads((WORK / "shots.json").read_text(encoding="utf-8"))
    if idx >= len(shots):
        print(f"镜号 {idx} 超出范围（共 {len(shots)} 镜）")
        return
    shot = shots[idx]

    # 参考图：与管线同一套选取逻辑
    man = json.loads((ASSETS / "assets.json").read_text(encoding="utf-8"))
    selected = set(man.get("selected") or [])
    char_refs, char_always = {}, {}
    for c in man.get("characters", []):
        files = [f for f in (c.get("files") or []) if not selected or f in selected]
        if files:
            char_refs[c["name"]] = comfy.upload_image(OUTPUT / files[0])
            char_always[c["name"]] = bool(c.get("always"))
    scene_refs = {s["name"]: comfy.upload_image(OUTPUT / s["file"])
                  for s in man.get("scenes", []) if s.get("file")}

    shot_scene = str(shot.get("scene", ""))
    refs = d._pick_char_refs(char_refs, char_always, shot_scene) + d._pick_scene_ref(scene_refs, shot_scene)

    # H3 提示词：与管线同一套规则（题材包 + 故事圣经 + ai._DETAIL_RULES）
    genres = d.load_genres()
    script_text = (ROOT / "juben/juqing.txt").read_bytes().decode("utf-8", errors="ignore")
    _gname, genre_body = d.pick_genre(script_text, genres)
    bible = json.loads((WORK / "bible.json").read_text(encoding="utf-8"))
    brief = d.bible_brief(bible)

    plain = str(shot.get("prompt", "")).strip()
    print(f"镜 {idx + 1}｜{frames} 帧｜参考图 {len(refs)} 张｜正在写 H3 提示词…", flush=True)
    t0 = time.time()
    h3 = ai.h3_prompt(plain, "t2v", frames / 24.0, local=True,
                      extra_rules=f"{genre_body[:900]}\n\n【故事圣经·全片一致】\n{brief[:900]}")
    if not h3:
        print("✗ H3 提示词生成失败（本地模型未就绪？）")
        return
    print(f"   提示词 {len(h3)} 字符，用时 {time.time() - t0:.0f}s", flush=True)
    print(f"   出片中（{frames} 帧，参考图 {len(refs)} 张）…", flush=True)

    t0 = time.time()
    ok, _ = generate_h3_shot(task_id=f"probe_{tag}", prompt=h3, seed=20260918,
                             width=640, height=832, length=frames, steps=20,
                             nsfw=True, ref_image_names=refs or None, timeout=14400)
    dt = (time.time() - t0) / 60
    if not ok:
        print("✗ 出片失败")
        return

    produced = OUTPUT / f"probe_{tag}.mp4"
    if not produced.exists():
        print(f"✗ 产物未找到：{produced}")
        return

    m = motion_score(produced)
    out = {"shot": idx, "frames": frames, "refs": len(refs),
           "minutes": round(dt, 1), "motion": round(m, 2), "video": produced.name}
    result_file.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 用时 {dt:.1f} 分钟｜帧间差 {m:.2f}｜参考图 {len(refs)} 张 → {result_file.name}")
    print("    对照：旧规范 124 帧 ≈ 10~15（几乎不动）；历史 A/B 最好到过 28")


if __name__ == "__main__":
    main()
