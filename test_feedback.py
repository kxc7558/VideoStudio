# -*- coding: utf-8 -*-
"""意见箱冒烟测试。直跑：venv/Scripts/python.exe test_feedback.py（--api 加测接口层）"""
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 意见箱目录指向临时目录，不污染真实 data/feedback/
_tmp = Path(tempfile.mkdtemp(prefix="vs_feedback_test_"))
import shared.paths as paths

paths.FEEDBACK = _tmp
import db.feedback_store as feedback_store

feedback_store.FEEDBACK = _tmp

failures = []


def check(name, fn):
    try:
        fn()
        print("PASS", name)
    except Exception as e:
        failures.append(name)
        print("FAIL", name, "->", type(e).__name__, e)


def t_add_and_list():
    item = feedback_store.add_feedback("测试吐槽：进度条不动", page="i2v")
    assert item["status"] == "new"
    assert item["text"] == "测试吐槽：进度条不动"
    items = feedback_store.list_feedback()
    assert items[0]["id"] == item["id"]


def t_update_status():
    item = feedback_store.add_feedback("测试吐槽2", page="t2v")
    feedback_store.update_status(item["id"], "done", reply="已修复：进度条常驻显示")
    after = feedback_store.get_feedback(item["id"])
    assert after["status"] == "done"
    assert "已修复" in after["reply"]


def t_wontfix():
    item = feedback_store.add_feedback("测试吐槽3", page="")
    feedback_store.update_status(item["id"], "wontfix", reply="成本太高，暂不做")
    after = feedback_store.get_feedback(item["id"])
    assert after["status"] == "wontfix"


def t_api_roundtrip():
    import app
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    r = c.post("/api/feedback", data={"text": "接口冒烟吐槽", "page": "story"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    items = c.get("/api/feedback").json()["items"]
    assert any(x["id"] == rid for x in items)
    r3 = c.get("/api/feedback/changelog")
    assert r3.status_code == 200 and "text" in r3.json()


check("数据层：新增+列表排序", t_add_and_list)
check("数据层：处理+回信", t_update_status)
check("数据层：wontfix 状态", t_wontfix)
if "--api" in sys.argv:
    check("接口层：POST/GET/changelog 冒烟", t_api_roundtrip)

print(len(failures), "failures")
sys.exit(1 if failures else 0)
