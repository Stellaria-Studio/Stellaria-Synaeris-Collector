"""Focused keyboard/button hooks and Raw Input mouse deltas for locked cameras."""
import ctypes
from ctypes import wintypes as wt
import threading

MARKERS = {0x75: "THINK", 0x76: "MISTAKE", 0x77: "DISCOVERY", 0x78: "SUBGOAL_COMPLETE"}


class RawMouse:
    def __init__(self, on_delta, failure):
        self.on_delta, self.failure = on_delta, failure
        self.hwnd = None
        self.thread = threading.Thread(target=self.run, name="synaeris-raw-input", daemon=True)
        self.ready = threading.Event()
        self.error = None

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Raw Input startup timeout")
        if self.error:
            raise self.error

    def run(self):
        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

            class WindowClass(ctypes.Structure):
                _fields_ = [("style", wt.UINT), ("proc", proc_type), ("cls_extra", ctypes.c_int),
                            ("win_extra", ctypes.c_int), ("instance", wt.HINSTANCE), ("icon", wt.HICON),
                            ("cursor", wt.HANDLE), ("background", wt.HBRUSH),
                            ("menu", wt.LPCWSTR), ("name", wt.LPCWSTR)]

            class Device(ctypes.Structure):
                _fields_ = [("page", wt.USHORT), ("usage", wt.USHORT), ("flags", wt.DWORD), ("target", wt.HWND)]

            class Header(ctypes.Structure):
                _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("device", wt.HANDLE), ("wparam", wt.WPARAM)]

            class Buttons(ctypes.Structure):
                _fields_ = [("flags", wt.USHORT), ("data", wt.USHORT)]

            class ButtonUnion(ctypes.Union):
                _fields_ = [("buttons", wt.ULONG), ("parts", Buttons)]

            class Mouse(ctypes.Structure):
                _fields_ = [("flags", wt.USHORT), ("buttons", ButtonUnion), ("raw_buttons", wt.ULONG),
                            ("dx", wt.LONG), ("dy", wt.LONG), ("extra", wt.ULONG)]

            user.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            user.DefWindowProcW.restype = ctypes.c_ssize_t
            user.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT), wt.UINT]
            user.GetRawInputData.restype = wt.UINT
            user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
            user.CreateWindowExW.restype = wt.HWND
            user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
            user.RegisterRawInputDevices.argtypes = [ctypes.POINTER(Device), wt.UINT, wt.UINT]
            user.DestroyWindow.argtypes = [wt.HWND]
            user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            user.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
            kernel.GetModuleHandleW.argtypes = [wt.LPCWSTR]
            kernel.GetModuleHandleW.restype = wt.HMODULE

            @proc_type
            def callback(hwnd, msg, wparam, lparam):
                try:
                    if msg == 0x00FF:
                        size = wt.UINT()
                        user.GetRawInputData(wt.HANDLE(lparam), 0x10000003, None, ctypes.byref(size), ctypes.sizeof(Header))
                        if size.value <= 65536 and size.value >= ctypes.sizeof(Header):
                            buffer = ctypes.create_string_buffer(size.value)
                            got = user.GetRawInputData(wt.HANDLE(lparam), 0x10000003, buffer, ctypes.byref(size), ctypes.sizeof(Header))
                            header = Header.from_buffer(buffer)
                            if got == size.value and header.type == 0 and size.value >= ctypes.sizeof(Header)+ctypes.sizeof(Mouse):
                                mouse = Mouse.from_buffer(buffer, ctypes.sizeof(Header))
                                if mouse.dx or mouse.dy:
                                    self.on_delta(mouse.dx, mouse.dy, bool(mouse.flags & 1),
                                                  int(header.device or 0), int(mouse.extra))
                    elif msg == 0x0010:
                        user.DestroyWindow(hwnd)
                        return 0
                    elif msg == 0x0002:
                        user.PostQuitMessage(0)
                        return 0
                except Exception as exc:
                    self.failure("RAW_INPUT_CALLBACK", exc)
                return user.DefWindowProcW(hwnd, msg, wparam, lparam)

            name = f"SynaerisRawMouse_{id(self)}"
            instance = kernel.GetModuleHandleW(None)
            cls = WindowClass(0, callback, 0, 0, instance, None, None, None, None, name)
            if not user.RegisterClassW(ctypes.byref(cls)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = user.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, wt.HWND(-3), None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            device = Device(1, 2, 0x100, self.hwnd)  # INPUTSINK, never NOLEGACY: game retains normal input.
            if not user.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(Device)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            message = wt.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
            # RIDEV_REMOVE unregisters this process only.
            remove = Device(1, 2, 1, None)
            user.RegisterRawInputDevices(ctypes.byref(remove), 1, ctypes.sizeof(Device))
            user.UnregisterClassW(name, instance)
        except Exception as exc:
            self.error = exc
            self.failure("RAW_INPUT", exc)
            self.ready.set()

    def close(self):
        if self.hwnd:
            user = ctypes.WinDLL("user32")
            user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            user.PostMessageW(self.hwnd, 0x0010, 0, 0)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Raw Input shutdown timeout")


class InputRecorder:
    def __init__(self, window, writer, bridge, stop, failure):
        self.window, self.writer, self.bridge = window, writer, bridge
        self.stop, self.failure = stop, failure
        self.keys = set()
        self.mouse_buttons = set()
        self.raw = RawMouse(self.delta, failure)
        self.keyboard = self.mouse = None

    def actor(self, injected):
        if not injected:
            return "HUMAN"
        # SendInput injection cannot establish the producer's identity.
        return "UNKNOWN_INJECTED"

    def key_filter(self, msg, data):
        if not self.window.active():
            return False
        injected = bool(data.flags & 0x10)
        down = msg in {0x100, 0x104}
        key = int(data.vkCode)
        repeat = down and key in self.keys
        if down:
            self.keys.add(key)
        else:
            self.keys.discard(key)
        self.writer.emit("input", "KEY_DOWN" if down else "KEY_UP",
            dict(vk=key, scan=int(data.scanCode), injected=injected, repeat=repeat,
                 bgi_tool_active=bool(self.bridge.active_tools)),
            source="win32_low_level_hook", actor=self.actor(injected))
        if down and not repeat and not injected:
            if key in MARKERS:
                self.writer.mark(MARKERS[key])
            elif key == 0x79:  # F10 emergency stop
                self.stop()
        return False

    def mouse_filter(self, msg, data):
        if not self.window.active() or msg == 0x200:
            return False  # Relative motion comes from Raw Input, never cursor differences.
        injected = bool(data.flags & 1)
        buttons = {0x201: ("LEFT", True), 0x202: ("LEFT", False), 0x204: ("RIGHT", True),
                   0x205: ("RIGHT", False), 0x207: ("MIDDLE", True), 0x208: ("MIDDLE", False),
                   0x20B: ("XBUTTON", True), 0x20C: ("XBUTTON", False)}
        if msg in buttons:
            button, down = buttons[msg]
            if button == "XBUTTON":
                button = "X1" if (int(data.mouseData) >> 16) == 1 else "X2"
            if down:
                self.mouse_buttons.add(button)
            else:
                self.mouse_buttons.discard(button)
            rect = self.window.initial_rect
            payload = dict(button=button, x=int(data.pt.x)-rect[0], y=int(data.pt.y)-rect[1], injected=injected)
            kind = "BUTTON_DOWN" if down else "BUTTON_UP"
        elif msg in {0x20A, 0x20E}:
            signed = ctypes.c_short((int(data.mouseData) >> 16) & 0xFFFF).value
            payload, kind = dict(delta=signed / 120, horizontal=msg == 0x20E, injected=injected), "WHEEL"
        else:
            return False
        self.writer.emit("input", kind, payload, source="win32_low_level_hook", actor=self.actor(injected))
        return False

    def delta(self, dx, dy, absolute, device_handle=0, extra_info=0):
        if self.window.active():
            self.writer.emit("input", "MOUSE_ABSOLUTE" if absolute else "MOUSE_DELTA",
                dict(dx=dx, dy=dy, unit="normalized_0_65535" if absolute else "raw_counts",
                     absolute=absolute, device_handle=device_handle, extra_info=extra_info,
                     injection_status="unavailable", provenance_qualified=False,
                     bgi_tool_active=bool(self.bridge.active_tools)),
                source="win32_raw_input", actor="UNKNOWN")

    def start(self):
        from pynput import keyboard, mouse
        self.raw.start()
        self.keyboard = keyboard.Listener(win32_event_filter=self.key_filter)
        self.mouse = mouse.Listener(win32_event_filter=self.mouse_filter)
        self.keyboard.start()
        self.mouse.start()
        self.keyboard.wait()
        self.mouse.wait()

    def focus_lost(self):
        if self.keys or self.mouse_buttons:
            self.writer.emit("input", "INPUT_STATE_RESET", {"keys": sorted(self.keys),
                "buttons": sorted(self.mouse_buttons), "reason": "focus_lost"}, source="collector")
            self.keys.clear()
            self.mouse_buttons.clear()

    def close(self):
        for listener in (self.keyboard, self.mouse):
            if listener:
                listener.stop()
                listener.join(timeout=3)
        self.raw.close()
