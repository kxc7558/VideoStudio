# -*- coding: utf-8 -*-
"""生成执行（业务层）：单段/长视频/拼片的编排。任务数据经 db 层读写。"""
import json
import threading
from pathlib import Path

import ai
import comfy
from db import tasks_store
from shared import ffmpeg_tools
from shared.paths import OUTPUT, WORKFLOWS
from service.workflows import _build_workflow, NSFW_LORA, ANIME_LORA, MODEL_VARIANT
def _cancelled(task_id: str) -> bool:
    return bool(tasks.get(task_id, {}).get("cancelled"))


def _run_task(task_id, model, mode, image_name, prompt, seed, width, height, length, steps, last_image_name=None, first_local_path=None, last_local_path=None, nsfw=False, style="real"):
    try:
        if _cancelled(task_id):
            return
        _update(task_id, state="running", msg="正在生成，请稍候…")
        seconds = length / 24.0 if model == "h3" else 0.0
        # 指定首尾帧：先让本地视觉模型看首尾两帧，再写过渡提示词（H3 FL2VA 官方格式，尽力而为）
        # nsfw=True 时走本地 uncensored 模型（无审查出片不碰云端 DeepSeek）。
        if last_image_name and first_local_path and last_local_path:
            _update(task_id, msg="正在分析首尾帧，编写过渡提示词…")
            first_desc = ai.describe_image(first_local_path)
            last_desc = ai.describe_image(last_local_path)
            if first_desc and last_desc:
                t = ai.transition_prompt(first_desc, last_desc, prompt, seconds, local=nsfw)
                if t:
                    _update(task_id, ai_prompts=[{"segment": 1, "original": prompt, "rewritten": t}])
                    prompt = t
        elif model == "h3" and not nsfw:
            # 单段 H3：把普通提示词按 H3 官方格式改写（尽力而为，失败沿用原提示词）
            _update(task_id, msg="正在按 MiniMax H3 官方格式改写提示词…")
            t = ai.h3_prompt(prompt, mode, seconds)
            if t:
                _update(task_id, ai_prompts=[{"segment": 1, "original": prompt, "rewritten": t}])
                prompt = t
        wf = _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps, last_image_name, use_lora=nsfw, style=style)
        prompt_id = comfy.submit(wf)
        if _cancelled(task_id):
            comfy.cancel(prompt_id)
            return
        _update(task_id, prompt_id=prompt_id, msg="已提交到生成队列…")
        ok, history = comfy.wait_done(prompt_id, should_cancel=lambda: _cancelled(task_id))
        if _cancelled(task_id):
            return
        if not ok:
            _update(task_id, state="error", msg="生成失败或超时，请重试")
            return
        video = comfy.find_video(history)
        if not video:
            _update(task_id, state="error", msg="未找到生成的视频")
            return
        dest = OUTPUT / f"{task_id}.mp4"
        comfy.download_video(video, dest)
        _update(task_id, state="done", msg="完成", video=dest.name)
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


def _run_long_task(task_id, model, segments, width, height, length, steps, seed, bridge=False, nsfw=False, style="real", review_each=False, keep_existing=False):
    """长视频：按 segments 逐段生成，段间「尾帧→下一段首帧」接续，最后拼接成一条。

    bridge=True 时（故事模式），每段完成后用本地视觉模型回看实际尾帧，再重写下一段提示词，
    让剧情踩在真实画面上发展、不漂移。
    nsfw=True 时：重写走本地 uncensored 模型（不碰云端），且 workflow 注入无审查 LoRA。
    review_each=True：每镜完成后记录段产物（审查面板展示），支持 resampled 标记换 seed 重抽；
    拼接后不直接完成，进入成片审查（awaiting_review/final）。
    keep_existing=True：mode="keep" 的段跳过生成，直接用磁盘上的已有段视频（审查重拼用）。
    """
    seg_videos = []
    ai_prompts = []
    seconds = length / 24.0 if model == "h3" else 0.0
    # H3 官方格式：第一段（首镜头）先改写成官方格式；后续段在 bridge 里改写（尽力而为）。
    # nsfw 无审查时跳过（官方格式是全年龄向措辞，与无审查链路不搭）。
    if model == "h3" and not nsfw and segments:
        orig0 = segments[0]["prompt"]
        t0 = ai.h3_prompt(orig0, segments[0]["mode"], seconds)
        if t0:
            segments[0]["prompt"] = t0
            ai_prompts.append({"segment": 1, "original": orig0, "rewritten": t0})
            _update(task_id, ai_prompts=ai_prompts)
    try:
        for i, seg in enumerate(segments):
            if _cancelled(task_id):
                return
            seg_id = f"{task_id}_s{i}"
            # 审查重拼：mode="keep" 的段直接复用磁盘上的旧视频
            if keep_existing and seg.get("mode") == "keep":
                existing = OUTPUT / seg.get("keep_video", f"{seg_id}.mp4")
                if existing.exists():
                    seg_videos.append(existing)
                    continue
                # 旧段丢了就退回正常生成（t2v 兜底）
                seg = {"mode": "t2v", "image": None, "prompt": seg.get("prompt", "")}
            # 用户审查标记的重抽：换 seed 重试（标记由 /api/review/resample 写入 resampled 列表）
            cur_seed = seed + i
            resampled = tasks.get(task_id, {}).get("resampled", [])
            if review_each and i in resampled:
                cur_seed = seed + i + 10000 * (len(resampled) + 1)  # 换种子重抽
                _update(task_id, msg=f"第 {i + 1} 镜按审查意见重抽…")
            attempt = 0
            while True:
                wf = _build_workflow(model, seg["mode"], seg.get("image"), seg["prompt"],
                                     cur_seed, width, height, length, seg_id, steps,
                                     use_lora=nsfw, style=style)
                pid = comfy.submit(wf)
                _update(task_id, state="running", prompt_id=pid,
                        msg=f"第 {i + 1}/{len(segments)} 段生成中…")
                if _cancelled(task_id):
                    comfy.cancel(pid)
                    return
                ok, history = comfy.wait_done(pid, should_cancel=lambda: _cancelled(task_id))
                if _cancelled(task_id):
                    return
                if ok and comfy.find_video(history):
                    break
                attempt += 1
                if attempt >= 2:
                    _update(task_id, state="error", msg=f"第 {i + 1} 段生成失败或超时")
                    return
                cur_seed += 1  # 自动重试换 seed
            video = comfy.find_video(history)
            if not video:
                _update(task_id, state="error", msg=f"第 {i + 1} 段未找到视频")
                return
            seg_dest = OUTPUT / f"{seg_id}.mp4"
            comfy.download_video(video, seg_dest)
            seg_videos.append(seg_dest)
            # 段产物记录（审查面板展示每镜视频 + 重抽/换锚入口）
            if review_each:
                with _lock:
                    t = tasks.setdefault(task_id, {})
                    seg_meta = t.setdefault("seg_meta", {})
                    seg_meta[str(i)] = {"video": seg_dest.name, "prompt": seg["prompt"]}
                    _write_meta(task_id, t)
            # 抽尾帧，作为下一段首帧（i2v 接续）
            if i < len(segments) - 1:
                frame_png = OUTPUT / f"{seg_id}_last.png"
                extract_last_frame(seg_dest, frame_png)
                segments[i + 1]["mode"] = "i2v"
                segments[i + 1]["image"] = comfy.upload_image(frame_png)
                # 尾帧回看：看实际结尾 → 重写下一段提示词（尽力而为，失败沿用原提示词）
                if bridge:
                    _update(task_id, msg=f"第 {i + 1}/{len(segments)} 段完成，正在分析结尾画面、接续下一段…")
                    prev_desc = ai.describe_image(frame_png)
                    original = segments[i + 1]["prompt"]
                    if model == "h3":
                        # H3：优先「回看尾帧→按官方格式接续」，失败退到纯官方格式改写（都输出官方格式）
                        bridged = ai.bridge_next_prompt(prev_desc, original, "h3", seconds, local=nsfw) if prev_desc else ""
                        if not bridged:
                            bridged = ai.h3_prompt(original, "i2v", seconds)
                    else:
                        bridged = ai.bridge_next_prompt(prev_desc, original, local=nsfw) if prev_desc else ""
                    if bridged:
                        segments[i + 1]["prompt"] = bridged
                        ai_prompts.append({"segment": i + 2, "original": original, "rewritten": bridged})
                        _update(task_id, ai_prompts=ai_prompts)
                # 尾帧只是段间临时素材，用完删掉，避免 output 里越攒越多
                frame_png.unlink(missing_ok=True)
        # 拼接所有段成一条长视频
        dest = OUTPUT / f"{task_id}.mp4"
        concat_videos(seg_videos, dest)
        if not review_each:
            for v in seg_videos:
                v.unlink(missing_ok=True)
        # review_each 模式：段视频保留（供单镜重抽/换锚），进入成片审查等待
        if review_each:
            _update(task_id, state="awaiting_review", review_stage="final",
                    msg="成片已出，请审查；可单镜重抽/换锚/自动检查修复，或点「通过」完成")
        else:
            _update(task_id, state="done", msg="完成", video=dest.name)
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


# ---- 一键成片：故事 → 本地写剧本 → 拆镜 → 人物抽卡锚定 → 逐镜生成 → 拼片 ----
# 极简 UI 背后的全自动管道。用户只给一句话/一段故事，其余全自动，内容只在本地流转。


def _generate_single(task_id, mode, image_name, prompt, seed, width, height, length, steps, nsfw, style="real"):
    """生成单个片段并下载到 output/{task_id}.mp4，返回 (成功?, 视频路径)。供抽卡复用。"""
    wf = _build_workflow("wan", mode, image_name, prompt, seed, width, height, length, task_id, steps, use_lora=nsfw, style=style)
    pid = comfy.submit(wf)
    ok, history = comfy.wait_done(pid, should_cancel=lambda: _cancelled(task_id))
    if not ok:
        return False, None
    video = comfy.find_video(history)
    if not video:
        return False, None
    dest = OUTPUT / f"{task_id}.mp4"
    comfy.download_video(video, dest)
    return True, dest


def _finish_all_shots(task_id, shots):
    """全部镜头通过后：把各段按顺序拼接成片，进成片审查（awaiting_review/final）。"""
    seg_videos = [OUTPUT / f"{task_id}_s{i}.mp4" for i in range(len(shots))]
    missing = [i for i, v in enumerate(seg_videos) if not v.exists()]
    if missing:
        _update(task_id, state="error", msg=f"第 {[m + 1 for m in missing]} 镜缺失，无法拼接")
        return
    dest = OUTPUT / f"{task_id}.mp4"
    concat_videos(seg_videos, dest)
    _update(task_id, state="awaiting_review", review_stage="final",
            msg="全镜通过，已拼片。成片审查：重抽/重跑抽卡/自动检查修复，或通过完成")


