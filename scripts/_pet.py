# -*- coding: utf-8 -*-
"""桌宠 v5 —— 3D 橘猫·纯形象版 + 一键拉起产线。

v5 变化（针对两个反馈）：
 1. 窗口缩到只留猫图（150×150），去掉头顶进度花和底部常驻文字；
    状态改「右下角状态灯 + 鼠标悬停气泡」。
 2. 「继续」真正拉起产线，不再只是写控制文件：
    写 RUN → 引擎(8188)没起就拉起 ComfyUI 并轮询等就绪
    → 批量脚本没跑就拉起 _run_batch_seg2to10.py。
    进程 DETACHED 独立运行，桌宠退出不影响产线（重启后双击猫即可全量恢复）。

交互：
 - 双击：智能切换——产线没跑则完整拉起；正在跑则切暂停/继续
 - 右键：恢复产线 / 暂停 / 结束 / 退出桌宠（不影响产线）
 - 悬停：气泡显示引擎/产线/进度详情
 - 拖到屏幕边缘自动收窄成缝，鼠标靠近滑出

依赖：httpx、tkinter（标准库）。猫图为离线预处理好的 pet.png。
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------- 路径与常量 ----------------
OUTPUT_DIR = Path(r"D:\VideoStudio\output")
PET_IMG = Path(r"D:\VideoStudio\_kitty_assets\pet.png")
CONTROL = OUTPUT_DIR / "_control.txt"
COMFY = "http://127.0.0.1:8188"

# 产线进程
ENGINE_PY = r"D:\ComfyUI_Wan\venv\Scripts\python.exe"
ENGINE_DIR = r"D:\ComfyUI_Wan"
BATCH_PY = r"D:\VideoStudio\venv\Scripts\python.exe"
BATCH_DIR = r"D:\VideoStudio"
ENGINE_PID = OUTPUT_DIR / "_engine.pid"
BATCH_PID = OUTPUT_DIR / "_batch.pid"
ENGINE_LOG = OUTPUT_DIR / "_engine.log"
BATCH_LOG = OUTPUT_DIR / "batch.log"

# 独立进程 flags：不继承控制台 + 独立进程组 + 无窗口
DETACHED = 0x00000008 | 0x00000200 | 0x08000000

# 进度日志源（解析 tqdm 的 s/it 采样进度）
LOG_SOURCES = [
    OUTPUT_DIR / "_engine.log",  # 引擎 stdout（含 tqdm 进度条）
    OUTPUT_DIR / "batch.log",
    Path(r"C:/Users/32492/AppData/Local/Temp/comfyui_regenerate.log"),
]

PER_SEG_MIN = 55        # 每段预估分钟（兜底）
SEC_PER_IT = 171        # 每采样步兜底秒数
TOTAL = 10

# 颜色（品红 #FF00FF 作透明键，形象里绝不使用它）
KEY = "#FF00FF"
# 状态灯
C_RUN = "#2ecc71"     # 运行中 绿
C_PAUSE = "#f39c12"   # 暂停 黄
C_STOP = "#e74c3c"    # 已停止 / 引擎未启动 红
C_IDLE = "#95a5a6"    # 待命 灰
C_DONE = "#f1c40f"    # 全部完成 金


# ---------------- 进程工具 ----------------
def _alive(pid) -> bool:
    """Windows 进程存活检测（OpenProcess + GetExitCodeProcess）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    STILL_ACTIVE = 259
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def engine_ready() -> bool:
    try:
        return httpx.get(COMFY + "/system_stats", timeout=3).status_code == 200
    except Exception:
        return False


def batch_running() -> bool:
    try:
        return _alive(BATCH_PID.read_text().strip())
    except Exception:
        return False


def _start_engine() -> bool:
    """拉起 ComfyUI 引擎（若未就绪）。返回是否已就绪/已启动。"""
    if engine_ready():
        return True
    try:
        if _alive(ENGINE_PID.read_text().strip()):
            return False  # 已在启动中
    except Exception:
        pass
    env = dict(os.environ)
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    with open(ENGINE_LOG, "ab") as logf:
        p = subprocess.Popen(
            [ENGINE_PY, "main.py", "--lowvram", "--reserve-vram", "0.3",
             "--listen", "127.0.0.1", "--port", "8188"],
            cwd=ENGINE_DIR, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=DETACHED)
    ENGINE_PID.write_text(str(p.pid))
    return False


def _start_batch() -> bool:
    """拉起批量脚本（若未在跑）。返回是否已在跑。"""
    if batch_running():
        return True
    with open(BATCH_LOG, "ab") as logf:
        p = subprocess.Popen(
            [BATCH_PY, "-u", "_run_batch_seg2to10.py"],
            cwd=BATCH_DIR, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=DETACHED)
    BATCH_PID.write_text(str(p.pid))
    return False


def resume_pipeline(cb=None) -> bool:
    """完整拉起产线：写 RUN → 引擎 → 等就绪 → 批量脚本。"""
    control_set("RUN")
    if not engine_ready():
        _start_engine()
        if cb:
            cb("引擎启动中…（冷启动约 1-2 分钟）")
        for _ in range(120):  # 最多等 6 分钟
            time.sleep(3)
            if engine_ready():
                break
    if not engine_ready():
        if cb:
            cb("引擎启动失败，见 _engine.log")
        return False
    if cb:
        cb("引擎就绪")
    if not batch_running():
        _start_batch()
        if cb:
            cb("批量产线已启动")
    return True


# ---------------- 状态读取 ----------------
def read_state():
    done = {i for i in range(1, TOTAL + 1)
            if (OUTPUT_DIR / f"highres_seg{i:02d}.mp4").exists()}
    frac = s_it = v = total = None
    # 取最近修改的日志，解析 tqdm 进度条
    cands = [p for p in LOG_SOURCES if p.exists() and p.stat().st_size > 0]
    if cands:
        p = max(cands, key=lambda q: q.stat().st_mtime)
        try:
            with p.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                log = f.read().decode("utf-8", errors="replace")
            log = re.sub(r"\x1b\[[0-9;]*m", "", log)
            lines = [l for l in re.split(r"[\r\n]", log) if "it]" in l]
            if lines:
                m = re.search(
                    r"(\d+)/(\d+)\s*\[\d+:\d+<.*?(\d+(?:\.\d+)?)s/it", lines[-1])
                if m:
                    v, total, s_it = (int(m.group(1)), int(m.group(2)),
                                      float(m.group(3)))
                    frac = v / total
        except Exception:
            pass
    engine = engine_ready()
    batch = batch_running()
    running = pending = None
    if engine:
        try:
            q = httpx.get(COMFY + "/queue", timeout=3).json()
            running = len(q.get("queue_running", []))
            pending = len(q.get("queue_pending", []))
        except Exception:
            pass
    return {"done": done, "frac": frac, "v": v, "total": total, "s_it": s_it,
            "engine": engine, "batch": batch, "running": running, "pending": pending}


def control_get():
    try:
        return CONTROL.read_text(encoding="utf-8").strip().upper()
    except Exception:
        return ""


def control_set(what: str):
    try:
        CONTROL.write_text(what, encoding="utf-8")
    except Exception:
        pass


# ---------------- UI ----------------
class Pet(tk.Tk):
    W, H = 150, 150

    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=KEY)
        try:
            self.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        self.geometry(f"{self.W}x{self.H}+1060+80")

        self._drag = {"x": 0, "y": 0, "sx": 0, "sy": 0, "moved": False}
        self._paused = False
        self._tip = None
        self._img = None
        self._st = {}

        self.canvas = tk.Canvas(self, width=self.W, height=self.H, bg=KEY,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        if PET_IMG.exists():
            self._img = tk.PhotoImage(file=str(PET_IMG))

        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Double-Button-1>", lambda e: self._on_double())
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Enter>", lambda e: self._show_tip())
        self.canvas.bind("<Leave>", lambda e: self._hide_tip())

        self.refresh()
        self.after(2500, self._tick)
        self.after(800, self._edge_tick)

    # ---------- 绘制 ----------
    def _lamp_color(self):
        st = self._st
        if len(st.get("done", ())) >= TOTAL:
            return C_DONE
        ctl = control_get()
        if ctl == "PAUSE":
            return C_PAUSE
        busy = bool(st.get("running") or st.get("pending"))
        if ctl == "STOP" and not busy:
            return C_STOP
        if not st.get("engine"):
            return C_STOP
        if not st.get("batch") and not busy:
            return C_STOP
        if busy:
            return C_RUN
        return C_IDLE

    def _draw(self):
        c = self.canvas
        c.delete("all")
        if self._img is not None:
            c.create_image(self.W // 2, self.H // 2, image=self._img)
        color = self._lamp_color()
        c.create_oval(self.W - 20, self.H - 20, self.W - 8, self.H - 8,
                      fill=color, outline="white", width=2)

    # ---------- 状态刷新 ----------
    def _tick(self):
        self.refresh()
        self.after(2500, self._tick)

    def refresh(self):
        self._st = read_state()
        self._paused = (control_get() == "PAUSE")
        self._draw()

    def _cur(self):
        done = self._st.get("done", set())
        for i in range(1, TOTAL + 1):
            if i not in done:
                return i
        return TOTAL

    def _est(self, st, n):
        if st.get("frac") and st.get("v") and st.get("total"):
            s_it = st.get("s_it") or SEC_PER_IT
            rem_cur = (st["total"] - st["v"]) * s_it / 60
        else:
            rem_cur = PER_SEG_MIN
        return rem_cur + max(0, TOTAL - n - 1) * PER_SEG_MIN

    # ---------- 悬停气泡 ----------
    def _tip_lines(self):
        st = self._st
        n = len(st.get("done", ()))
        ctl = control_get()
        eng = "运行中" if st.get("engine") else "未启动"
        bat = "运行中" if st.get("batch") else "未启动"
        lines = [f"小猫故事片 · 已完成 {n}/{TOTAL} 段"]
        lines.append(f"引擎 {eng} ｜ 产线 {bat}")
        if ctl == "PAUSE":
            lines.append("状态：已暂停 ⏸")
        elif ctl == "STOP":
            lines.append("状态：已停止 ⏹")
        else:
            lines.append("状态：运行 / 待命")
        if n >= TOTAL:
            lines.append("全部完成 🎉")
        elif st.get("frac"):
            lines.append(f"第 {self._cur()} 段 {st['frac']*100:.0f}% · "
                         f"剩约 {self._est(st, n)/60:.1f} 小时")
        lines.append("双击恢复/暂停 · 右键菜单")
        return lines

    def _show_tip(self):
        if self._drag.get("moved"):
            return
        self._hide_tip()
        t = tk.Toplevel(self)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg="#30224a")
        tk.Label(t, text="\n".join(self._tip_lines()), bg="#30224a", fg="white",
                 font=("Microsoft YaHei", 9), justify="left",
                 padx=10, pady=8).pack()
        t.update_idletasks()
        x = self.winfo_x() + self.W + 6
        y = self.winfo_y()
        if x + t.winfo_width() > self.winfo_screenwidth():
            x = self.winfo_x() - t.winfo_width() - 6
        t.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._tip = t

    def _hide_tip(self, e=None):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    # ---------- 气泡提示（短暂） ----------
    def _flash(self, msg):
        self._hide_tip()
        t = tk.Toplevel(self)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg="#3a2e7a")
        tk.Label(t, text=msg, bg="#3a2e7a", fg="white",
                 font=("Microsoft YaHei", 10, "bold"), padx=10, pady=6).pack()
        t.update_idletasks()
        x = self.winfo_x() + (self.W - t.winfo_width()) // 2
        y = self.winfo_y() - t.winfo_height() - 8
        t.geometry(f"+{max(0, x)}+{max(0, y)}")
        t.after(2200, t.destroy)

    # ---------- 控制 ----------
    def _on_double(self):
        if self._drag.get("moved"):
            return
        st = self._st
        n = len(st.get("done", ()))
        if n >= TOTAL:
            self._flash("已全部完成 🎉")
            return
        ctl = control_get()
        busy = bool(st.get("running") or st.get("pending")) or st.get("batch")
        if ctl == "PAUSE":
            control_set("RUN")
            self._flash("继续生成 ▶")
        elif busy or ctl == "RUN":
            control_set("PAUSE")
            self._flash("已暂停 ⏸")
        else:
            threading.Thread(target=self._resume_bg, daemon=True).start()

    def _resume_bg(self):
        def cb(msg):
            self.after(0, lambda: self._flash(msg))
        ok = resume_pipeline(cb)
        self.after(0, lambda: self._flash(
            "产线已拉起，开始出片" if ok else "启动失败，看 _engine.log"))

    def _stop(self):
        control_set("STOP")
        try:
            httpx.post(COMFY + "/interrupt", timeout=5)
        except Exception:
            pass
        self._flash("已结束 ⏹")

    def _menu(self, e):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="恢复产线 ▶（拉起引擎+脚本）",
                      command=lambda: threading.Thread(
                          target=self._resume_bg, daemon=True).start())
        m.add_command(label="暂停 ⏸",
                      command=lambda: (control_set("PAUSE"), self._flash("已暂停 ⏸")))
        m.add_separator()
        m.add_command(label="结束 ⏹（中断+停止）", command=self._stop)
        m.add_separator()
        m.add_command(label="退出桌宠（不影响产线）", command=self.destroy)
        m.tk_popup(e.x_root, e.y_root)

    # ---------- 拖动 ----------
    def _drag_start(self, e):
        self._hide_tip()
        self._drag.update({"x": e.x_root - self.winfo_x(),
                           "y": e.y_root - self.winfo_y(),
                           "sx": e.x_root, "sy": e.y_root, "moved": False})

    def _drag_move(self, e):
        if (abs(e.x_root - self._drag["sx"]) > 4
                or abs(e.y_root - self._drag["sy"]) > 4):
            self._drag["moved"] = True
        try:
            self.geometry(f"+{e.x_root - self._drag['x']}+{e.y_root - self._drag['y']}")
        except Exception:
            pass

    # ---------- 边缘隐藏 ----------
    def _edge_tick(self):
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x, y = self.winfo_x(), self.winfo_y()
            px, py = self.winfo_pointerxy()
        except Exception:
            self.after(800, self._edge_tick)
            return
        HIDE = 6
        MARGIN = 70
        GRAB = 46
        cx, cy = x + self.W / 2, y + self.H / 2
        dist = {"L": cx, "T": cy, "R": sw - cx, "B": sh - cy}
        edge = min(dist, key=dist.get)
        near = dist[edge] < MARGIN
        mouse_edge = (px < GRAB or px > sw - GRAB or py < GRAB or py > sh - GRAB)
        if near and not mouse_edge:
            if edge == "L":
                self.geometry(f"{-self.W + HIDE:+d}+{y}")
            elif edge == "R":
                self.geometry(f"{sw - HIDE:+d}+{y}")
            elif edge == "T":
                self.geometry(f"+{x}{-self.H + HIDE:+d}")
            else:
                self.geometry(f"+{x}{sh - HIDE:+d}")
        elif mouse_edge:
            nx, ny = x, y
            if edge in ("L", "R"):
                nx = 8 if edge == "L" else sw - self.W - 8
                ny = max(60, min(ny, sh - self.H - 60))
            else:
                ny = 8 if edge == "T" else sh - self.H - 8
                nx = max(40, min(x, sw - self.W - 40))
            self.geometry(f"+{nx}+{ny}")
        self.after(800, self._edge_tick)


if __name__ == "__main__":
    try:
        Pet().mainloop()
    except Exception:
        import traceback
        try:
            (OUTPUT_DIR / "_pet_crash.log").write_text(
                traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
