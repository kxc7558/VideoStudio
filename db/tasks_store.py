# -*- coding: utf-8 -*-
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
