# -*- coding: utf-8 -*-
"""一次性迁移脚本：把 app.py 按行号切分到四层架构。
运行后 app.py 变成瘦入口+兼容门面。
"""
import re
from pathlib import Path

BASE = Path(r"D:\VideoStudio")
src = (BASE / "app.py").read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)

# ---- 按函数名切分（每个函数从其 def/装饰器行到下一个顶级定义前） ----
# 找出所有顶级定义的 (name, start_line_idx, end_line_idx)
defs = []
for i, ln in enumerate(lines):
    m = re.match(r"^(?:@[\w\.\(=][^\n]*\n)*def (\w+)|^(?:@[\w\.\(=][^\n]*\n)*async def (\w+)", ln)
    if m:
        name = m.group(1) or m.group(2)
        # 回溯装饰器行
        start = i
        j = i - 1
        while j >= 0 and lines[j].startswith("@"):
            start = j
            j -= 1
        defs.append([name, start, None])
for k in range(len(defs) - 1):
    defs[k][2] = defs[k + 1][1]
if defs:
    defs[-1][2] = len(lines)

func_map = {d[0]: (d[1], d[2]) for d in defs}

def extract(names):
    """按名字抽取函数块文本（含前导装饰器与空行）。"""
    out = []
    for n in names:
        if n not in func_map:
            raise KeyError(f"function {n} not found in app.py")
        s, e = func_map[n]
        out.append("".join(lines[s:e]).rstrip() + "\n\n\n")
    return "".join(out)

# ---- 1. shared/paths.py ----
paths_py = '''# -*- coding: utf-8 -*-
"""路径常量（公共层）：全项目统一的目录与二进制位置。零依赖。"""
from pathlib import Path

BASE = Path(__file__).parent.parent
WORKFLOWS = BASE / "workflows"
OUTPUT = BASE / "output"
UPLOADS = BASE / "uploads"
JUBEN = BASE / "juben"
WEB = BASE / "web"
CREATIVE_PROFILES = OUTPUT / "creative_profiles.json"

# 拼接视频用的 ffmpeg（复用 ComfyUI 自带的二进制，避免再下载）
FFMPEG = r"D:\\ComfyUI_Wan\\venv\\Lib\\site-packages\\imageio_ffmpeg\\binaries\\ffmpeg-win-x86_64-v7.1.exe"
'''
(BASE / "shared" / "paths.py").write_text(paths_py, encoding="utf-8")

# ---- 2. shared/ffmpeg_tools.py ----
ffmpeg_py = '''# -*- coding: utf-8 -*-
"""ffmpeg 工具（公共层）：时长读取、拼接、抽帧。零业务规则。"""
import re
import subprocess

from shared.paths import FFMPEG


def video_duration(path) -> float:
    """用 ffmpeg 读视频时长（秒）；失败返回 0。"""
    try:
        r = subprocess.run(
            [FFMPEG, "-i", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r"Duration:\\s*(\\d+):(\\d+):(\\d+\\.\\d+)", r.stderr or r.stdout or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def hard_concat(paths: list, out) -> object:
    """硬切拼接（兜底，重新编码保证参数一致）。"""
    list_file = Path(out).with_suffix(".txt")
    list_file.write_text(
        "\\n".join(f"file '{Path(p).as_posix()}'" for p in paths), encoding="utf-8"
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


def concat_videos(paths: list, out, transition: float = 0.9) -> object:
    """按顺序把多个 mp4 用「交叉溶解」拼成一条。

    任一视频时长读不出来或太短时，退回硬切拼接兜底，保证一定出片。
    """
    from pathlib import Path as _P
    if len(paths) < 2:
        subprocess.run(
            [FFMPEG, "-y", "-i", str(paths[0]),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)],
            capture_output=True, timeout=1800,
        )
        return out
    durs = [video_duration(p) for p in paths]
    if any(d <= transition for d in durs):
        return hard_concat(paths, out)
    cmd = [FFMPEG, "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    parts = []
    acc = durs[0]
    prev_label = "[0:v]"
    for i in range(1, len(paths)):
        offset = acc - transition
        label = f"[v{i}]"
        parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}{label}"
        )
        prev_label = label
        acc = acc + durs[i] - transition
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", prev_label,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out),
    ]
    subprocess.run(cmd, capture_output=True, timeout=3600)
    return out


def extract_last_frame(video_path, out_png) -> object:
    """用 ffmpeg 抽视频最后一帧存成 png，供下一段当首帧（首尾帧接续）。"""
    subprocess.run(
        [FFMPEG, "-y", "-sseof", "-0.2", "-i", str(video_path),
         "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=300,
    )
    return out_png
'''
(BASE / "shared" / "ffmpeg_tools.py").write_text(ffmpeg_py, encoding="utf-8")

# ---- 3. db/tasks_store.py（从 app.py 抽任务档案相关） ----
tasks_py = '''# -*- coding: utf-8 -*-
"""任务档案存取（数据层）：内存 dict + 磁盘 json 持久化。无业务规则。"""
import json
import threading
import time
from pathlib import Path

from shared.paths import OUTPUT

tasks = {}
_lock = threading.Lock()


def reconcile_stale_tasks():
    """重启后把上次遗留的 queued/running 任务标记为中断（oneclick 由独立管道管理，跳过）。"""
    for f in OUTPUT.glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("mode") == "oneclick":
            continue
        if meta.get("state") in ("queued", "running"):
            meta["state"] = "error"
            meta["msg"] = "上次运行被重启打断"
            try:
                f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


def meta_dict(task_id: str, t: dict) -> dict:
    """把内存任务转成要落盘的档案。"""
    return {
        "task_id": task_id,
        "mode": t.get("mode", ""),
        "model": t.get("model", ""),
        "prompt": t.get("prompt", ""),
        "creative": t.get("creative", {}),
        "ai_prompts": t.get("ai_prompts", []),
        "resolution": t.get("resolution", ""),
        "duration": t.get("duration", ""),
        "steps": t.get("steps"),
        "seed": t.get("seed"),
        "state": t.get("state", ""),
        "msg": t.get("msg", ""),
        "video": t.get("video", ""),
        "style": t.get("style", ""),
        "review_stage": t.get("review_stage"),
        "shots": t.get("shots", []),
        "resampled": t.get("resampled", []),
        "seg_meta": t.get("seg_meta", {}),
        "character_card": t.get("character_card"),
        "card_desc": t.get("card_desc", ""),
        "cur_shot": t.get("cur_shot"),
        "approved_shots": t.get("approved_shots", []),
        "created": t.get("created", 0),
        "updated": t.get("updated", 0),
    }


def write_meta(task_id: str, t: dict):
    """把任务档案写到磁盘（重启不丢）；失败不抛出，避免拖垮生成流程。"""
    try:
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / f"{task_id}.json").write_text(
            json.dumps(meta_dict(task_id, t), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def update(task_id: str, **kw):
    """更新任务（跨进程安全：内存无此任务时先读磁盘档案合并，避免空壳覆盖丢数据）。"""
    with _lock:
        t = tasks.setdefault(task_id, {})
        if not t:
            f = OUTPUT / f"{task_id}.json"
            if f.exists():
                try:
                    disk = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(disk, dict) and disk.get("task_id"):
                        tasks[task_id] = disk
                        t = tasks[task_id]
                except Exception:
                    pass
        t.update(kw)
        t["updated"] = int(time.time())
        t.setdefault("task_id", task_id)
        write_meta(task_id, t)


def task_or_none(task_id: str):
    """按 id 取任务：先内存后磁盘。"""
    t = tasks.get(task_id)
    if not t and (OUTPUT / f"{task_id}.json").exists():
        try:
            t = json.loads((OUTPUT / f"{task_id}.json").read_text(encoding="utf-8"))
            tasks[task_id] = t
        except Exception:
            t = None
    return t


def list_from_disk() -> dict:
    """读磁盘上全部任务档案（跳过下划线开头的数据文件）。"""
    result = {}
    for f in OUTPUT.glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("task_id"):
                result[meta["task_id"]] = meta
        except Exception:
            continue
    return result


def cancelled(task_id: str) -> bool:
    return bool(tasks.get(task_id, {}).get("cancelled"))
'''
(BASE / "db" / "tasks_store.py").write_text(tasks_py, encoding="utf-8")

# ---- 4. db/profiles_store.py ----
prof_py = '''# -*- coding: utf-8 -*-
"""制片资料存取（数据层）：creative_profiles.json 读写。无业务规则。"""
import json

from shared.paths import CREATIVE_PROFILES

FIELDS = {
    "scenes": ("id", "name", "place", "era", "atmosphere", "lighting", "palette", "camera"),
    "characters": ("id", "name", "identity", "appearance", "wardrobe", "behavior"),
}


def profiles() -> dict:
    """读取本地制片资料；损坏文件时回退为空，不能影响出片。"""
    try:
        data = json.loads(CREATIVE_PROFILES.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {kind: data.get(kind, []) for kind in FIELDS}
    except Exception:
        pass
    return {kind: [] for kind in FIELDS}


def clean_profiles(data: dict) -> dict:
    cleaned = {}
    for kind, fields in FIELDS.items():
        items = data.get(kind, []) if isinstance(data, dict) else []
        cleaned[kind] = [
            {field: str(item.get(field, ""))[:800] for field in fields}
            for item in items[:30] if isinstance(item, dict) and str(item.get("id", ""))
        ]
    return cleaned


def save_profiles(data: dict) -> dict:
    cleaned = clean_profiles(data)
    CREATIVE_PROFILES.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned
'''
(BASE / "db" / "profiles_store.py").write_text(prof_py, encoding="utf-8")

print("shared + db written")
print("funcs available:", sorted(func_map.keys()))
