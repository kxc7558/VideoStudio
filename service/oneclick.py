# -*- coding: utf-8 -*-
"""一键成片（业务层）：编剧→拆镜→抽卡→逐镜审查→拼片的完整状态机。"""
import json
import random
import shutil
import threading
import time
import uuid
from pathlib import Path

import ai
import comfy
import storyboard
from db import tasks_store
from shared import ffmpeg_tools
from shared.paths import OUTPUT, JUBEN, UPLOADS
from service.generation import _generate_single, _finish_all_shots
from service.workflows import _build_workflow
def _pick_best_character_card(candidates):
    """人物抽卡自动选：用本地视觉模型描述候选首帧，选描述信息最全（人物最清晰）的一张。

    candidates: [(seg_id, video_path)]。返回选中的 seg_id；全失败返回 None。
    """
    best_id, best_score = None, -1
    for seg_id, video_path in candidates:
        frame_png = OUTPUT / f"{seg_id}_card.png"
        extract_last_frame(video_path, frame_png)
        desc = ai.describe_image(frame_png)
        score = len(desc)
        if score > best_score:
            best_score, best_id = score, seg_id
        frame_png.unlink(missing_ok=True)
    return best_id


def _resolve_character_anchor(task_id: str, character_image: str = "", seed: int = 0, width: int = 480, height: int = 832, steps: int = 20, style: str = "real", phase: str = "preview") -> str | None:
    """角色锚三来源解析（优先级：上传图 > 描述生成图 > 自动抽卡），返回锚帧路径名或 None。

    - character_image 非空：用户上传/从角色库选的定妆图，直接验证后当锚（跳过抽卡）。
      支持 uploads/ 或 output/_character_cards/ 下的文件名。
    - 否则走原有 4 候选抽卡（t2v 生成迷你视频抽尾帧，视觉模型选最佳）。
    锚选出后写任务 meta：character_card（锚来源 id）、card_desc（视觉模型描述，供全片前置）。
    """
    # 来源 B/C：外部锚帧（上传图或描述生成的角色卡）
    if character_image.strip():
        name = Path(character_image.strip()).name  # 防路径穿越
        for cand_dir in (OUTPUT / "_character_cards", UPLOADS):
            src = cand_dir / name
            if src.exists():
                anchor = OUTPUT / f"{task_id}_anchor.png"
                shutil.copyfile(src, anchor)
                card_desc = ai.describe_image(anchor) or ""
                _update(task_id, character_card=f"ext:{name}", card_desc=card_desc,
                        msg="已使用指定角色形象" + (f"（{card_desc[:40]}…）" if card_desc else ""))
                return anchor.name
        _update(task_id, msg=f"指定的角色图 {name} 不存在，改用自动抽卡")

    # 来源 A：自动抽卡（原逻辑）
    candidates = []
    first_prompt = shots_first_prompt(task_id)
    for k in range(4):
        if _cancelled(task_id):
            return None
        _update(task_id, msg=f"③ 人物抽卡 {k + 1}/4…")
        cand_id = f"{task_id}_card{k}"
        ok, vid = _generate_single(cand_id, "t2v", None, first_prompt, seed + 1000 + k, width, height, 33, steps, True, style)
        if ok:
            candidates.append((cand_id, vid))
    if not candidates:
        return None
    card_seg_id = _pick_best_character_card(candidates)
    card_desc = ""
    if card_seg_id:
        anchor_tmp = OUTPUT / f"{card_seg_id}_anchor.png"
        if not anchor_tmp.exists():
            extract_last_frame(OUTPUT / f"{card_seg_id}.mp4", anchor_tmp)
        card_desc = ai.describe_image(anchor_tmp)
    _update(task_id, character_card=card_seg_id, card_desc=card_desc)
    for cand_id, _ in candidates:
        if cand_id != card_seg_id:
            (OUTPUT / f"{cand_id}.mp4").unlink(missing_ok=True)
    return f"{card_seg_id}_anchor.png" if card_seg_id else None


def shots_first_prompt(task_id: str) -> str:
    """取任务分镜第一镜的提示词（抽卡用）。没有分镜数据时退回通用定妆提示词。"""
    shots = tasks.get(task_id, {}).get("shots", [])
    if shots:
        return str(shots[0].get("prompt", "")).strip() or "a woman standing, cinematic lighting, detailed face"
    return "a woman standing, cinematic lighting, detailed face"


def _run_oneclick_task(task_id, idea, style, width, height, length, steps, seed, script_text="", character_image=""):
    """一键成片阶段1：本地编剧写故事（或用自带剧本）→ 拆镜 → 落盘分镜 → 暂停等分镜审查。

    script_text 非空时跳过写故事（用户自带剧本，长剧本走进度窗口拆镜控上下文）。
    character_image 非空时用指定角色图当锚（三来源之一），否则审查阶段自动抽卡。
    审查通过后由 /api/review/storyboard 启动阶段2（_oc_stage2）。
    """
    try:
        NSFW_BATCH = 6        # 每批拆 6 镜
        NSFW_TARGET = 24      # 24 镜 ≈ 100 秒净长
        style_brief = {
            "anime": "全片 2D Japanese anime style，角色为动漫人物，画面是动漫赛璐璐质感",
            "real": "全片写实 live-action cinematic style，角色为真人",
        }.get(style, "写实 live-action cinematic style")

        # ① 本地写故事（提示词不外发）；有自带剧本则直接用
        if script_text.strip():
            story = script_text.strip()
            _update(task_id, state="running", msg=f"① 使用自带剧本（{len(story)} 字）")
        else:
            _update(task_id, state="running", msg="① 本地编剧正在把想法写成故事…")
            story = ai.uncensored_text(
                "你是短剧编剧。根据用户的想法创作一个完整连贯的成人向短剧故事，中文，1500 字以上，"
                f"分多个场景推进。风格要求：{style_brief}。直接输出故事正文，不要解释。",
                idea,
            )
        if not story:
            _update(task_id, state="error", msg="本地编剧失败（Ollama qwen3.8 未就绪？）")
            return

        # ② 分批拆镜（本地，不走云端）。长剧本用「进度窗口」切段落发，控 qwen3.8 上下文
        shots = []
        import storyboard as sb
        story_len = len(story)
        window = max(3000, story_len // 8)  # 每批发 ~1/8 剧本，足够拆一批镜且有前后文
        while len(shots) < NSFW_TARGET:
            if _cancelled(task_id):
                return
            done_ratio = min(0.92, len(shots) / NSFW_TARGET)
            start = int(story_len * done_ratio)
            end = min(story_len, start + window)
            if end <= start:
                break
            chunk = story[start:end]
            _update(task_id, msg=f"② 拆分镜 {len(shots)}/{NSFW_TARGET}（剧本进度 {start * 100 // max(1, story_len)}%~{end * 100 // max(1, story_len)}%）…")
            brief = (f"\n\n全片风格：{style_brief}。这是一部长剧本的连续拆镜任务，全片目标 {NSFW_TARGET} 镜，"
                     f"本批从剧本片段里拆出 {NSFW_BATCH} 个镜头，剧情按片段顺序推进，不要回头重复。\n"
                     "另外：给每个镜头加一个布尔字段 \"is_new_scene\"——该镜换了新地点/新时间/明显换了布景就是 true，"
                     "与上一镜同场景连续演出就是 false。这个字段决定该镜用文生视频还是画面接续生成。")
            try:
                batch = sb.split_story(chunk, NSFW_BATCH, brief, local=True)
            except Exception:  # noqa: BLE001
                batch = []
            if not batch:
                break
            for s in batch:
                s["id"] = len(shots) + 1
                shots.append(s)
        if len(shots) < 3:
            _update(task_id, state="error", msg="拆镜失败（本地模型未就绪或输出异常）")
            return

        # ③ 暂停：分镜审查（用户可对任意镜提意见 → qwen 按意见改写后继续）
        _update(task_id, state="awaiting_review", review_stage="storyboard",
                shots=shots, story_len=len(story),
                msg=f"分镜已拆好（{len(shots)} 镜），请审查：可对任意镜填写修改意见，或直接通过")
        # ④ 后台生成每镜预览图（迷你视频抽首帧），审查面板边生成边可看
        threading.Thread(
            target=_generate_shot_previews,
            args=(task_id, shots, style, width, height, seed, character_image),
            daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


def _generate_shot_previews(task_id, shots, style, width, height, seed, character_image=""):
    """分镜审查阶段的预览图后台生成：先解析角色锚（三来源），再每镜用「锚帧 i2v」出预览。

    锚驱动：所有预览从同一人物锚出发，审查时看到的人物从头到尾是同一个。
    只为审查提供画面参考，与正片生成无关（正片在审查通过后从头生成）。
    """
    # 角色锚（无锚时解析）：与正片同一套逻辑，锚选出后正片阶段直接复用
    card_seg_id = tasks.get(task_id, {}).get("character_card")
    if not card_seg_id:
        _resolve_character_anchor(task_id, character_image, seed, width, height, 20, style, phase="preview")
        card_seg_id = tasks.get(task_id, {}).get("character_card")
    if not card_seg_id:
        _update(task_id, msg="人物抽卡失败，预览图改用无锚模式")
        card_seg_id = None

    anchor = None
    if card_seg_id:
        if str(card_seg_id).startswith("ext:"):
            # 外部锚（上传图/角色卡）：锚文件是 {task_id}_anchor.png
            anchor = OUTPUT / f"{task_id}_anchor.png"
        else:
            anchor = OUTPUT / f"{card_seg_id}_anchor.png"
            if not anchor.exists() and (OUTPUT / f"{card_seg_id}.mp4").exists():
                extract_last_frame(OUTPUT / f"{card_seg_id}.mp4", anchor)

    for i, s in enumerate(shots):
        if _cancelled(task_id):
            return
        prev_png = OUTPUT / f"{task_id}_prev{i}.png"
        if prev_png.exists():
            continue
        p = str(s.get("prompt", "")).strip()
        prev_id = f"{task_id}_prev{i}"
        _update(task_id, msg=f"预览图生成中 {i + 1}/{len(shots)}（不影响审查操作）…")
        try:
            if anchor and anchor.exists():
                # 锚驱动：人物锚做首帧 i2v，人物一致
                wf = _build_workflow("wan", "i2v", comfy.upload_image(anchor), p, seed + 500 + i, 240, 416, 17, prev_id, 4, use_lora=True, style=style)
            else:
                wf = _build_workflow("wan", "t2v", None, p, seed + 500 + i, 240, 416, 17, prev_id, 4, use_lora=True, style=style)
            pid = comfy.submit(wf)
            ok, history = comfy.wait_done(pid, timeout=600)
            if not ok:
                continue
            vinfo = comfy.find_video(history)
            if not vinfo:
                continue
            tmp = OUTPUT / f"{prev_id}.mp4"
            comfy.download_video(vinfo, tmp)
            subprocess.run([FFMPEG, "-y", "-i", str(tmp), "-frames:v", "1", str(prev_png)],
                           capture_output=True, timeout=120)
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    _update(task_id, msg="预览图已全部生成（人物锚统一），审查面板可直接看画面")


def _oc_stage2(task_id, shots, style, width, height, length, steps, seed, force_recard=False, character_image=""):
    """一键成片阶段2：角色锚解析(三来源，如无锚) → 生成「当前镜」→ 停在逐镜审查（awaiting_shot）。

    逐镜审查流：每镜生成完暂停，用户通过才生成下一镜；全部通过后拼片进成片审查。
    当前镜号存任务 meta 的 cur_shot（0 起）。
    """
    try:
        with _lock:
            tt = tasks.setdefault(task_id, {})
            cur = tt.setdefault("cur_shot", 0)
        # 角色锚：只在还没有锚时解析（首镜前）
        card_seg_id = tasks.get(task_id, {}).get("character_card")
        if not card_seg_id:
            anchor_name = _resolve_character_anchor(task_id, character_image, seed, width, height, steps, style, phase="main")
            card_seg_id = tasks.get(task_id, {}).get("character_card")
            if not card_seg_id:
                _update(task_id, state="error", msg="人物抽卡全部失败，请重试")
                return

        # 生成「当前镜」：模式由场景标注决定（剧情自由度 > 画面接续）
        #   新场景镜（is_new_scene=true）→ t2v：剧情完全由提示词驱动，锚不绑架画面
        #   同场景镜（false）→ 上一镜尾帧 i2v：画面自然接续
        # 人物一致性统一靠 card_desc 前置（两种模式都带）。
        if cur >= len(shots):
            _finish_all_shots(task_id, shots)
            return
        s = shots[cur]
        p = str(s.get("prompt", "")).strip()
        card_desc = tasks.get(task_id, {}).get("card_desc", "")
        if card_desc:
            p = f"same character as before: {card_desc}. {p}"
        is_new_scene = bool(s.get("is_new_scene", cur == 0))  # 首镜或标注新场景
        _update(task_id, state="running", msg=f"生成第 {cur + 1}/{len(shots)} 镜（{'新场景·文生' if is_new_scene else '同场景·接续'}）…")
        ok = False
        if not is_new_scene and cur > 0:
            # 同场景：上一镜尾帧做首帧（画面接续）
            prev_seg = OUTPUT / f"{task_id}_s{cur - 1}.mp4"
            bridge_png = OUTPUT / f"{task_id}_bridge{cur}.png"
            extract_last_frame(prev_seg, bridge_png)
            ok, _ = _generate_single(f"{task_id}_s{cur}", "i2v", comfy.upload_image(bridge_png), p, seed + cur, width, height, length, steps, True, style)
            bridge_png.unlink(missing_ok=True)
        else:
            # 新场景（或首镜）：锚帧只保人物，剧情走 t2v
            ok, _ = _generate_single(f"{task_id}_s{cur}", "t2v", None, p, seed + cur, width, height, length, steps, True, style)
        if not ok:
            _update(task_id, state="awaiting_shot", cur_shot=cur,
                    msg=f"第 {cur + 1} 镜生成失败，可重抽或跳过")
            return
        # 停在逐镜审查
        _update(task_id, state="awaiting_shot", cur_shot=cur,
                msg=f"第 {cur + 1}/{len(shots)} 镜已生成，请审查：通过→下一镜；重抽→换一版")
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


