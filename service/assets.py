# -*- coding: utf-8 -*-
"""资产生成（业务层）：人物卡 / 场景卡，供出片时做「参考图全程注意力」锁定一致性。

三 provider：
- **cloud**：豆包 Seedream（云端，质量最优）——仅普通剧情，成人内容会被平台拦。
- **chatgpt**：ChatGPT 网页版（用订阅额度，不花 API 钱）——仅普通剧情；首次需 `node scripts/chatgpt-image.js --login` 登录一次。
- **local**：NoobAI-XL（本地 SDXL 动漫）——**无审查内容用**，中文描述先转英文 tag。

产物落盘 `output/_assets_<剧本名>/{characters,scenes}/`，人工选定后由导演管线复用。
"""
import json
import random
from pathlib import Path

import ai
import comfy
from service import character as char_mod
from service import doubao
from shared.paths import BASE, OUTPUT

# 云端提示词模板（中文友好）
_CLOUD_CHAR_SUFFIX = "，人物设定图，正面半身立绘，全身服装可见，五官清晰，纯色简洁背景，高质量"
_CLOUD_SCENE_SUFFIX = "，场景概念图，无人物，宽画幅，电影感光线，高质量"

# 本地 SDXL 提示词模板（英文 tag，NoobAI 训练分布）
_LOCAL_CHAR_SUFFIX = ("masterpiece, best quality, very aesthetic, character reference sheet, "
                      "solo, full body, standing, clear face, plain background")
_LOCAL_SCENE_SUFFIX = ("masterpiece, best quality, very aesthetic, no humans, scenery, "
                       "wide shot, cinematic lighting, detailed environment")


def asset_dir(script_stem: str) -> Path:
    """资产目录（按剧本名隔离，可复用）。"""
    d = OUTPUT / f"_assets_{script_stem}"
    (d / "characters").mkdir(parents=True, exist_ok=True)
    (d / "scenes").mkdir(parents=True, exist_ok=True)
    return d


def char_prompt_from_bible(c: dict) -> str:
    """把故事圣经的人物条目拼成出图描述。"""
    parts = [c.get("name", ""), c.get("identity", ""), c.get("appearance", ""), c.get("wardrobe", "")]
    return "，".join(p for p in parts if p)


def scene_prompt_from_bible(s: dict) -> str:
    """把故事圣经的场景条目拼成出图描述。"""
    parts = [s.get("name", ""), s.get("place", ""), s.get("lighting", ""), s.get("palette", "")]
    return "，".join(p for p in parts if p)


def to_english_tags(desc: str) -> str:
    """中文描述 → 英文 SDXL tag（本地出图用；走本地 qwen3.8，不外发）。失败返回原描述。

    keep_alive=0：翻译完立即卸载 LLM——紧接着就要用 ComfyUI 出图，显存要留给它。
    """
    out = ai.uncensored_text(
        "把用户给的动漫人物/场景中文描述翻译成英文 Stable Diffusion 提示词标签。"
        "只输出英文 tag，逗号分隔，不要解释、不要引号。保留所有外貌细节（发色/发型/体型/服装颜色）。",
        desc,
        max_tokens=300,
        keep_alive=0,
    )
    return out.strip() if out else desc


CHATGPT_BRIDGE = BASE / "scripts" / "chatgpt-image.js"


def _download(url: str, dest: Path) -> bool:
    return doubao.download_image(url, dest)


def chatgpt_image(prompt: str, dest: Path, timeout: int = 420) -> dict:
    """调 Node 桥用 ChatGPT 网页版出图（订阅额度）。首次需先 --login 登录一次。

    返回 {"ok": bool, "msg": str}。浏览器为有头模式（无头会被 Cloudflare 403）。
    """
    import subprocess
    if not CHATGPT_BRIDGE.exists():
        return {"ok": False, "msg": f"桥脚本不存在：{CHATGPT_BRIDGE}"}
    try:
        r = subprocess.run(
            ["node", str(CHATGPT_BRIDGE), "--prompt", prompt, "--out", str(dest)],
            capture_output=True, text=True, timeout=timeout, cwd=str(BASE),
            encoding="utf-8", errors="ignore",
        )
        for line in reversed((r.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    d = json.loads(line)
                    return {"ok": bool(d.get("ok")), "msg": str(d.get("msg", ""))[:160]}
                except Exception:
                    continue
        return {"ok": False, "msg": (r.stdout or r.stderr or "桥无输出")[-160:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": f"ChatGPT 出图超时（{timeout}s）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"[:160]}


def generate_character_asset(name: str, desc: str, provider: str, out_dir: Path, candidates: int = 2) -> dict:
    """生成某个人物的角色卡。

    cloud：豆包 Seedream 出一张（云端按张计费/占额度，故候选少）
    local：NoobAI-XL 出 candidates 张候选
    返回 {"name", "desc", "files": [相对路径], "provider"}
    """
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or f"char{random.randint(1000, 9999)}"
    files = []
    if provider == "chatgpt":
        dest = out_dir / "characters" / f"{safe}_01.png"
        r = chatgpt_image(f"{desc}{_CLOUD_CHAR_SUFFIX}", dest)
        if r["ok"] and dest.exists():
            files.append(str(dest.relative_to(OUTPUT)))
        else:
            return {"name": name, "desc": desc, "files": [], "provider": provider, "error": r["msg"]}
    elif provider == "cloud":
        r = doubao.generate_image(f"{desc}{_CLOUD_CHAR_SUFFIX}")
        if r["ok"]:
            dest = out_dir / "characters" / f"{safe}_01.png"
            if _download(r["url"], dest):
                files.append(str(dest.relative_to(OUTPUT)))
        else:
            return {"name": name, "desc": desc, "files": [], "provider": provider, "error": r["msg"][:120]}
    else:
        tags = to_english_tags(desc)
        prompt = f"{tags}, {_LOCAL_CHAR_SUFFIX}"
        wf = char_mod.build_card_workflow(prompt, seed=random.randint(1, 2**31 - 1), batch=candidates)
        pid = comfy.submit(wf)
        ok, history = comfy.wait_done(pid, timeout=900)
        if ok:
            imgs = char_mod.collect_images(history)
            for i, img in enumerate(imgs[:candidates], 1):
                dest = out_dir / "characters" / f"{safe}_{i:02d}.png"
                if comfy.download_binary(img, dest):
                    files.append(str(dest.relative_to(OUTPUT)))
    return {"name": name, "desc": desc, "files": files, "provider": provider}


def generate_scene_asset(name: str, desc: str, provider: str, out_dir: Path) -> dict:
    """生成场景参考图（无人物，纯环境）。"""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or f"scene{random.randint(1000, 9999)}"
    dest = out_dir / "scenes" / f"{safe}_01.png"
    if provider == "chatgpt":
        r = chatgpt_image(f"{desc}{_CLOUD_SCENE_SUFFIX}", dest)
        return {"name": name, "desc": desc, "file": str(dest.relative_to(OUTPUT)) if r["ok"] and dest.exists() else "",
                "provider": provider, "error": "" if r["ok"] else r["msg"]}
    if provider == "cloud":
        r = doubao.generate_image(f"{desc}{_CLOUD_SCENE_SUFFIX}")
        if not r["ok"]:
            return {"name": name, "desc": desc, "file": "", "provider": provider, "error": r["msg"][:120]}
        ok = _download(r["url"], dest)
    else:
        tags = to_english_tags(desc)
        wf = char_mod.build_card_workflow(f"{tags}, {_LOCAL_SCENE_SUFFIX}",
                                          seed=random.randint(1, 2**31 - 1), batch=1,
                                          width=char_mod.CARD_HEIGHT, height=char_mod.CARD_WIDTH)  # 横版场景
        pid = comfy.submit(wf)
        ok, history = comfy.wait_done(pid, timeout=900)
        imgs = char_mod.collect_images(history) if ok else []
        ok = comfy.download_binary(imgs[0], dest) if imgs else False
    return {"name": name, "desc": desc, "file": str(dest.relative_to(OUTPUT)) if ok else "",
            "provider": provider}


def build_all(bible: dict, script_stem: str, provider: str = "local", char_candidates: int = 2) -> dict:
    """按故事圣经批量出资产：全部人物 + 全部场景。返回清单（落盘 assets.json）。"""
    d = asset_dir(script_stem)
    manifest = {"provider": provider, "characters": [], "scenes": []}

    for c in bible.get("characters", []):
        name = c.get("name", "")
        desc = char_prompt_from_bible(c)
        if not desc:
            continue
        item = generate_character_asset(name, desc, provider, d, char_candidates)
        manifest["characters"].append(item)

    for s in bible.get("scenes", []):
        name = s.get("name", "")
        desc = scene_prompt_from_bible(s)
        if not desc:
            continue
        manifest["scenes"].append(generate_scene_asset(name, desc, provider, d))

    (d / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def pick_reference(manifest: dict, kind: str, index: int = 0) -> str:
    """从资产清单取参考图（选定后传给出片）。kind: characters|scenes。"""
    items = manifest.get(kind, [])
    if not items:
        return ""
    it = items[min(index, len(items) - 1)]
    if kind == "characters":
        files = it.get("files") or []
        return files[0] if files else ""
    return it.get("file", "")
