# -*- coding: utf-8 -*-
"""导演管线：剧本 → 故事圣经 → 分镜表 → H3 提示词 → 逐镜无审查出片 → 拼接成片。

全本地：编剧/分镜/提示词走 Ollama qwen3.8-uncensored，出片走 ComfyUI MiniMax H3 无审查链路。
剧本文件只读字节送本地模型，不打印内容、不落日志。

用法：
    python director.py --script juben/juqing.txt --shots 12 --out output/my_drama.mp4
    python director.py --script juben/juqing.txt --plan-only        # 只出分镜表，不烧显卡
    python director.py --script juben/juqing.txt --start 6         # 从第 6 镜续跑

断点续跑：每镜出片后立即落盘到工作目录，已存在的段自动跳过。
控制：output/_control.txt 写 PAUSE 暂停、STOP 停止（与桌宠/其他产线一致）。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]   # .claude/skills/director/scripts/ → 项目根
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import ai
import comfy
import storyboard
from service import assets as assets_mod
from service.generation import generate_h3_shot
from shared.ffmpeg_tools import concat_videos
from shared.paths import OUTPUT

# H3 帧网格 17k+5（24fps）：56≈2.3s / 73≈3.0s / 124≈5.2s / 192≈8.0s
FRAME_GRID = [56, 73, 124, 192, 294]
GENRE_DIR = Path(__file__).resolve().parents[1] / "references" / "genres"
CONTROL = OUTPUT / "_control.txt"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def control_state() -> str:
    """读控制文件：RUN / PAUSE / STOP（缺失视为 RUN）。"""
    try:
        return (CONTROL.read_text(encoding="utf-8").strip().upper() or "RUN")
    except Exception:
        return "RUN"


def load_genres() -> dict:
    """读题材包 frontmatter：name / match 正则 / 正文。"""
    genres = {}
    if not GENRE_DIR.exists():
        return genres
    for f in GENRE_DIR.glob("*.md"):
        if f.name == "README.md":
            continue
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        fm, body = m.group(1), m.group(2)
        name = (re.search(r"^name:\s*(\S+)", fm, re.M) or [None, f.stem])[1]
        match = (re.search(r"match:\s*(.+)$", fm, re.M) or [None, ""])[1].strip()
        genres[name] = {"match": match, "body": body.strip(), "file": f.name}
    return genres


def pick_genre(script_text: str, genres: dict, forced: str = "") -> tuple:
    """按剧本内容匹配题材（forced 优先）。返回 (题材名, 镜头语言正文)。"""
    if forced and forced in genres:
        return forced, genres[forced]["body"]
    hits = []
    for name, g in genres.items():
        pat = g.get("match") or ""
        if not pat:
            continue
        try:
            if re.search(pat, script_text, re.I):
                hits.append(name)
        except re.error:
            continue
    if hits:
        # 取第一个命中的题材（题材包 priority 由文件名顺序近似，够用）
        name = hits[0]
        return name, genres[name]["body"]
    generic = genres.get("screenwriter")
    return ("screenwriter", generic["body"]) if generic else ("", "")


def sample_script(text: str, head: int = 6000, mid: int = 3000, tail: int = 3000) -> str:
    """长剧本采样：头+中+尾，既控上下文又保住人物/结局信息。"""
    n = len(text)
    if n <= head + mid + tail:
        return text
    m0 = max(0, n // 2 - mid // 2)
    return f"{text[:head]}\n\n……（中略）……\n\n{text[m0:m0 + mid]}\n\n……（中略）……\n\n{text[-tail:]}"


def build_bible(script_text: str, genre_body: str) -> dict:
    """阶段①：抽故事圣经（人物/场景/不可违背的事实）。失败返回空 dict。

    长剧本先采样；max_tokens 调大以免结构化输出被截断（默认 2048 会截断）。
    """
    system = (
        "你是短剧制片人。从用户给的剧本里提取「故事圣经」——后续所有镜头都必须遵守的硬事实。\n"
        "严格只输出 JSON，格式：\n"
        '{"title":"片名","logline":"一句话钩子",'
        '"characters":[{"name":"姓名","identity":"身份年龄","appearance":"外貌特征(发色/脸型/体型)","wardrobe":"服装(颜色+款式)","behavior":"行为特征"}],'
        '"scenes":[{"name":"场景名","place":"地点","lighting":"光线基调","palette":"色彩"}],'
        '"rules":["不可违背的设定，如：女主全程左眉有痣"]}\n'
        "人物外貌必须具体到可直接画出来（发色、发型、脸型、体型、固定服装颜色）。"
        "人物最多 6 个、场景最多 6 个，只留最重要的。JSON 要完整闭合。"
    )
    user = f"剧本如下：\n\n{sample_script(script_text)}"
    raw = ai.uncensored_text(system, user, max_tokens=4096)
    if not raw:
        return {}
    try:
        return json.loads(storyboard._extract_json(raw))
    except Exception:
        return {}


def bible_brief(bible: dict) -> str:
    """把故事圣经压成可注入每次调用的提示词前置。"""
    if not bible:
        return ""
    parts = []
    if bible.get("logline"):
        parts.append(f"全片钩子：{bible['logline']}")
    for c in bible.get("characters", [])[:6]:
        d = "；".join(f"{k}：{c.get(k)}" for k in ("identity", "appearance", "wardrobe", "behavior") if c.get(k))
        if d:
            parts.append(f"人物「{c.get('name', '')}」（全片保持一致）：{d}")
    for s in bible.get("scenes", [])[:6]:
        d = "；".join(f"{k}：{s.get(k)}" for k in ("place", "lighting", "palette") if s.get(k))
        if d:
            parts.append(f"场景「{s.get('name', '')}」：{d}")
    if bible.get("rules"):
        parts.append("不可违背：" + "；".join(str(r) for r in bible["rules"][:6]))
    return "\n".join(parts)


def split_shots(script_text: str, target: int, brief: str, genre_body: str, batch: int = 6) -> list:
    """阶段②：分批拆镜（本地模型），每批带故事圣经 + 题材镜头语言。"""
    shots = []
    story_len = max(1, len(script_text))
    window = max(3000, story_len // max(1, (target // batch) or 1))
    while len(shots) < target:
        done = min(0.92, len(shots) / target)
        start = int(story_len * done)
        end = min(story_len, start + window)
        if end <= start:
            break
        chunk = script_text[start:end]
        log(f"② 拆分镜 {len(shots)}/{target}（剧本 {start * 100 // story_len}%~{end * 100 // story_len}%）")
        ctx = f"\n\n{genre_body[:1500]}\n\n{brief}".strip()
        try:
            picked = storyboard.split_story(chunk, batch, ctx, local=True)
        except Exception as e:  # noqa: BLE001
            log(f"   拆镜失败：{type(e).__name__}，跳过本批")
            picked = []
        if not picked:
            break
        for s in picked:
            s["id"] = len(shots) + 1
            shots.append(s)
    return shots


def main():
    ap = argparse.ArgumentParser(description="导演管线：剧本 → 短剧成片（本地无审查）")
    ap.add_argument("--script", required=True, help="剧本文件路径")
    ap.add_argument("--shots", type=int, default=12, help="目标镜头数（默认 12）")
    ap.add_argument("--genre", default="", help="强制题材（suspense/sweet/chase-action/...）")
    ap.add_argument("--start", type=int, default=0, help="从第 N 镜续跑（0 起）")
    ap.add_argument("--plan-only", action="store_true", help="只出故事圣经+分镜表，不出片")
    ap.add_argument("--out", default="", help="成片输出路径")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=832)
    ap.add_argument("--length", type=int, default=124, help="每镜帧数（H3 网格 56/73/124/192）")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--no-nsfw", action="store_true", help="关闭无审查链路（默认开启）")
    ap.add_argument("--asset-provider", default="", choices=["", "local", "cloud"],
                    help="资产出图引擎：local=NoobAI（无审查）/ cloud=豆包 Seedream（质量更优，仅普通剧情）。默认按 nsfw 自动选")
    ap.add_argument("--char-candidates", type=int, default=2, help="每人物出几张候选（local 有效）")
    ap.add_argument("--assets-only", action="store_true", help="只出资产（人物卡/场景卡），先人工选定再出片")
    ap.add_argument("--no-ref", action="store_true", help="不用参考图（退回纯 t2v）")
    args = ap.parse_args()

    if args.length not in FRAME_GRID:
        log(f"⚠️ 帧数 {args.length} 不在 H3 网格 {FRAME_GRID}，已纠正为最近的 {min(FRAME_GRID, key=lambda g: abs(g - args.length))}")
        args.length = min(FRAME_GRID, key=lambda g: abs(g - args.length))

    script_path = Path(args.script)
    if not script_path.exists():
        log(f"❌ 剧本不存在：{script_path}")
        sys.exit(1)
    # 只读字节送本地模型；打印长度不打印内容
    script_text = script_path.read_bytes().decode("utf-8", errors="ignore")
    work = OUTPUT / f"_director_{script_path.stem}"
    work.mkdir(parents=True, exist_ok=True)
    log(f"剧本载入（{len(script_text)} 字）｜工作目录 {work.name}")

    if not ai.uncensored_ready():
        log("❌ 本地编剧模型未就绪（Ollama qwen3.8-uncensored）")
        sys.exit(1)

    genres = load_genres()
    genre_name, genre_body = pick_genre(script_text, genres, args.genre)
    log(f"① 题材匹配：{genre_name or '（通用）'}（{len(genres)} 个题材包可用）")

    # 阶段①：故事圣经
    bible_file = work / "bible.json"
    if bible_file.exists():
        bible = json.loads(bible_file.read_text(encoding="utf-8"))
        log("① 复用已有故事圣经")
    else:
        log("① 本地 qwen3.8 正在提炼故事圣经…")
        bible = build_bible(script_text, genre_body)
        bible_file.write_text(json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"① 故事圣经完成（{len(bible.get('characters', []))} 人物 / {len(bible.get('scenes', []))} 场景）")
    brief = bible_brief(bible)

    # 阶段②：分镜表
    shots_file = work / "shots.json"
    if shots_file.exists():
        shots = json.loads(shots_file.read_text(encoding="utf-8"))
        log(f"② 复用已有分镜（{len(shots)} 镜）")
    else:
        log(f"② 本地 qwen3.8 正在拆 {args.shots} 个镜头…")
        shots = split_shots(script_text, args.shots, brief, genre_body)
        shots_file.write_text(json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"② 分镜完成（{len(shots)} 镜）")
    if not shots:
        log("❌ 分镜为空，退出")
        sys.exit(1)

    if args.plan_only:
        log(f"✅ 只出计划：故事圣经 {bible_file.name}｜分镜 {shots_file.name}（{len(shots)} 镜）")
        return

    # 阶段②.5：资产（人物卡 + 场景卡）——出片前先定角色长相与场景基调
    assets_file = work / "assets.json"
    manifest = {}
    if assets_file.exists():
        manifest = json.loads(assets_file.read_text(encoding="utf-8"))
        chars = len(manifest.get("characters", []))
        scenes = len(manifest.get("scenes", []))
        log(f"②.5 复用已有资产（{chars} 人物 / {scenes} 场景，provider={manifest.get('provider')}）")
    elif bible:
        # provider：无审查内容必须本地（云端会拦）；普通剧情默认云端（质量更优）
        provider = args.asset_provider or ("local" if not args.no_nsfw else "cloud")
        log(f"②.5 生成资产（provider={provider}）——人物卡 + 场景卡…")
        if not comfy.is_ready():
            log("❌ ComfyUI 未启动（8188），无法出资产")
            sys.exit(1)
        manifest = assets_mod.build_all(bible, script_path.stem, provider=provider,
                                        char_candidates=args.char_candidates)
        for c in manifest.get("characters", []):
            log(f"   人物「{c.get('name')}」→ {len(c.get('files', []))} 张"
                + (f"｜失败：{c.get('error', '')}" if c.get("error") else ""))
        for s in manifest.get("scenes", []):
            log(f"   场景「{s.get('name')}」→ {'OK' if s.get('file') else '失败'}"
                + (f"｜{s.get('error', '')}" if s.get("error") else ""))
    else:
        log("②.5 无故事圣经，跳过资产")

    if args.assets_only:
        log(f"✅ 只出资产：{assets_file.relative_to(OUTPUT)}（人工选定后去掉 --assets-only 再跑）")
        return

    # 参考图：选定的人物卡上传给 ComfyUI，全片每镜都用它做「全程注意力」
    ref_comfy_name = ""
    if manifest and not args.no_ref:
        pick = assets_mod.pick_reference(manifest, "characters", 0)
        if pick:
            try:
                ref_comfy_name = comfy.upload_image(OUTPUT / pick)
                log(f"②.5 参考图已就位：{Path(pick).name} → 全片 Ref2VA 锁定")
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ 参考图上传失败（{type(e).__name__}），退回 t2v")
                ref_comfy_name = ""

    # 阶段③：集中写完所有 H3 提示词（qwen 用完立即卸载，把内存让给视频模型）
    seconds = args.length / 24.0
    prompts_file = work / "h3_prompts.json"
    if prompts_file.exists():
        h3_prompts = json.loads(prompts_file.read_text(encoding="utf-8"))
        log(f"③ 复用已有 H3 提示词（{len(h3_prompts)} 条）")
    else:
        log(f"③ 本地 qwen3.8 集中写 {len(shots)} 镜的 H3 提示词（写完自动卸载模型）…")
        h3_prompts = []
        for i, shot in enumerate(shots):
            plain = str(shot.get("prompt", "")).strip()
            if not plain:
                h3_prompts.append("")
                continue
            rules = genre_body[:900]
            if brief:
                rules = f"{rules}\n\n【故事圣经·全片一致】\n{brief[:900]}"
            log(f"   第 {i + 1}/{len(shots)} 镜提示词…")
            txt = ai.h3_prompt(plain, "t2v", seconds, local=True, extra_rules=rules)
            if not txt:
                txt = plain
            elif brief:
                txt = f"{txt}\n\n[Continuity] {brief[:400]}"
            h3_prompts.append(txt)
        prompts_file.write_text(json.dumps(h3_prompts, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"③ H3 提示词完成（{len(h3_prompts)} 条）")
        # 卸载 qwen，把内存/显存让给 H3
        try:
            import httpx
            httpx.post("http://127.0.0.1:11434/api/generate",
                       json={"model": ai.UNCENSORED_MODEL, "keep_alive": 0}, timeout=30)
            log("   已卸载本地编剧模型（释放内存给视频模型）")
        except Exception:
            pass

    if not comfy.is_ready():
        log("❌ ComfyUI 未启动（8188）")
        sys.exit(1)

    # 阶段④：逐镜出片（H3 独占内存）
    segs = []
    for i, shot in enumerate(shots):
        seg = work / f"seg_{i:02d}.mp4"
        if seg.exists():
            segs.append(seg)
            continue
        if i < args.start:
            continue
        ctrl = control_state()
        if ctrl == "STOP":
            log("⏹ 控制文件为 STOP，停止")
            break
        while ctrl == "PAUSE":
            log("⏸ 控制文件为 PAUSE，等待…")
            time.sleep(15)
            ctrl = control_state()
            if ctrl == "STOP":
                break
        if ctrl == "STOP":
            break

        h3_text = (h3_prompts[i] if i < len(h3_prompts) else "") or str(shot.get("prompt", "")).strip()
        if not h3_text:
            continue
        log(f"④ 第 {i + 1}/{len(shots)} 镜：出片中（{args.width}×{args.height}/{args.length}帧/{args.steps}步）…")
        ok, _ = generate_h3_shot(
            task_id=f"director_{script_path.stem}_{i:02d}",
            prompt=h3_text, seed=20260915 + i,
            width=args.width, height=args.height, length=args.length,
            steps=args.steps, nsfw=not args.no_nsfw,
            ref_image_name=ref_comfy_name or None,   # Ref2VA：参考图全程注意力锁人物/场景
            timeout=7200,   # 124 帧 ≈ 52 分钟/镜，留足余量（机器有负载时更慢）
        )
        produced = OUTPUT / f"director_{script_path.stem}_{i:02d}.mp4"
        if ok and produced.exists():
            produced.replace(seg)
            segs.append(seg)
            log(f"✅ 第 {i + 1}/{len(shots)} 镜完成：{seg.name}")
        else:
            log(f"❌ 第 {i + 1}/{len(shots)} 镜失败，跳过（可 --start {i} 续跑）")

    # 阶段⑤：拼接成片
    if len(segs) < 2:
        log(f"⚠️ 只有 {len(segs)} 段，未拼接（至少 2 段）")
        return
    out = Path(args.out) if args.out else (OUTPUT / f"{script_path.stem}_drama.mp4")
    log(f"⑤ 拼接 {len(segs)} 段 → {out.name}")
    concat_videos(segs, out)
    log(f"🎬 成片完成：{out}（{len(segs)} 镜 × {seconds:.1f}s ≈ {len(segs) * seconds:.0f}s）")


if __name__ == "__main__":
    main()
