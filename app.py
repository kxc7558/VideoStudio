# -*- coding: utf-8 -*-
"""出片台 —— 面向小白的一键出视频客户端后端，封装 ComfyUI 的 I2V/T2V。"""
import json
import random
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import comfy
import storyboard

BASE = Path(__file__).parent
WORKFLOWS = BASE / "workflows"
OUTPUT = BASE / "output"
UPLOADS = BASE / "uploads"
OUTPUT.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="出片台")
app.mount("/web", StaticFiles(directory=BASE / "web"), name="web")

# 启动后连 ComfyUI 的 WebSocket，实时收采样进度（引擎没起时会自动反复重连，无副作用）
comfy.start_progress_listener()

tasks = {}
_lock = threading.Lock()


def _reconcile_stale_tasks():
    """重启后把上次遗留的 queued/running 任务标记为中断（避免一直显示「进行中」）。"""
    for f in OUTPUT.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("state") in ("queued", "running"):
            meta["state"] = "error"
            meta["msg"] = "上次运行被重启打断"
            try:
                f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


_reconcile_stale_tasks()

# 分辨率预设（中文标签 -> (宽, 高)）
RESOLUTIONS = {
    "竖屏 9:16": (480, 832),
    "方屏 1:1": (640, 640),
    "横屏 16:9": (832, 480),
}
# 时长预设（中文标签 -> 帧数 length，Wan 要求 4n+1）
DURATIONS = {
    "短 · 约2秒": 33,
    "中 · 约3秒": 49,
    "长 · 约5秒": 81,
}
# MiniMax H3 的时长帧数（24fps，17k+5 网格：56/73/124 分别约 2.3/3.0/5.2 秒）
DURATIONS_H3 = {
    "短 · 约2秒": 56,
    "中 · 约3秒": 73,
    "长 · 约5秒": 124,
}
# 可切换的模型：wan（Wan 2.2 双专家）/ h3（MiniMax H3 单模型）
MODELS = ("wan", "h3")
# 文生视频步数档位（前端直接传步数数字）。图生视频固定 4 步（LightX2V 蒸馏锁死），不接受自选。
MAX_STEPS = 50

DEFAULT_PROMPT = {
    "i2v": "画面自然流畅地动起来，动作连贯，镜头缓慢推进，高画质，细节丰富",
    "t2v": "电影级画质，细节丰富，画面自然流畅，镜头缓慢推进",
}

# 拼接视频用的 ffmpeg（复用 ComfyUI 自带的二进制，避免再下载）
FFMPEG = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"


def concat_videos(paths: list, out: Path) -> Path:
    """按顺序把多个 mp4 拼接成一个（重新编码，保证参数一致）。"""
    list_file = out.with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in paths), encoding="utf-8"
    )
    try:
        subprocess.run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)],
            capture_output=True, timeout=1800,
        )
    finally:
        list_file.unlink(missing_ok=True)
    return out


def extract_last_frame(video_path: Path, out_png: Path) -> Path:
    """用 ffmpeg 抽视频最后一帧存成 png，供下一段当首帧（首尾帧接续）。"""
    subprocess.run(
        [FFMPEG, "-y", "-sseof", "-0.2", "-i", str(video_path),
         "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=300,
    )
    return out_png


def _meta_dict(task_id: str, t: dict) -> dict:
    """把内存任务转成要落盘的档案。"""
    return {
        "task_id": task_id,
        "mode": t.get("mode", ""),
        "model": t.get("model", ""),
        "prompt": t.get("prompt", ""),
        "resolution": t.get("resolution", ""),
        "duration": t.get("duration", ""),
        "steps": t.get("steps"),
        "seed": t.get("seed"),
        "state": t.get("state", ""),
        "msg": t.get("msg", ""),
        "video": t.get("video", ""),
        "created": t.get("created", 0),
        "updated": t.get("updated", 0),
    }


def _write_meta(task_id: str, t: dict):
    """把任务档案写到磁盘（重启不丢）；失败不抛出，避免拖垮生成流程。"""
    try:
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / f"{task_id}.json").write_text(
            json.dumps(_meta_dict(task_id, t), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _update(task_id: str, **kw):
    with _lock:
        t = tasks.setdefault(task_id, {})
        t.update(kw)
        t["updated"] = int(time.time())
        _write_meta(task_id, t)


def _build_wan_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps):
    """Wan 2.2 工作流：i2v 用 LightX2V 4 步双专家，t2v 用双专家对半切步数。"""
    wf = json.loads((WORKFLOWS / f"{mode}_api.json").read_text(encoding="utf-8"))
    prefix = f"video/{task_id}"
    if mode == "i2v":
        wf["1"]["inputs"]["image"] = image_name
        wf["3"]["inputs"]["text"] = prompt
        wf["10"]["inputs"].update({"width": width, "height": height, "length": length})
        wf["11"]["inputs"]["noise_seed"] = seed
        wf["12"]["inputs"]["noise_seed"] = seed
        wf["15"]["inputs"]["filename_prefix"] = prefix
    else:  # t2v
        # 双专家：高噪声专家跑前半段、低噪声专家跑后半段，步数对半切。
        half = steps // 2
        wf["2"]["inputs"]["text"] = prompt
        wf["9"]["inputs"].update({"width": width, "height": height, "length": length})
        wf["10"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": 0, "end_at_step": half})
        wf["11"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": half, "end_at_step": steps})
        wf["14"]["inputs"]["filename_prefix"] = prefix
    return wf


def _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps):
    """MiniMax H3 工作流：单模型（FL2VA）同时支持 t2v 与 i2v（首帧）。"""
    wf = json.loads((WORKFLOWS / f"h3_{mode}_api.json").read_text(encoding="utf-8"))
    wf["4"]["inputs"].update({"prompt": prompt, "width": width, "height": height, "length": length})
    wf["5"]["inputs"]["noise_seed"] = seed
    wf["8"]["inputs"]["steps"] = steps
    wf["12"]["inputs"]["filename_prefix"] = f"video/{task_id}"
    if mode == "i2v":
        wf["0"]["inputs"]["image"] = image_name
    return wf


def _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps=20):
    if model == "h3":
        return _build_h3_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps)
    return _build_wan_workflow(mode, image_name, prompt, seed, width, height, length, task_id, steps)


def _cancelled(task_id: str) -> bool:
    return bool(tasks.get(task_id, {}).get("cancelled"))


def _run_task(task_id, model, mode, image_name, prompt, seed, width, height, length, steps):
    try:
        if _cancelled(task_id):
            return
        _update(task_id, state="running", msg="正在生成，请稍候…")
        wf = _build_workflow(model, mode, image_name, prompt, seed, width, height, length, task_id, steps)
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


def _run_long_task(task_id, model, segments, width, height, length, steps, seed):
    """长视频：按 segments 逐段生成，段间「尾帧→下一段首帧」接续，最后拼接成一条。"""
    seg_videos = []
    try:
        for i, seg in enumerate(segments):
            if _cancelled(task_id):
                return
            seg_id = f"{task_id}_s{i}"
            wf = _build_workflow(model, seg["mode"], seg.get("image"), seg["prompt"],
                                 seed + i, width, height, length, seg_id, steps)
            pid = comfy.submit(wf)
            _update(task_id, state="running", prompt_id=pid,
                    msg=f"第 {i + 1}/{len(segments)} 段生成中…")
            if _cancelled(task_id):
                comfy.cancel(pid)
                return
            ok, history = comfy.wait_done(pid, should_cancel=lambda: _cancelled(task_id))
            if _cancelled(task_id):
                return
            if not ok:
                _update(task_id, state="error", msg=f"第 {i + 1} 段生成失败或超时")
                return
            video = comfy.find_video(history)
            if not video:
                _update(task_id, state="error", msg=f"第 {i + 1} 段未找到视频")
                return
            seg_dest = OUTPUT / f"{seg_id}.mp4"
            comfy.download_video(video, seg_dest)
            seg_videos.append(seg_dest)
            # 抽尾帧，作为下一段首帧（i2v 接续）
            if i < len(segments) - 1:
                frame_png = OUTPUT / f"{seg_id}_last.png"
                extract_last_frame(seg_dest, frame_png)
                segments[i + 1]["mode"] = "i2v"
                segments[i + 1]["image"] = comfy.upload_image(frame_png)
        # 拼接所有段成一条长视频
        dest = OUTPUT / f"{task_id}.mp4"
        concat_videos(seg_videos, dest)
        for v in seg_videos:
            v.unlink(missing_ok=True)
        _update(task_id, state="done", msg="完成", video=dest.name)
    except Exception as e:  # noqa: BLE001
        if _cancelled(task_id):
            return
        _update(task_id, state="error", msg=f"出错：{e}")


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"comfy": comfy.is_ready(), "t2v_ready": comfy.t2v_ready(), "h3_ready": comfy.h3_ready()}


@app.get("/api/tasks")
def list_tasks():
    """列出所有任务：从磁盘档案读（重启不丢），合并内存里进行中的最新状态。"""
    result = {}
    for f in OUTPUT.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            if meta.get("task_id"):
                result[meta["task_id"]] = meta
        except Exception:
            continue
    with _lock:
        for tid, t in tasks.items():
            result[tid] = _meta_dict(tid, t)
    items = list(result.values())
    # 进行中的排最前，其余按更新时间倒序（新在前）
    items.sort(key=lambda x: (0 if x.get("state") in ("queued", "running") else 1,
                              -(x.get("updated") or 0)))
    return {"tasks": items}


@app.post("/api/generate")
async def generate(
    model: str = Form("wan"),
    mode: str = Form(...),
    prompt: str = Form(""),
    resolution: str = Form("方屏 1:1"),
    duration: str = Form("长 · 约5秒"),
    seed: int = Form(-1),
    steps: int = Form(20),
    segments: int = Form(1),
    image: UploadFile = File(None),
):
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if model not in MODELS:
        return JSONResponse({"error": "未知模型"}, 400)
    if mode not in ("i2v", "t2v"):
        return JSONResponse({"error": "未知模式"}, 400)
    if model == "h3":
        if not comfy.h3_ready():
            return JSONResponse({"error": "MiniMax H3 模型还没就绪（可能还在下载，或引擎需重启）"}, 503)
    elif mode == "t2v" and not comfy.t2v_ready():
        return JSONResponse({"error": "文生视频模型还在下载中，暂不可用"}, 503)

    width, height = RESOLUTIONS.get(resolution, (640, 640))
    length = (DURATIONS_H3 if model == "h3" else DURATIONS).get(duration, 124 if model == "h3" else 81)
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)

    # 步数：Wan 文生视频要双专家对半切（保证偶数），其余直接夹到 [4, MAX_STEPS]。
    steps = max(4, min(int(steps), MAX_STEPS))
    if model == "wan" and mode == "t2v" and steps % 2:
        steps += 1

    task_id = uuid.uuid4().hex[:12]

    image_name = None
    if image is not None:
        raw = await image.read()
        ext = Path(image.filename or "x.png").suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return JSONResponse({"error": "请上传 png / jpg / webp 图片"}, 400)
        local = UPLOADS / f"{task_id}{ext}"
        local.write_bytes(raw)
        image_name = comfy.upload_image(local)

    if mode == "i2v" and not image_name:
        return JSONResponse({"error": "图生视频需要先上传一张图片"}, 400)
    if not prompt.strip():
        prompt = DEFAULT_PROMPT[mode]

    _update(
        task_id,
        state="queued", msg="排队中…",
        mode=mode, model=model, seed=seed, steps=steps,
        prompt=prompt, resolution=resolution, duration=duration,
        created=int(time.time()),
    )
    segments = max(1, min(int(segments), 6))
    if segments > 1:
        # 长视频：第一段按 mode，后续段「尾帧→下一段首帧」接续（i2v）
        segs = [{"mode": mode, "image": image_name, "prompt": prompt}]
        for _ in range(segments - 1):
            segs.append({"mode": "i2v", "image": None, "prompt": prompt})
        threading.Thread(
            target=_run_long_task,
            args=(task_id, model, segs, width, height, length, steps, seed),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_run_task,
            args=(task_id, model, mode, image_name, prompt, seed, width, height, length, steps),
            daemon=True,
        ).start()
    return {"task_id": task_id, "seed": seed}


@app.post("/api/storyboard")
async def storyboard_split(story: str = Form(...), n_shots: int = Form(6)):
    """把一段故事拆成分镜列表。"""
    n_shots = max(1, min(int(n_shots), 20))  # 任意数量，但限制在合理范围
    try:
        shots = storyboard.split_story(story, n_shots)
        return {"shots": shots}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"拆解失败：{e}"}, 500)


@app.post("/api/story-long")
async def story_long(
    model: str = Form("wan"),
    prompts: str = Form(""),
    resolution: str = Form("方屏 1:1"),
    duration: str = Form("长 · 约5秒"),
    seed: int = Form(-1),
    steps: int = Form(20),
):
    """故事模式长视频：把若干镜头按「首尾帧」接续，生成一条连续长视频。"""
    if not comfy.is_ready():
        return JSONResponse({"error": "生成引擎（ComfyUI）未启动，请先启动它"}, 503)
    if model not in MODELS:
        return JSONResponse({"error": "未知模型"}, 400)
    if model == "h3":
        if not comfy.h3_ready():
            return JSONResponse({"error": "MiniMax H3 模型还没就绪"}, 503)
    elif not comfy.t2v_ready():
        return JSONResponse({"error": "文生视频模型还在下载中，暂不可用"}, 503)

    shot_prompts = [p.strip() for p in prompts.split("\n") if p.strip()]
    if len(shot_prompts) < 2:
        return JSONResponse({"error": "至少需要 2 个镜头才能做连续长视频"}, 400)

    width, height = RESOLUTIONS.get(resolution, (640, 640))
    length = (DURATIONS_H3 if model == "h3" else DURATIONS).get(duration, 124 if model == "h3" else 81)
    steps = max(4, min(int(steps), MAX_STEPS))
    if model == "wan" and steps % 2:
        steps += 1
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)

    task_id = uuid.uuid4().hex[:12]
    # 第一段 t2v，后续段用上一段尾帧做首帧（i2v）
    segs = [{"mode": "t2v", "image": None, "prompt": shot_prompts[0]}]
    for p in shot_prompts[1:]:
        segs.append({"mode": "i2v", "image": None, "prompt": p})

    _update(
        task_id,
        state="queued", msg="排队中…",
        mode="story", model=model, seed=seed, steps=steps,
        prompt="、".join(shot_prompts[:3]) + ("…" if len(shot_prompts) > 3 else ""),
        resolution=resolution, duration=duration,
        created=int(time.time()),
    )
    threading.Thread(
        target=_run_long_task,
        args=(task_id, model, segs, width, height, length, steps, seed),
        daemon=True,
    ).start()
    return {"task_id": task_id, "seed": seed}


@app.post("/api/concat")
async def concat(task_ids: str = Form(...)):
    """把若干已生成的片段按顺序拼接成完整视频，返回新任务 id。"""
    ids = [t.strip() for t in task_ids.split(",") if t.strip()]
    paths = [OUTPUT / f"{tid}.mp4" for tid in ids if (OUTPUT / f"{tid}.mp4").exists()]
    if len(paths) < 2:
        return JSONResponse({"error": "至少需要 2 个已生成的片段才能拼接"}, 400)
    out_id = uuid.uuid4().hex[:12]
    out = OUTPUT / f"{out_id}.mp4"
    try:
        concat_videos(paths, out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"拼接失败：{e}"}, 500)
    _update(out_id, state="done", msg="拼接完成", video=out.name, mode="concat",
            created=int(time.time()))
    return {"task_id": out_id}


@app.get("/api/status/{task_id}")
def status(task_id: str):
    t = tasks.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, 404)
    d = dict(t)
    state = t.get("state")
    pid = t.get("prompt_id")
    if pid and state in ("queued", "running"):
        qs, pos = comfy.queue_status(pid)
        d["queue_state"] = qs
        d["queue_pos"] = pos
        d["progress"] = round(comfy.get_progress(pid) * 100, 1)
    elif state == "done":
        d["queue_state"] = "done"
        d["progress"] = 100.0
    else:
        d["queue_state"] = state
        d["progress"] = 0.0
    return d


@app.post("/api/cancel/{task_id}")
def cancel_task(task_id: str):
    """取消一个排队中或正在生成的任务。"""
    t = tasks.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, 404)
    if t.get("state") not in ("queued", "running"):
        return JSONResponse({"error": "任务已结束，无需取消"}, 400)
    # 先打标记，让后台线程尽快停下来（哪怕还没提交到引擎）
    t["cancelled"] = True
    _update(task_id, state="cancelled", msg="已取消")
    pid = t.get("prompt_id")
    if pid:
        comfy.cancel(pid)
    return {"task_id": task_id}


@app.get("/api/video/{task_id}")
def video(task_id: str):
    t = tasks.get(task_id)
    if t and t.get("state") == "done":
        return FileResponse(OUTPUT / t["video"], media_type="video/mp4")
    # 回退到磁盘：重启后内存清空，但视频文件仍在
    f = OUTPUT / f"{task_id}.mp4"
    if f.exists():
        return FileResponse(f, media_type="video/mp4")
    return JSONResponse({"error": "视频还没生成好"}, 404)
