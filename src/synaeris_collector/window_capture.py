"""HWND-targeted WGC capture; bounded latest frame, no desktop fallback."""
import ctypes
from ctypes import wintypes
import threading
import time
import numpy as np


class WindowCapture:
    backend = "wgc"
    diagnostic = "Window-targeted Windows Graphics Capture; no desktop fallback"

    def __init__(self, window, background=False):
        from windows_capture import WindowsCapture
        self.window, self.background = window, background
        self.rect = window.initial_rect
        self.width = (self.rect[2] - self.rect[0]) // 2 * 2
        self.height = (self.rect[3] - self.rect[1]) // 2 * 2
        if self.width < 64 or self.height < 64 or window.user.IsIconic(window.hwnd):
            raise RuntimeError("后台窗口录像需要游戏已恢复、窗口化或无边框；请勿最小化")
        self.lock, self.ready = threading.Lock(), threading.Event()
        self.last = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        self.generation = self.read_generation = 0
        self.callback_qpc_ns = self.native_timespan = None
        self.error = None
        self.closed = False
        self.available = False
        self.metadata = {}
        self.capture = WindowsCapture(window_hwnd=int(window.hwnd), cursor_capture=False,
                                      minimum_update_interval=16)

        @self.capture.event
        def on_frame_arrived(frame, control):
            try:
                stamp = time.perf_counter_ns()
                pixels = self.client_pixels(frame.frame_buffer)
                with self.lock:
                    self.last = np.ascontiguousarray(pixels[:, :, :3]).copy()
                    self.callback_qpc_ns, self.native_timespan = stamp, int(frame.timespan)
                    self.generation += 1
                self.ready.set()
            except Exception as error:
                self.error = str(error)
                self.ready.set()
                control.stop()

        @self.capture.event
        def on_closed():
            self.closed = True
            self.ready.set()

        self.control = self.capture.start_free_threaded()
        if not self.ready.wait(10) or self.error or not self.generation:
            self.close()
            raise RuntimeError("WGC startup failed: " + str(self.error or "no window frame"))

    def client_pixels(self, frame):
        client_width, client_height = self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]
        if frame.shape[:2] == (client_height, client_width):
            return frame[:self.height, :self.width]
        bounds = wintypes.RECT()
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
        if dwm.DwmGetWindowAttribute(self.window.hwnd, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)):
            raise RuntimeError("WGC window bounds unavailable")
        if frame.shape[:2] != (bounds.bottom - bounds.top, bounds.right - bounds.left):
            raise RuntimeError("WGC content/client dimensions do not agree")
        x, y = self.rect[0] - bounds.left, self.rect[1] - bounds.top
        if not (0 <= x <= frame.shape[1] - self.width and 0 <= y <= frame.shape[0] - self.height):
            raise RuntimeError("WGC client crop escapes captured window")
        return frame[y:y + self.height, x:x + self.width]

    def grab(self):
        if self.error or self.closed:
            raise RuntimeError("WGC session ended: " + str(self.error or "window closed"))
        active = self.window.active()
        minimized = bool(self.window.user.IsIconic(self.window.hwnd))
        if not minimized and self.window.rect() != self.rect:
            raise RuntimeError("游戏窗口位置或大小已改变，请重新开始一个 Episode")
        with self.lock:
            age = time.perf_counter_ns() - self.callback_qpc_ns if self.callback_qpc_ns else None
            self.available = not minimized and (active or self.background) and age is not None and age <= 2_000_000_000
            fresh = self.available and self.generation != self.read_generation
            self.read_generation = self.generation
            self.metadata = {"capture_available": self.available, "source_callback_qpc_ns": self.callback_qpc_ns,
                             "source_native_timespan": self.native_timespan, "source_frame_age_ns": age,
                             "source_clock_scope": "callback QPC; native timespan retained without assumed conversion"}
            return self.last.copy() if self.available else np.zeros_like(self.last), active, fresh

    def close(self):
        if getattr(self, "control", None):
            self.control.stop()
            self.control = None
