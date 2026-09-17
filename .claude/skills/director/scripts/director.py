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


# ComfyUI 位置（长跑防崩：挂了自动拉起）
COMFY_DIR = Path(r"D:\ComfyUI_Wan")
COMFY_PY = COMFY_DIR / "venv" / "Scripts" / "python.exe"


def ensure_comfy(wait_s: int = 240) -> bool:
    """确保 ComfyUI 在线：不在就拉起并等就绪。

    长跑必备——H3 跑大帧数偶发 CUDA OOM 会**整进程消失**（不报错、直接没），
    没有这层保护，一次崩溃就浪费整晚。
    """
    if comfy.is_ready():
        return True
    log("⚠️ ComfyUI 无响应（疑似 OOM 崩进程），正在拉起…")
    try:
        import subprocess
        subprocess.Popen(
            [str(COMFY_PY), "main.py", "--lowvram", "--reserve-vram", "0.3",
             "--listen", "127.0.0.1", "--port", "8188"],
            cwd=str(COMFY_DIR),
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        log(f"拉起失败：{type(e).__name__}: {e}")
        return False
    waited = 0
    while waited < wait_s:
        time.sleep(10)
        waited += 10
        if comfy.is_ready():
            log(f"ComfyUI 已恢复（{waited}s）")
            return True
    log(f"ComfyUI {wait_s}s 内未就绪")
    return False


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
        # priority 写在 metadata.wind-comic.priority（嵌套两层），单独抓数字即可
        pr = re.search(r"^[ \t]+priority:\s*(\d+)", fm, re.M)
        genres[name] = {"match": match, "body": body.strip(), "file": f.name,
                        "priority": int(pr.group(1)) if pr else 0}
    return genres


def pick_genre(script_text: str, genres: dict, forced: str = "") -> tuple:
    """按剧本内容匹配题材（forced 优先）。返回 (题材名, 镜头语言正文)。

    ⚠️ 踩坑（2026-09-18）：原先取 `hits[0]`，也就是**按文件遍历顺序取第一个命中**——
       剧本里随便出现一次「回忆 / 内心」就会被判成「内心独白」，
       于是一整部片子的运镜全变成「极慢推近，慢到几乎察觉不到」，主体也跟着不动。
       现在改成**按命中次数计分**（命中多的题材才配代表全片），priority 作同分时的次序。
    """
    if forced and forced in genres:
        return forced, genres[forced]["body"]
    hits = []
    for name, g in genres.items():
        pat = g.get("match") or ""
        if not pat:
            continue
        try:
            n = len(re.findall(pat, script_text, re.I))
        except re.error:
            continue
        if n:
            hits.append((n, g.get("priority", 0), name))
    if hits:
        hits.sort(key=lambda x: (-x[0], -x[1], x[2]))
        log("① 题材命中：" + "、".join(f"{n}×{name}" for n, _p, name in hits))
        return hits[0][2], genres[hits[0][2]]["body"]
    generic = genres.get("screenwriter")
    return ("screenwriter", generic["body"]) if generic else ("", "")


def _pick_char_refs(refs: dict, always: dict, text: str) -> list:
    """本镜该挂哪几张人物卡：**画面描述里点名的优先**。

    ⚠️ 踩坑（2026-09-18）：原先 always=True 的角色是**每一镜都挂**，于是只有莉莉丝的镜头
    也把爱丽丝的卡挂上。Ref2VA 的参考图是全程注意力——挂谁就像谁，
    既让主体被"钉"住不敢动，又让每张卡的 token 参与每一步采样（4~5 张时慢到 2 小时/镜）。
    现在改成：点名谁挂谁；一个都没点名（如「姐妹的亲密」）才退回 always 标的主角卡。
    """
    named = [v for k, v in refs.items() if k and k in text]
    if named:
        return named
    return [v for k, v in refs.items() if always.get(k)]


def _pick_scene_ref(scene_refs: dict, text: str) -> list:
    """本镜该挂哪张场景卡——**每镜只挂一张**。

    ⚠️ 踩坑（2026-09-18）：两张场景卡都标了 always，导致每一帧同时挂「古代石室」和
    「召唤法阵中央」两个背景参考——同一空间的两张卡在打架。
    场景名在画面描述里常只以片段出现（「石室中央」「法阵中央」），
    所以先整体匹配，再从长到短找 ≥2 字的子串，都不中才退回第一张常态场景卡。
    """
    if not scene_refs:
        return []
    for k, v in scene_refs.items():
        if k and k in text:
            return [v]
    for k, v in scene_refs.items():
        if not k:
            continue
        for n in range(len(k) - 1, 1, -1):
            for s in range(len(k) - n + 1):
                if k[s:s + n] in text:
                    return [v]
    return [next(iter(scene_refs.values()))]


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
            picked = storyboard.split_story(chunk, batch, ctx, local=(ai.BRAIN == "local"))
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
    ap.add_argument("--asset-provider", default="", choices=["", "local", "cloud", "chatgpt", "codex"],
                    help="资产出图引擎：local=NoobAI（无审查）/ codex=Codex 内置 image_gen（普通剧情首选，走订阅不花钱）/ cloud=豆包 Seedream / chatgpt=ChatGPT 网页版。默认按 nsfw 自动选")
    ap.add_argument("--char-candidates", type=int, default=2, help="每人物出几张候选（local 有效）")
    ap.add_argument("--assets-only", action="store_true", help="只出资产（人物卡/场景卡），先人工选定再出片")
    ap.add_argument("--pick", default="", help="选定要用的资产文件名（逗号分隔，如 \"莉莉丝_01.png,召唤石室_01.png\"）；不传则停在资产阶段等人挑")
    ap.add_argument("--no-ref", action="store_true", help="不用参考图（退回纯 t2v）")
    ap.add_argument("--brain", default="", choices=["", "codex", "cloud", "local"],
                    help="导演大脑：codex=走 ChatGPT 订阅 / cloud=DeepSeek / local=本地无审查。默认按文件后缀自动选（.md→codex，.txt→local）")
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

    # 大脑按用户约定自动选：.md（可读故事）走云端；.txt（禁读）走本地无审查
    brain = args.brain or ("codex" if script_path.suffix.lower() == ".md" else "local")
    ai.BRAIN = brain
    log(f"导演大脑：{brain}（{'云端订阅' if brain == 'codex' else 'DeepSeek' if brain == 'cloud' else '本地无审查'}）")
    work = OUTPUT / f"_director_{script_path.stem}"
    work.mkdir(parents=True, exist_ok=True)
    log(f"剧本载入（{len(script_text)} 字）｜工作目录 {work.name}")

    if brain == "local" and not ai.uncensored_ready():
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
    # ⚠️ 清单必须与资产图同目录（asset_dir），否则会出现「清单在 A、图在 B」的错配
    assets_file = assets_mod.asset_dir(script_path.stem) / "assets.json"
    manifest = {}
    if assets_file.exists():
        manifest = json.loads(assets_file.read_text(encoding="utf-8"))
        chars = len(manifest.get("characters", []))
        scenes = len(manifest.get("scenes", []))
        log(f"②.5 复用已有资产（{chars} 人物 / {scenes} 场景，provider={manifest.get('provider')}）")
    elif bible:
        # provider：无审查内容必须本地（云端会拦）；普通剧情默认云端（质量更优）
        provider = args.asset_provider or ("local" if not args.no_nsfw else "codex")
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
        log(f"✅ 只出资产：{assets_file.relative_to(OUTPUT)}（人工选定后用 --pick 指定再跑）")
        return

    # ---- 人工选定关卡（硬规则，代码强制）----
    # 参考图一旦定错，整片十几小时算力全废；形象挑选必须由人做。
    if args.pick:
        chosen = [x.strip() for x in args.pick.split(",") if x.strip()]
        available = [f for c in manifest.get("characters", []) for f in c.get("files", [])]
        available += [s["file"] for s in manifest.get("scenes", []) if s.get("file")]
        bad = [c for c in chosen if c not in available]
        if bad:
            log(f"❌ --pick 里有不存在的文件：{bad}")
            log("   可选清单见下方，或看资产目录")
            args.pick = ""
        else:
            manifest["selected"] = chosen
            assets_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"②.5 已记录选定：{chosen}")

    if manifest and not manifest.get("selected") and not args.no_ref:
        log("")
        log("⏸ 停在资产阶段——**请先挑形象**（参考图定错=整片算力白费）")
        for c in manifest.get("characters", []):
            for f in c.get("files", []):
                log(f"   人物「{c.get('name')}」候选：{Path(f).name}")
        for s in manifest.get("scenes", []):
            if s.get("file"):
                log(f"   场景「{s.get('name')}」候选：{Path(s['file']).name}")
        log("")
        log("👉 挑好后加参数重跑，例如：")
        log(f'   --pick "{Path(manifest["characters"][0]["files"][0]).name},'
            f'{Path(manifest["scenes"][0]["file"]).name if manifest.get("scenes") and manifest["scenes"][0].get("file") else ""}"')
        log(f"   资产目录：{assets_mod.asset_dir(script_path.stem)}")
        return

    # 参考图：选定的优先；没选定（--no-ref 场景）就不用
    selected = manifest.get("selected") or []
    char_refs = {}          # {角色名: comfy 文件名}
    char_always = {}        # {角色名: 是否每镜都带}
    scene_refs = {}         # {场景名: comfy 文件名}
    scene_always = {}       # {场景名: 是否每镜都带}
    if manifest and not args.no_ref:
        try:
            for c in manifest.get("characters", []):
                files = [f for f in (c.get("files") or []) if not selected or f in selected]
                if files:
                    key = c.get("name", "")
                    char_refs[key] = comfy.upload_image(OUTPUT / files[0])
                    char_always[key] = bool(c.get("always"))
            for s in manifest.get("scenes", []):
                if s.get("file") and (not selected or s["file"] in selected):
                    key = s.get("name", "")
                    scene_refs[key] = comfy.upload_image(OUTPUT / s["file"])
                    scene_always[key] = bool(s.get("always"))
            log(f"②.5 参考图就位：{len(char_refs)} 人物 + {len(scene_refs)} 场景 → Ref2VA 锁定"
                f"（标 always 的每镜都带，其余按镜头文本匹配）")
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ 参考图上传失败（{type(e).__name__}），退回 t2v")
            char_refs, scene_refs, char_always, scene_always = {}, {}, {}, {}

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
            txt = ai.h3_prompt(plain, "t2v", seconds, local=(brain == "local"), extra_rules=rules)
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
        # 本镜参考图 = 该镜涉及的人物卡 + 该镜所属场景卡
        # ⚠️ 只用镜头的「画面描述(scene)」匹配：narration/h3 提示词里含故事圣经连续性块
        #（列出全片所有人物），拿它们匹配会让人物卡误挂到每一镜。
        shot_scene = str(shot.get("scene", ""))
        shot_refs = _pick_char_refs(char_refs, char_always, shot_scene) + _pick_scene_ref(scene_refs, shot_scene)
        log(f"   本镜参考图 {len(shot_refs)} 张")
        if not h3_text:
            continue
        log(f"④ 第 {i + 1}/{len(shots)} 镜：出片中（{args.width}×{args.height}/{args.length}帧/{args.steps}步）…")
        ok = False
        for attempt in range(3):
            if not ensure_comfy():      # ComfyUI 崩了（多为 OOM 整进程消失）→ 自动拉起
                log("❌ ComfyUI 拉不起来，中止")
                break
            ok, _ = generate_h3_shot(
                task_id=f"director_{script_path.stem}_{i:02d}",
                prompt=h3_text, seed=20260915 + i + attempt * 1000,
                width=args.width, height=args.height, length=args.length,
                steps=args.steps, nsfw=not args.no_nsfw,
                ref_image_names=shot_refs or None,   # Ref2VA：人物全程 + 本镜场景，全程注意力锁定
                timeout=14400,  # 124 帧 + 多参考图实测约 2 小时/镜；给到 4 小时，
                                # ⚠️ 实测 7200s 会在成片前 ~1 分钟判死、误弃已完成的产物
            )
            if ok:
                break
            log(f"   第 {i + 1} 镜第 {attempt + 1} 次失败（多为 OOM），20 秒后换种子重试…")
            time.sleep(20)
        produced = OUTPUT / f"director_{script_path.stem}_{i:02d}.mp4"
        if ok and produced.exists():
            produced.replace(seg)
            segs.append(seg)
            log(f"✅ 第 {i + 1}/{len(shots)} 镜完成：{seg.name}")
        else:
            log(f"❌ 第 {i + 1}/{len(shots)} 镜 3 次均失败，跳过（可 --start {i} 续跑）")

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
