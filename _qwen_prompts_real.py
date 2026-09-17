# -*- coding: utf-8 -*-
"""资产参考图提示词 —— 由本地 qwen3.8 撰写（编排者不参与创作）。

流程：故事圣经的角色/场景设定 → qwen3.8 写 N 组提示词（英文）→ ComfyUI 出图 → 人工挑选锁定。
本脚本只写"任务说明书"，提示词内容全部由本地无审查模型生成。

用法：
    python _qwen_prompts_real.py <名字> [组数=12] [style=anime] [ckpt覆盖]
      style=anime  角色卡（WAI-illustrious 赛璐璐，产物 {名字}_a01.png）
      style=real   写实人像（Juggernaut，产物 {名字}_r01.png）
      style=scene  场景背景（同动漫底模，产物 {场景名}_s01.png）
    名字在 bible.json 里找不到时回落到 bible_full.json（主人只在那份里）。

规范（出图前必读）：
    ① 裸体/纯色背景/单人 —— 参考图元素越多，Ref2VA 注意力越分散；
    ② **按设定判断性别**：男性角色必须 1boy，绝不能出现 1girl；判错整张废；
       这条由 qwen 判断、由 `_gender_guard` 在代码层复核（判错直接剔除，不靠自觉）。
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import comfy
import ai
from service.character import build_card_workflow
from storyboard import _extract_json

BIBLE = ROOT / "output/_director_juqing/bible.json"
BIBLE_FULL = ROOT / "output/_director_juqing/bible_full.json"   # 角色更全（含主人），做回落
OUT = ROOT / "output/_assets_juqing/_cards"   # 按 style 分文件（a*=anime, r*=real, s*=scene）
CKPT = "RealVisXL_V5.0_fp16.safetensors"

# ---- 性别判定（按设定，不按名字）----
# 只有 主人 是男性，且设定里明写「人类男性」；其余魅魔设定里无性别词 → 默认女性。
_MALE_HINTS = ("男性", "男人", "少年", "男")
_FEMALE_HINTS = ("女性", "女人", "少女", "女")


def gender_of(setting: dict) -> str:
    """按角色设定判性别：设定文本里出现男性词即 male，否则 female。

    只看设定（identity/appearance/wardrobe/behavior），不看名字——
    「主人」这类称呼本身不含性别信息。
    """
    text = " ".join(str(setting.get(k) or "") for k in
                    ("identity", "role", "appearance", "wardrobe", "behavior"))
    if any(h in text for h in _MALE_HINTS) and not any(h in text for h in _FEMALE_HINTS):
        return "male"
    return "female"


def _has_tag(text: str, tag: str) -> bool:
    """按词边界判断 tag 是否出现。

    必须用词边界——`male focus` 是 `female focus` 的子串，朴素的 `in` 判断
    会把女性角色的提示词整批误杀（实测：爱丽丝 12 条全被剔除）。
    """
    return re.search(rf"(?<![a-z]){re.escape(tag)}(?![a-z])", text.lower()) is not None


def _gender_guard(prompts: list, gender: str, name: str) -> list:
    """代码层复核性别：male 的提示词里出现 1girl/1woman 就剔除。

    qwen 判对是常态，但不能靠自觉——判错的那张会污染后面所有镜头。
    剔除后不足 4 条则返回空表，由调用方中止（宁可不做，不出错的参考图）。
    """
    if gender == "male":
        bad = ("1girl", "1woman", "2girls", "female")
        ok = [p for p in prompts
              if _has_tag(p, "1boy") and not any(_has_tag(p, t) for t in bad)]
    else:
        bad = ("1boy", "1man", "2boys", "male focus")
        ok = [p for p in prompts if _has_tag(p, "1girl") or _has_tag(p, "1woman")]
        ok = [p for p in ok if not any(_has_tag(p, t) for t in bad)]
    dropped = len(prompts) - len(ok)
    if dropped:
        print(f"⚠ {name}（{gender}）性别不符，剔除 {dropped} 条")
    if len(ok) < 4:
        print(f"✗ {name} 合格提示词仅 {len(ok)} 条，中止（不出错性别的参考图）")
        return []
    return ok


def _desc_of(char: dict) -> str:
    return "；".join(f"{k}:{char.get(k)}" for k in
                     ("identity", "appearance", "wardrobe", "behavior") if char.get(k))

# ---- 任务说明书（只描述要求与美学规范，不写提示词内容）----
# 由调用方按 style 选择；两套都强调「简洁单人卡」——参考图元素越多，Ref2VA 注意力越分散
_BRIEF_ANIME = (
    "你是短剧的角色定妆师，为下面的角色撰写 {n} 组**动漫角色裸体设定图**用的提示词（英文，SDXL 动漫模型）。\n"
    "角色设定：\n{desc}\n\n"

    "【参考图规范·最重要】这类图会被 AI 当「参考图」逐帧参考，**画面越简单，AI 越能准确锁定人物**。\n"
    "必须做到：\n"
    "  - **完全裸体**：不穿任何衣物，也不要饰品、道具、翅膀等附加物；\n"
    "  - **纯色背景**（纯白或极浅灰），不要场景、不要阴影投影、不要装饰元素；\n"
    "  - **单人单角色**，直立站姿（自然垂手或轻微侧身），不要复杂动作；\n"
    "  - 全身可见、线条干净、轮廓清晰，人物居中占画面主要部分。\n"

    "【硬规则·第一条】**先按角色设定判断性别，再决定 tag。**\n"
    "  - 设定写的是**男性** → 必须写 `1boy, male focus, muscular`，"
    "**整条提示词里绝对不能出现 `1girl`**（写错这张参考图直接报废，后面所有镜头全跟着错）；\n"
    "  - 设定写的是**女性** → 写 `1girl`；\n"
    "  - 判断依据**只看角色设定本身**，不要凭角色名字、称呼或惯性猜。\n"
    "【内容要求】\n"
    "1. 动漫风格，高质量赛璐璐质感，线条干净；\n"
    "2. 必须写出裸体特征 tag（女性 completely nude, bare breasts, nipples, navel；"
    "男性 completely nude, muscular, defined abs, male body），否则模型会给穿衣服；\n"
    "3. 写清长相：发色发型、眼型瞳色、脸型、体型（动漫模型吃 Danbooru tag）；\n"
    "4. {n} 组之间只在**发型/姿势/视角/光线**上做差异，方便挑选。\n\n"
    "只输出 JSON 数组，形如 [\"prompt one\", \"prompt two\", ...]，不要任何解释。"
)

_BRIEF_REAL = (
    "你是短剧的角色定妆师，为下面的角色撰写 {n} 组**写实人像摄影**用的提示词（英文，SDXL 写实模型）。\n"
    "角色设定：\n{desc}\n\n"
    "【参考图规范·最重要】这类图会被 AI 当「参考图」逐帧参考，**画面越简单，AI 越能准确锁定人物**。\n"
    "  - 纯色简洁背景（纯白/浅灰），无场景无道具；单人、直立或轻微侧身；全身可见、轮廓清晰。\n"
    "【美学规范】85mm portrait lens、浅景深、柔光/伦勃朗光、眼神光(catchlights)、"
    "自然妆造、真实皮肤纹理与毛孔；优先 medium shot / close-up portrait，全身最多 2 组。\n"
    "【硬规则·第一条】**先按角色设定判断性别，再决定 tag。**\n"
    "  - 设定写的是**男性** → 必须写 `1boy, male focus`，**整条提示词里绝对不能出现 `1woman`/`1girl`**；\n"
    "  - 设定写的是**女性** → 写 `1woman`；\n"
    "  - 判断依据**只看角色设定本身**，不要凭角色名字、称呼或惯性猜。\n"
    "【内容要求】东亚人长相；全裸或近乎全裸，衣物只用极少量布料或透明丝物；"
    "可加脚环/腿环/腰链等饰品；{n} 组在长相/发型/光线/构图上有明显差异。\n"
    "只输出 JSON 数组，形如 [\"prompt one\", \"prompt two\", ...]，不要任何解释。"
)

_BRIEF_SCENE = (
    "你是短剧的美术指导，为下面的场景撰写 {n} 组**动漫背景美术设定图**用的提示词（英文，SDXL 动漫模型）。\n"
    "场景设定：\n{desc}\n\n"

    "【参考图规范·最重要】这类图会被 AI 当「参考图」逐帧参考，**画面越干净，AI 越能准确锁定场景**。\n"
    "必须做到：\n"
    "  - **画面里没有任何人物**，一个人影都不要，必须写 `no humans`；\n"
    "  - **一张图只画一个空间**，构图干净、景深明确，主体就是环境本身；\n"
    "  - **不要任何可读文字**：不要招牌、告示、书本字、海报、水印、字母数字。\n"

    "【风格】日本动画的手绘背景美术，2D 平涂上色，赛璐璐风格，MAPPA 动画电影质感，丝滑 2D 日系动漫。"
    "这是画出来的动画背景，**不是照片、不是实拍、不要写实、不要 3D 渲染、不要摄影感**。\n"
    "【内容要求】\n"
    "1. **严格照场景设定写**：地点、光线、色调以设定为准——设定说昏暗就必须是昏暗，不要自作主张调亮；\n"
    "2. 写清空间结构：墙壁与地面的材质、柱子、门窗、法阵这类标志物，别让模型自由发挥；\n"
    "3. {n} 组之间只在**机位（远景/中景/俯瞰/低角度/局部细节）与光线强弱**上做差异，方便挑选；\n"
    "4. Danbooru 式 tag 与自然语言都可以，但 `no humans` 必须写进去。\n\n"

    "只输出 JSON 数组，形如 [\"prompt one\", \"prompt two\", ...]，不要任何解释。"
)

BRIEFS = {
    "anime": _BRIEF_ANIME,
    "real": _BRIEF_REAL,
    "scene": _BRIEF_SCENE,
}
BRIEF = _BRIEF_ANIME          # 默认（兼容旧调用）


def qwen_prompts(desc: str, n: int, brief: str = "") -> list:
    raw = ai.uncensored_text((brief or BRIEF).format(n=n, desc=desc), "开始", max_tokens=4096)
    if not raw:
        return []
    try:
        obj = json.loads(_extract_json(raw))
    except Exception:
        return []
    if isinstance(obj, dict):                     # 模型可能包一层 {"prompts": [...]}
        for v in obj.values():
            if isinstance(v, list):
                obj = v
                break
    return [str(x).strip() for x in obj if isinstance(x, str) and len(str(x)) > 20][:n] if isinstance(obj, list) else []


def _load_bible() -> dict:
    """合并两份圣经：bible.json（导演管线在用）+ bible_full.json（角色更全）。"""
    merged = {"characters": [], "scenes": []}
    for path in (BIBLE, BIBLE_FULL):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("characters", "scenes"):
            known = {x.get("name") for x in merged[key]}
            merged[key] += [x for x in data.get(key, []) if x.get("name") not in known]
    return merged


def _find(bible: dict, name: str, kind: str) -> dict | None:
    return next((x for x in bible.get(kind, []) if x.get("name") == name), None)


def _scene_desc(scene: dict) -> str:
    return "；".join(f"{k}:{scene.get(k)}" for k in
                     ("place", "lighting", "palette", "when") if scene.get(k))


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "莉莉丝"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    style = sys.argv[3] if len(sys.argv) > 3 else "anime"
    ckpt_arg = sys.argv[4] if len(sys.argv) > 4 else ""
    if style not in BRIEFS:
        print(f"style 只能是 {'/'.join(BRIEFS)}，收到 {style}")
        return
    bible = _load_bible()

    if style == "scene":
        scene = _find(bible, name, "scenes")
        if not scene:
            print("场景不存在:", name, "（可选：", "、".join(s.get("name", "") for s in bible["scenes"]), "）")
            return
        desc, gender = _scene_desc(scene), ""
        print(f"{name}: 场景 {scene.get('place', '')}")
    else:
        char = _find(bible, name, "characters")
        if not char:
            print("角色不存在:", name, "（可选：", "、".join(c.get("name", "") for c in bible["characters"]), "）")
            return
        desc = _desc_of(char)
        gender = gender_of(char)
        print(f"{name}: 设定判为 {gender}（按设定文本判定，非按名字）")
        if not any(h in desc for h in _MALE_HINTS + _FEMALE_HINTS):
            print(f"⚠ {name} 设定里没有明确的性别词，默认按 female 出图——若实际是男性请先在圣经里写清")

    tag = {"anime": "a", "real": "r", "scene": "s"}[style]
    OUT.mkdir(parents=True, exist_ok=True)
    pf = OUT / f"_{name}_{style}_prompts.json"
    if pf.exists():
        prompts = json.loads(pf.read_text(encoding="utf-8"))
        print(f"复用 qwen 已写好的 {len(prompts)} 条提示词（{style}）；要重写先删 {pf.name}")
    else:
        print(f"qwen3.8 正在撰写 {n} 组提示词（{style}，27B，约 3-8 分钟）…")
        t0 = time.time()
        prompts = qwen_prompts(desc, n, BRIEFS[style])
        print(f"写好 {len(prompts)} 条，用时 {time.time()-t0:.0f}s")
        if len(prompts) < 4:
            print("提示词太少，放弃（qwen 输出异常）")
            return
        pf.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")

    if gender:
        prompts = _gender_guard(prompts, gender, name)
        if not prompts:
            return

    made = 0
    for i, prompt in enumerate(prompts, 1):
        dest = OUT / f"{name}_{tag}{i:02d}.png"
        if dest.exists():
            continue
        wf = build_card_workflow(prompt, seed=990000 + i * 331, batch=1, style=style)
        if ckpt_arg:
            wf["1"]["inputs"]["ckpt_name"] = ckpt_arg
        wf["7"]["inputs"]["filename_prefix"] = f"real_{tag}/{name}_{i:02d}"
        t0 = time.time()
        try:
            pid = comfy.submit(wf)
            ok, hist = comfy.wait_done(pid, timeout=1200)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(prompts)}] 提交失败 {type(e).__name__}", flush=True)
            continue
        if not ok:
            print(f"[{i}/{len(prompts)}] 生成失败", flush=True)
            continue
        imgs = [v for _n, o in hist.get("outputs", {}).items() for v in o.get("images", [])]
        if imgs and comfy.download_binary(imgs[0], dest):
            made += 1
            print(f"[{i}/{len(prompts)}] ok {time.time()-t0:.0f}s", flush=True)
    print(f"=== {name} 出图 {made} 张 → {OUT}")


if __name__ == "__main__":
    main()
