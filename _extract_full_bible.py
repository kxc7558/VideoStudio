# -*- coding: utf-8 -*-
"""完整抽取：把剧本里所有出场人物与所有场景列全（含次要/功能性角色）。

产出 output/_director_juqing/bible_full.json，供资产批量生成使用。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / ".claude" / "skills" / "director" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ai
import storyboard
import director

SYSTEM = (
    "你是短剧制片统筹。通读剧本，列出**全部**出场人物与**全部**出现的地点，不要遗漏次要角色。\n"
    "严格只输出 JSON：\n"
    '{"characters":[{"name":"姓名或称呼","role":"主/次","identity":"身份年龄",'
    '"appearance":"外貌(发色发型/体型/特征)","wardrobe":"服装或身体特征","behavior":"行为特征"}],'
    '"scenes":[{"name":"场景名","place":"地点环境","lighting":"光线","palette":"色彩","when":"出现时机"}]}\n'
    "要求：人物最多 8 个、场景最多 8 个；即使只有一只手或一个背影出镜也要列（role 标 功能性）；"
    "外貌要具体到能画出来。JSON 完整闭合。"
)


def main() -> None:
    text = Path("juben/juqing.txt").read_text(encoding="utf-8", errors="ignore")
    sampled = director.sample_script(text)
    raw = ai.uncensored_text(SYSTEM, f"剧本如下：\n\n{sampled}", max_tokens=4096)
    if not raw:
        print("模型无输出")
        return
    try:
        obj = json.loads(storyboard._extract_json(raw))
    except Exception as e:  # noqa: BLE001
        print("解析失败:", type(e).__name__, str(e)[:120])
        return

    print("=== 完整人物清单 ===")
    for c in obj.get("characters", []):
        app = str(c.get("appearance", ""))[:46]
        print(f"  [{c.get('role', '?')}] {c.get('name')} | {app}")
    print("=== 完整场景清单 ===")
    for s in obj.get("scenes", []):
        place = str(s.get("place", ""))[:40]
        light = str(s.get("lighting", ""))[:24]
        print(f"  {s.get('name')} | {place} | {light}")

    out = Path("output/_director_juqing/bible_full.json")
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已存 {out.name}（人物 {len(obj.get('characters', []))} / 场景 {len(obj.get('scenes', []))}）")


if __name__ == "__main__":
    main()
