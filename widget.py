"""Small, local-only Galaxy Book desktop widget. No browser or web server."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

BASE = Path(__file__).resolve().parent
TITLE = "Galaxy Book3 Pro"
CONFIG = BASE / "settings.json"
BG = "#232b34"
INK = "#f1f4f7"
MUTED = "#a3adb8"
DIM = "#798693"
MINT = "#9be9bb"
RED = "#f28b82"
LINE = "#35414c"
PILL = "#304f43"
ZOOM_LEVELS = (0.85, 0.9, 1.0, 1.1, 1.25, 1.5)
DEFAULTS = {"pinned": True, "zoom": 0.85, "interval": 2, "sensor_url": None}


def load_settings():
    result = dict(DEFAULTS)
    try:
        stored = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(stored,dict):
            return result
        result.update({k: stored[k] for k in DEFAULTS if k in stored})
        for key in ("x", "y"):
            if isinstance(stored.get(key), int):
                result[key] = stored[key]
    except (OSError, ValueError, TypeError):
        pass
    result["zoom"] = result["zoom"] if result["zoom"] in ZOOM_LEVELS else DEFAULTS["zoom"]
    result["interval"] = result["interval"] if result["interval"] in (2, 5) else 2
    result["pinned"] = bool(result["pinned"])
    if result.get("sensor_url") not in (None, "http://127.0.0.1:8085/data.json", "http://localhost:8085/data.json"):
        result["sensor_url"] = None
    return result


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def percent(value):
    return f"{max(0, min(100, value)):.0f}%" if finite(value) else "—"


def speed(value):
    if not finite(value) or value < 0:
        return "—"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB/s"
    if value >= 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value:.0f} B/s"


def duration(value):
    if not finite(value) or value <= 0 or value > 7 * 24 * 3600:
        return None
    minutes = max(1, int(value / 60))
    hours, minutes = divmod(minutes, 60)
    return f"약 {hours}시간 {minutes:02d}분 남음"


def metric_warnings(data, previous=None):
    previous = previous or {}
    def high(key, on, off):
        value = data.get(key)
        return finite(value) and (value > off if previous.get(key) else value >= on)
    cpu = high("cpu_percent",80,70)
    ram = high("ram_percent",85,80)
    value = data.get("battery_percent")
    low = finite(value) and (value < 25 if previous.get("battery_low") else value <= 20)
    return {"cpu_percent":cpu,"ram_percent":ram,"battery_low":low,"battery":low}


def power_value(value, direction):
    if not finite(value) or value < 0:
        return "—"
    sign = "+" if direction == "charging" else "−" if direction == "discharging" else "" if value == 0 else None
    return f"{sign}{value:.1f} W" if sign is not None else "—"


def section_plan(available):
    sections = []
    if available.get("performance_mode"):
        sections.append(("mode", 36))
    sections.extend((("cpu", 64), ("ram", 58)))
    if available.get("battery"):
        sections.append(("battery", 56))
    power_rows = sum(bool(available.get(key)) for key in ("battery_watts", "input_power_w", "cpu_power_w"))
    if power_rows:
        sections.append(("power", 44 if power_rows == 1 else 36 + 21 * power_rows))
    sections.append(("network", 58))
    if available.get("fan_rpm"):
        sections.append(("fan", 48))
    return sections


def save_settings(settings):
    try:
        temp = CONFIG.with_suffix(".tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(CONFIG)
    except OSError:
        logging.exception("Could not save settings")


def single_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\GalaxyBook3ProLocalWidget")
    if ctypes.get_last_error() != 183:
        return handle
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    hwnd = user.FindWindowW(None, TITLE)
    if hwnd:
        user.ShowWindow(hwnd, 5)
        user.SetForegroundWindow(hwnd)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle(handle)
    return None


class Widget:
    WIDTH, HEIGHT = 340, 500

    def __init__(self, root):
        from telemetry import Collector

        self.root = root
        self.settings = load_settings()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.awake = threading.Event()
        self.awake.set()
        self.results = queue.Queue(maxsize=1)
        self.commands = queue.Queue()
        self.sample = {}
        self.last_update = None
        self.stale = False
        self.available = {}
        self.warnings = metric_warnings({})
        self.current_height = None
        self.history = deque(maxlen=40)
        self.fonts = {}
        self.moving = False
        self.move_finished = False
        self.native_hwnd = None
        self.clip_size = None
        self.notification = None
        self.old_proc = None
        self.proc_ref = None
        self.collector_type = Collector
        self.worker = threading.Thread(target=self.collect, name="metrics", daemon=True)

        root.title(TITLE)
        root.configure(bg=BG,borderwidth=0,highlightthickness=0)
        # Keep a normal native top-level window so Alt+Tab and desktop tools can
        # find it. Only the system caption is removed, not the whole window type.
        root.overrideredirect(False)
        root.attributes("-topmost", self.settings["pinned"])
        root.protocol("WM_DELETE_WINDOW", self.close)
        # An opaque window avoids colour-key layered-window trails while moving.
        # Native clipping below supplies the rounded corners instead.
        self.canvas = tk.Canvas(root, bd=0, highlightthickness=0, bg=BG)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.pointer_down)
        self.canvas.bind("<Button-3>", self.context_menu)
        self.canvas.bind("<Motion>", self.pointer_hover)
        root.bind("<Configure>", self.window_configured)
        root.bind("<Escape>", lambda _: self.close_menu())
        root.bind("<Alt-F4>", lambda _: self.close())
        self.build_menu()
        self.set_size()
        self.render()
        root.update_idletasks()
        self.show_native()
        self.install_power_notifications()
        self.worker.start()
        root.after(500, self.consume)

    def show_native(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        hwnd = user.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        self.native_hwnd = hwnd
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = user.GetWindowLongPtrW(hwnd, -16)
        user.SetWindowLongPtrW(hwnd, -16, style & ~(0x00C00000 | 0x00040000 | 0x00010000 | 0x00020000))
        exstyle = user.GetWindowLongPtrW(hwnd, -20)
        # Remove native raised/client/static edges as well as layered styling.
        edges = 0x00000100 | 0x00000200 | 0x00020000
        user.SetWindowLongPtrW(hwnd, -20, (exstyle | 0x00040000) & ~(0x00000080 | 0x00080000 | edges))
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            no_border = wintypes.DWORD(0xFFFFFFFE)  # DWMWA_COLOR_NONE
            dwm.DwmSetWindowAttribute(hwnd,34,ctypes.byref(no_border),ctypes.sizeof(no_border))
        except (OSError,AttributeError):
            pass  # Older Windows versions have no DWM border-colour attribute.
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
        # The launcher hides its console; explicitly show the actual widget.
        self.root.deiconify()
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindow(hwnd, 4)
        self.apply_window_region()

    def window_configured(self, event):
        if event.widget is self.root:
            self.apply_window_region()

    def apply_window_region(self):
        """Clip an opaque native window, only when its dimensions change."""
        if not self.native_hwnd:
            return
        size = (self.root.winfo_width(), self.root.winfo_height())
        if size == self.clip_size or min(size) < 2:
            return
        user = ctypes.WinDLL("user32",use_last_error=True)
        gdi = ctypes.WinDLL("gdi32",use_last_error=True)
        user.SetWindowRgn.argtypes = [wintypes.HWND,wintypes.HRGN,wintypes.BOOL]
        user.SetWindowRgn.restype = ctypes.c_int
        gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int]*6
        gdi.CreateRoundRectRgn.restype = wintypes.HRGN
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.DeleteObject.restype = wintypes.BOOL
        diameter = max(2,round(self.scale(44)))
        region = gdi.CreateRoundRectRgn(0,0,size[0]+1,size[1]+1,diameter,diameter)
        if not region:
            return
        # SetWindowRgn can synchronously generate another Configure event.
        self.clip_size = size
        if not user.SetWindowRgn(self.native_hwnd,region,True):
            # On success Windows owns and later deletes the region itself.
            gdi.DeleteObject(region)
            self.clip_size = None

    def scale(self, value):
        return value * self.factor

    def set_size(self):
        try:
            # Negative font sizes are device pixels; scale once for screen DPI.
            dpi = ctypes.windll.user32.GetDpiForWindow(self.root.winfo_id()) or 96
        except (AttributeError, OSError):
            dpi = 96
        self.factor = dpi / 96 * self.settings["zoom"]
        self.fonts.clear()
        self.current_height = None
        logical_height = 64 + sum(height for _,height in section_plan(self.available))
        width, height = round(self.scale(self.WIDTH)), round(self.scale(logical_height))
        area = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0)
        if area.right <= area.left:
            area = wintypes.RECT(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        x = self.settings.get("x", area.right - width - round(self.scale(28)))
        y = self.settings.get("y", area.top + round(self.scale(72)))
        x = max(area.left, min(x, area.right - width))
        y = max(area.top, min(y, area.bottom - height))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.canvas.configure(width=width, height=height)

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = ("맑은 고딕", -round(self.scale(size)), "bold" if bold else "normal")
        return self.fonts[key]

    def text(self, x, y, text, size=12, color=INK, bold=False, anchor="w", tag=None):
        return self.canvas.create_text(self.scale(x), self.scale(y), text=text,
                                       font=self.font(size, bold), fill=color, anchor=anchor,
                                       tags=(tag,) if tag else ())

    def line(self, x1, y1, x2, y2, color=LINE, width=1, **kw):
        return self.canvas.create_line(*(self.scale(v) for v in (x1, y1, x2, y2)),
                                       fill=color, width=self.scale(width), **kw)

    def rounded(self, x, y, w, h, r, fill, outline="", width=1):
        p = [x+r,y, x+w-r,y, x+w,y, x+w,y+r, x+w,y+h-r,
             x+w,y+h, x+w-r,y+h, x+r,y+h, x,y+h, x,y+h-r, x,y+r, x,y]
        return self.canvas.create_polygon([self.scale(v) for v in p], smooth=True,
                                          splinesteps=24, fill=fill, outline=outline,
                                          width=self.scale(width))

    def chip(self, right, y, label, active=False):
        width = max(42, 18 + len(label) * 10)
        self.rounded(right-width, y-10, width, 21, 10,
                     PILL if active else "#303a45")
        self.text(right-width/2, y, label, 10, MINT if active else MUTED, anchor="center")

    def render(self):
        if self.moving:
            return
        c = self.canvas
        c.delete("all")
        d = {} if self.stale else self.sample
        warnings = {} if self.stale else self.warnings
        cpu_color = RED if warnings.get("cpu_percent") else MINT
        ram_color = RED if warnings.get("ram_percent") else MINT
        battery_color = RED if warnings.get("battery") else MINT
        sections = section_plan(self.available)
        logical_height = 64 + sum(height for _,height in sections)
        height = round(self.scale(logical_height))
        if self.current_height != height:
            self.current_height = height
            self.root.geometry(f"{round(self.scale(self.WIDTH))}x{height}")
            self.canvas.configure(height=height)
        # The opaque canvas fills the native rounded region. Drawing a second
        # rounded outline here leaves a visible vertical seam at some DPI scales.
        dot = "#e1bd76" if self.stale else (MINT if self.last_update else DIM)
        c.create_oval(*(self.scale(v) for v in (22,24,30,32)), fill=dot, outline="")
        self.text(39, 28, TITLE, 18, bold=True)
        self.draw_pin(290, 28)
        for x in (313, 318, 323):
            c.create_oval(*(self.scale(v) for v in (x-1,27,x+1,29)), fill=MUTED, outline="")
        self.line(22, 52, 318, 52)
        y = 52
        for index,(section,block_height) in enumerate(sections):
            if section == "mode":
                self.text(22,y+20,"성능 모드",12,MUTED)
                mode = d.get("performance_mode")
                self.chip(318,y+20,mode or "—",bool(mode))
            elif section == "cpu":
                self.text(22,y+23,"CPU",16,bold=True)
                has_temp = self.available.get("cpu_temp_c")
                self.text(243 if has_temp else 318,y+23,percent(d.get("cpu_percent")),19,cpu_color,anchor="e")
                if has_temp:
                    self.line(256,y+15,256,y+31,"#6c7681")
                    temp = d.get("cpu_temp_c")
                    self.text(318,y+23,f"{temp:.0f}°C" if finite(temp) else "—",18,INK,anchor="e")
                freq = d.get("cpu_ghz")
                self.text(22,y+48,f"{freq:.2f} GHz" if finite(freq) else "",11,MUTED)
                self.sparkline(154,y+39,164,17,cpu_color)
            elif section == "ram":
                self.text(22,y+22,"RAM",16,bold=True)
                self.text(318,y+22,percent(d.get("ram_percent")),19,ram_color,anchor="e")
                used,total = d.get("ram_used_gb"),d.get("ram_total_gb")
                self.text(22,y+46,f"{used:.1f} / {total:.1f} GB" if finite(used) and finite(total) else "—",11,MUTED)
                self.rounded(176,y+42,142,6,3,"#53606e")
                ram = d.get("ram_percent")
                if finite(ram) and ram > 0:
                    self.rounded(176,y+42,max(3,142*min(100,max(0,ram))/100),6,3,ram_color)
            elif section == "battery":
                self.draw_battery(23,y+14,d.get("battery_percent"),battery_color)
                self.text(66,y+22,"배터리",16,bold=True)
                self.text(318,y+22,percent(d.get("battery_percent")),19,battery_color,anchor="e")
                state = d.get("battery_state","unknown")
                label = {"charging":"충전 중","discharging":"배터리 사용 중","plugged":"전원 연결 · 충전 대기"}.get(state,"—")
                remaining = duration(d.get("battery_seconds"))
                if state == "discharging" and remaining:
                    label = remaining
                battery_percent = d.get("battery_percent")
                if state in ("plugged","charging") and finite(battery_percent) and battery_percent >= 100:
                    label = "전원 연결 · 충전 완료"
                self.text(22,y+45,label,10.5,MUTED)
            elif section == "power":
                direction = d.get("battery_power_direction")
                name = "전력"
                rows = [(name,d.get("battery_watts"))] if self.available.get("battery_watts") else []
                if self.available.get("input_power_w"):
                    rows.append(("입력 전력",d.get("input_power_w")))
                if self.available.get("cpu_power_w"):
                    rows.append(("CPU 전력",d.get("cpu_power_w")))
                if len(rows) == 1:
                    name,value = rows[0]
                    self.text(22,y+23,name,14,bold=True)
                    self.text(318,y+23,power_value(value,direction),18,MINT,anchor="e")
                else:
                    self.text(22,y+21,"전력",15,bold=True)
                    for row,(name,value) in enumerate(rows):
                        self.text(22,y+45+row*21,name,11.5,MUTED)
                        formatted = power_value(value,direction) if name == "전력" else f"{value:.1f} W" if finite(value) else "—"
                        self.text(318,y+45+row*21,formatted,12.5,MINT,anchor="e")
            elif section == "network":
                self.text(22,y+21,"네트워크",15,bold=True)
                self.text(22,y+44,"↓",15,MINT)
                self.text(40,y+44,speed(d.get("download_bps")),12.5)
                self.text(191,y+44,"↑",15,MINT)
                self.text(209,y+44,speed(d.get("upload_bps")),12.5)
            elif section == "fan":
                self.draw_fan(32,y+24)
                self.text(53,y+24,"팬",15,bold=True)
                rpm = d.get("fan_rpm")
                if finite(rpm) and rpm >= 0:
                    label = "정지" if rpm == 0 else "저속" if rpm < 2500 else "중속" if rpm < 4000 else "고속"
                    self.chip(132,y+24,label,True)
                    self.text(318,y+24,f"{rpm:,.0f} RPM",16,MINT,anchor="e")
                else:
                    self.text(318,y+24,"—",16,DIM,anchor="e")
            y += block_height
            if index < len(sections)-1:
                self.line(22,y,318,y)

    def sparkline(self, x, y, width, height, color=MINT):
        if len(self.history) < 2 or self.stale:
            self.line(x, y+height/2, x+width, y+height/2, "#4c5d60")
            return
        values = list(self.history)
        points = []
        for i, value in enumerate(values):
            points.extend((self.scale(x+i*width/(len(values)-1)), self.scale(y+height*(1-value/100))))
        self.canvas.create_line(points, fill=color, width=self.scale(1.4), smooth=False)

    def draw_battery(self, x, y, value, color=MINT):
        self.rounded(x, y, 30, 17, 3, BG, INK, 1.2)
        self.line(x+32, y+5, x+32, y+12, INK, 2)
        if finite(value) and value > 0:
            self.rounded(x+3, y+3, max(2, 24*max(0,min(100,value))/100), 11, 1.5, color)

    def draw_fan(self, x, y):
        for angle in (0, 120, 240):
            rad = math.radians(angle)
            px, py = x+6*math.sin(rad), y-6*math.cos(rad)
            self.canvas.create_oval(*(self.scale(v) for v in (px-4.2,py-4.2,px+4.2,py+4.2)), outline=MUTED, width=self.scale(1))
        self.canvas.create_oval(*(self.scale(v) for v in (x-2,y-2,x+2,y+2)), fill=BG, outline=INK, width=self.scale(1))

    def draw_pin(self, x, y):
        color = MINT if self.settings["pinned"] else MUTED
        self.line(x-2,y-7,x+5,y-3,color,1.2)
        self.line(x-2,y-7,x-4,y-1,color,1.2)
        self.line(x+5,y-3,x,y+1,color,1.2)
        self.line(x-4,y-1,x-4,y+3,color,1.2)
        self.line(x-4,y+3,x,y+1,color,1.2)
        self.line(x-3,y+2,x-8,y+8,color,1.2)

    def collect(self):
        collector = None
        try:
            collector = self.collector_type(sensor_url=self.settings.get("sensor_url"))
            while not self.stop.is_set():
                if not self.awake.is_set():
                    self.wake.wait(5)
                    self.wake.clear()
                    continue
                try:
                    while True:
                        command, value = self.commands.get_nowait()
                        if command == "sensor_url":
                            collector.close()
                            collector = self.collector_type(sensor_url=value)
                except queue.Empty:
                    pass
                try:
                    data = collector.sample()
                    data["_received"] = time.monotonic()
                    if self.results.full():
                        try:
                            self.results.get_nowait()
                        except queue.Empty:
                            pass
                    self.results.put_nowait(data)
                except Exception:
                    logging.exception("Sampling failed")
                self.wake.wait(self.settings["interval"])
                self.wake.clear()
        except Exception:
            logging.exception("Collector initialization failed")
        finally:
            if collector:
                collector.close()

    def consume(self):
        if self.stop.is_set():
            return
        dirty = False
        if self.move_finished:
            self.move_finished = False
            self.settings["x"],self.settings["y"] = self.root.winfo_x(),self.root.winfo_y()
            save_settings(self.settings)
            dirty = True
        try:
            data = self.results.get_nowait()
            if self.last_update and data["_received"] - self.last_update > 15:
                self.history.clear()
            self.sample = data
            self.warnings = metric_warnings(data,self.warnings)
            self.last_update = data["_received"]
            for key in ("battery_watts","cpu_temp_c","input_power_w","cpu_power_w","fan_rpm"):
                if finite(data.get(key)):
                    self.available[key] = True
            if data.get("performance_mode"):
                self.available["performance_mode"] = True
            if finite(data.get("battery_percent")):
                self.available["battery"] = True
            value = data.get("cpu_percent")
            if finite(value):
                self.history.append(max(0, min(100, value)))
            dirty = True
        except queue.Empty:
            pass
        stale = bool(self.last_update and time.monotonic()-self.last_update > max(12,self.settings["interval"]*3))
        if stale != self.stale:
            self.stale = stale
            dirty = True
        if dirty:
            self.render()
        self.root.after(1000 if not self.awake.is_set() else 500, self.consume)

    def build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=False, background=BG, foreground=INK,
                            activebackground="#3d4b58", activeforeground=INK,
                            borderwidth=1, relief="flat", font=("맑은 고딕",10))
        self.pin_var = tk.BooleanVar(value=self.settings["pinned"])
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.pin_var, command=self.toggle_pin)
        size_menu = tk.Menu(self.menu, tearoff=False, background=BG, foreground=INK)
        self.zoom_var = tk.DoubleVar(value=self.settings["zoom"])
        for zoom in ZOOM_LEVELS:
            size_menu.add_radiobutton(label=f"{zoom*100:.0f}%", variable=self.zoom_var, value=zoom, command=self.change_zoom)
        self.menu.add_cascade(label="크기", menu=size_menu)
        interval_menu = tk.Menu(self.menu, tearoff=False, background=BG, foreground=INK)
        self.interval_var = tk.IntVar(value=self.settings["interval"])
        for seconds in (2,5):
            interval_menu.add_radiobutton(label=f"{seconds}초", variable=self.interval_var, value=seconds, command=self.change_interval)
        self.menu.add_cascade(label="갱신 간격", menu=interval_menu)
        self.menu.add_separator()
        self.menu.add_command(label="센서 연결 안내", command=self.sensor_dialog)
        self.menu.add_command(label="현재 수집 상태", command=self.show_status)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=self.close)

    def context_menu(self, event=None):
        x = event.x_root if event else self.root.winfo_rootx()+round(self.scale(235))
        y = event.y_root if event else self.root.winfo_rooty()+round(self.scale(44))
        try:
            self.menu.tk_popup(x,y)
        finally:
            self.menu.grab_release()

    def close_menu(self):
        self.menu.unpost()

    def toggle_pin(self):
        self.settings["pinned"] = not self.settings["pinned"]
        self.pin_var.set(self.settings["pinned"])
        self.root.attributes("-topmost", self.settings["pinned"])
        save_settings(self.settings)
        self.render()

    def change_zoom(self):
        self.settings["zoom"] = self.zoom_var.get()
        self.settings["x"], self.settings["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.set_size()
        save_settings(self.settings)
        self.render()

    def change_interval(self):
        self.settings["interval"] = self.interval_var.get()
        save_settings(self.settings)
        self.wake.set()

    def pointer_down(self, event):
        x, y = event.x/self.factor, event.y/self.factor
        if y < 52 and 275 <= x < 304:
            self.toggle_pin()
        elif y < 52 and x >= 304:
            self.context_menu(event)
        elif y < 52:
            self.move_native(event.x_root,event.y_root)

    def move_native(self, x, y):
        """Let Windows move its composited surface rather than moving per pixel."""
        if self.moving or not self.native_hwnd:
            return
        user = ctypes.WinDLL("user32",use_last_error=True)
        user.ReleaseCapture.argtypes = []
        user.ReleaseCapture.restype = wintypes.BOOL
        user.PostMessageW.argtypes = [wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        self.moving = True
        user.ReleaseCapture()
        point = (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)
        # Queue, rather than synchronously enter, the native move loop. A nested
        # Windows loop inside a Tcl button callback can abort the interpreter.
        if not user.PostMessageW(self.native_hwnd,0x00A1,2,point):
            self.moving = False

    def pointer_hover(self, event):
        x,y = event.x/self.factor,event.y/self.factor
        self.canvas.configure(cursor="hand2" if y < 52 and x >= 275 else "fleur" if y < 52 else "")

    def sensor_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("센서 연결")
        dialog.configure(bg=BG)
        dialog.resizable(False,False)
        dialog.attributes("-topmost", True)
        dialog.transient(self.root)
        text = ("온도·CPU 전력·팬은 별도 센서 연결이 필요합니다.\n\n"
                "Libre Hardware Monitor가 이미 실행 중이고\n"
                "로컬 웹 서버(127.0.0.1:8085)가 켜져 있다면\n"
                "아래에서 연결할 수 있습니다.\n"
                "센서 프로그램 설치나 설정 변경은 하지 않습니다.\n\n"
                "팬의 저속·중속·고속은 위젯의 RPM 기준이며\n"
                "삼성 성능 모드와는 별개입니다.\n"
                "입력 전력은 현재 측정을 지원하지 않습니다.")
        tk.Label(dialog,text=text,justify="left",bg=BG,fg=INK,font=("맑은 고딕",10),padx=22,pady=20).pack()
        enabled = tk.BooleanVar(value=bool(self.settings.get("sensor_url")))
        tk.Checkbutton(dialog,text="로컬 센서 연결 사용",variable=enabled,bg=BG,fg=INK,selectcolor="#394650",activebackground=BG,activeforeground=INK,font=("맑은 고딕",10)).pack(anchor="w",padx=18)
        def apply():
            url = "http://127.0.0.1:8085/data.json" if enabled.get() else None
            self.settings["sensor_url"] = url
            self.commands.put(("sensor_url",url))
            self.wake.set()
            save_settings(self.settings)
            dialog.destroy()
        tk.Button(dialog,text="적용",command=apply,bg="#3a5348",fg=INK,relief="flat",padx=22,pady=5).pack(pady=16)

    def show_status(self):
        d = self.sample
        errors = d.get("errors") or {}
        content = [f"갱신 간격: {self.settings['interval']}초", "표시값은 이 노트북에서 수집한 실제 값입니다.", ""]
        content += [f"CPU 온도: {'연결됨' if finite(d.get('cpu_temp_c')) else '센서 미연결'}",
                    f"CPU 전력: {'연결됨' if finite(d.get('cpu_power_w')) else '센서 미연결'}",
                    f"팬 RPM: {'연결됨' if finite(d.get('fan_rpm')) else '센서 미연결'}",
                    "입력 전력: 측정 미지원",
                    "성능 모드: 삼성 설정의 저장된 값", ""]
        if errors:
            content.append("일부 항목은 현재 수집할 수 없어 —로 표시됩니다.")
        content.append("팬 구분: 정지 0 / 저속 <2,500 / 중속 <4,000 / 고속 ≥4,000 RPM")
        messagebox.showinfo("수집 상태", "\n".join(content), parent=self.root)

    def install_power_notifications(self):
        """Observe display power events without repeatedly querying system state."""
        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.GetParent.argtypes = [wintypes.HWND]
            user.GetParent.restype = wintypes.HWND
            self.hwnd = user.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            class GUID(ctypes.Structure):
                _fields_ = [("Data1",wintypes.DWORD),("Data2",wintypes.WORD),("Data3",wintypes.WORD),("Data4",ctypes.c_ubyte*8)]
            guid = GUID(0x6fe69556,0x704a,0x47a0,(ctypes.c_ubyte*8)(0x8f,0x24,0xc2,0x8d,0x93,0x6f,0xda,0x47))
            user.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE,ctypes.POINTER(GUID),wintypes.DWORD]
            user.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            self.notification = user.RegisterPowerSettingNotification(self.hwnd,ctypes.byref(guid),0)
            callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
            user.CallWindowProcW.argtypes = [ctypes.c_void_p,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
            user.CallWindowProcW.restype = ctypes.c_ssize_t
            setter = user.SetWindowLongPtrW
            setter.argtypes = [wintypes.HWND,ctypes.c_int,ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
            def wndproc(hwnd,msg,wp,lp):
                try:
                    if msg == 0x0231:  # WM_ENTERSIZEMOVE
                        self.moving = True
                    elif msg == 0x0232:  # WM_EXITSIZEMOVE
                        self.moving = False
                        self.move_finished = True
                    elif msg == 0x0218:
                        if wp == 0x8013 and lp:
                            value = ctypes.c_uint32.from_address(lp+20).value
                            self.awake.clear() if value == 0 else self.awake.set()
                            self.wake.set()
                        elif wp == 4:
                            self.awake.clear()
                        elif wp in (7,18):
                            self.awake.set()
                            self.wake.set()
                except Exception:
                    logging.exception("Power event")
                return user.CallWindowProcW(self.old_proc,hwnd,msg,wp,lp)
            self.proc_ref = callback(wndproc)
            self.old_proc = setter(self.hwnd,-4,ctypes.cast(self.proc_ref,ctypes.c_void_p))
            self.user32 = user
        except Exception:
            logging.exception("Display power events unavailable")

    def close(self):
        if self.stop.is_set():
            return
        self.settings["x"],self.settings["y"] = self.root.winfo_x(),self.root.winfo_y()
        save_settings(self.settings)
        self.stop.set()
        self.wake.set()
        if self.old_proc:
            self.user32.SetWindowLongPtrW(self.hwnd,-4,self.old_proc)
        if self.notification:
            self.user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
            self.user32.UnregisterPowerSettingNotification(self.notification)
        self.root.destroy()


def main():
    if sys.platform != "win32":
        raise SystemExit("This widget requires Windows.")
    handler = RotatingFileHandler(BASE/"widget.log",maxBytes=262144,backupCount=1,encoding="utf-8")
    logging.basicConfig(handlers=[handler],level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    mutex = single_instance()
    if mutex is None:
        return
    try:
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError,OSError):
            pass
        root = tk.Tk()
        app = Widget(root)
        root.mainloop()
    except Exception:
        logging.exception("Widget startup failed")
        raise
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
