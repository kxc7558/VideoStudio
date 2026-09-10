# -*- coding: utf-8 -*-
"""意见箱存取（数据层）：每条意见一个 json 文件，落盘 data/feedback/。无业务规则。"""
import json
import threading
import time

from shared.paths import FEEDBACK as _FEEDBACK

_lock = threading.Lock()
FEEDBACK = _FEEDBACK


def list_feedback() -> list:
    """列出全部意见：未处理在前（新→旧），已处理在后。"""
    if not FEEDBACK.exists():
        return []
    items = []
    for f in FEEDBACK.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("id"):
            items.append(d)
    status_rank = {"new": 0, "done": 1, "wontfix": 2}
    items.sort(key=lambda x: (status_rank.get(x.get("status"), 1), x.get("time", "")), reverse=False)
    items.sort(key=lambda x: status_rank.get(x.get("status"), 1))
    return items


def get_feedback(item_id: str) -> dict | None:
    """按 id 读取一条意见；不存在返回 None。"""
    f = FEEDBACK / f"{item_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_feedback(item: dict) -> None:
    """写入/覆盖一条意见文件（整文件写，item 已是合并后的完整档案）。"""
    FEEDBACK.mkdir(parents=True, exist_ok=True)
    (FEEDBACK / f"{item['id']}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_feedback(text: str, page: str = "", screenshot: str = "") -> dict:
    """新增一条吐槽，返回落盘后的完整档案。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    item = {
        "id": time.strftime("%Y-%m-%d") + "-" + format(int(time.time() * 1000) % 100000, "05d"),
        "time": ts,
        "text": text[:2000],
        "screenshot": screenshot,
        "page": page[:200],
        "status": "new",
        "reply": "",
    }
    with _lock:
        save_feedback(item)
    return item


def update_status(item_id: str, status: str, reply: str = "") -> dict | None:
    """更新一条意见的状态与回信（读-改-写，防覆盖）。返回更新后的档案。"""
    with _lock:
        item = get_feedback(item_id)
        if item is None:
            return None
        item["status"] = status
        item["reply"] = reply[:2000]
        item["handled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_feedback(item)
    return item
