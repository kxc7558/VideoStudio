# -*- coding: utf-8 -*-
"""豆包视频生成 CDP 管道（接口层适配器，shared 层——外部服务适配器）。

前提：豆包桌面版以以下参数启动（本机验证 Chromium 147 可用）：
    Doubao.exe --remote-debugging-port=9333 --remote-allow-origins=*

流程（全部验证过）：
    1. CDP 连接豆包聊天页
    2. 注入分镜提示词到 ProseMirror 输入框
    3. 点击「视频生成」模式按钮
    4. 触发发送（Enter）
    5. 轮询会话区出现成品视频 → 拿到下载链接 → httpx 下载 mp4

豆包每日额度只消耗在第 3~4 步（一次生成一条）。
"""
import base64
import json
import time
import urllib.request

import httpx

CDP = "http://127.0.0.1:9333"
VIDEO_BUTTON_LABEL = "视频生成"


def _ws_url() -> str:
    for t in json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=5).read()):
        if t.get("type") == "page" and "doubao-chat" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("豆包聊天页未找到（豆包未启动或没带调试参数）")


class DoubaoDriver:
    """一个豆包聊天页的 CDP 驱动器（每实例持有独立 WebSocket）。"""

    def __init__(self):
        import websocket
        self.ws = websocket.create_connection(_ws_url(), timeout=20)
        self._id = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def _rpc(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                r = msg.get("result", {})
                if "exceptionDetails" in r:
                    raise RuntimeError(str(r["exceptionDetails"])[:300])
                return r.get("result", {}).get("result", {}).get("value")
        raise TimeoutError(method)

    def _js(self, expr: str):
        return self._rpc("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)

    def cdp_alive(self) -> bool:
        try:
            urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
            return True
        except Exception:
            return False

    # ---- 页面操作 ----

    def new_chat(self):
        """新开一个会话，避免历史上下文互相污染。"""
        self._js("location.hash = '#/chat'; (()=>{const b=document.querySelector('[data-testid*=new],[aria-label*=新对话],[aria-label*=新建]'); if(b) b.click();})()")

    def inject_prompt(self, text: str) -> bool:
        """把提示词写进输入框（ProseMirror 需要 execCommand 触发框架感知）。"""
        r = self._js("""
((text) => {
  const el = document.querySelector('.tiptap.ProseMirror[contenteditable=\"true\"]');
  if (!el) return false;
  el.focus();
  document.execCommand('selectAll', false, null);
  document.execCommand('delete', false, null);
  document.execCommand('insertText', false, text);
  return el.textContent.length > 0;
})(%s)
""" % json.dumps(text, ensure_ascii=False))
        return bool(r)

    def click_video_mode(self) -> bool:
        """点「视频生成」模式按钮（豆包内置的文生视频入口）。"""
        r = self._js("""
(() => {
  for (const b of document.querySelectorAll('button')) {
    const t = (b.getAttribute('aria-label')||'') + (b.textContent||'');
    if (t.includes('视频生成') && !b.disabled) { b.click(); return true; }
  }
  return false;
})()
""")
        return bool(r)

    def send(self) -> bool:
        """触发发送（ProseMirror 回车）。"""
        r = self._js("""
(() => {
  const el = document.querySelector('.tiptap.ProseMirror[contenteditable=\"true\"]');
  if (!el) return false;
  el.focus();
  const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true});
  el.dispatchEvent(ev);
  return true;
})()
""")
        return bool(r)

    def submit_video_task(self, prompt: str) -> bool:
        """一条龙：注入提示词 → 视频模式 → 发送。任一步失败返回 False。"""
        if not self.inject_prompt(prompt):
            return False
        if not self.click_video_mode():
            return False
        return self.send()

    # ---- 结果抓取 ----

    def poll_video_result(self, timeout_s: int = 900, poll_s: float = 10.0):
        """轮询会话区最新一条消息里的视频元素，返回 mp4 直链或 None。"""
        deadline = time.time() + timeout_s
        expr = """
(() => {
  const vids = document.querySelectorAll('video');
  if (!vids.length) return null;
  const v = vids[vids.length - 1];
  const src = v.querySelector('source')?.src || v.src || '';
  return src || null;
})()
"""
        while time.time() < deadline:
            try:
                src = self._js(expr)
                if src:
                    return src
            except Exception:
                pass  # 页面刷新/CDP 抖动，继续轮询
            time.sleep(poll_s)
        return None

    def screenshot(self, path: str):
        """截当前页面（人工确认/调试用）。"""
        import websocket
        data = self._rpc("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(data["data"]))


def download_video(url: str, dest) -> bool:
    """把豆包 CDN 视频下载到本地。"""
    r = httpx.get(url, timeout=300, follow_redirects=True)
    r.raise_for_status()
    open(dest, "wb").write(r.content)
    return True


def start_doubao_with_cdp():
    """带调试参数启动豆包（已运行则先提示，不重复杀进程）。"""
    import subprocess
    exe = r"C:\Users\32492\AppData\Local\Doubao\Application\app\Doubao.exe"
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
        return True  # 已在跑
    except Exception:
        pass
    subprocess.Popen([exe, "--remote-debugging-port=9333", "--remote-allow-origins=*"])
    for _ in range(30):
        time.sleep(3)
        try:
            urllib.request.urlopen(f"{CDP}/json/version", timeout=3)
            return True
        except Exception:
            continue
    return False
