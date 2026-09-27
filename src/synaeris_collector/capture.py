import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import numpy as np


def hidden_process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_ffmpeg(explicit=""):
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(explicit)
        return explicit
    # Bundled static binary avoids DLL search problems of shared FFmpeg distributions.
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def process_elevated(pid):
    """Read TokenElevation, without changing privileges or security configuration."""
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not security.OpenProcessToken(process, 8, ctypes.byref(token)):
            return None
        value, size = wintypes.DWORD(), wintypes.DWORD()
        if not security.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
            return None
        return bool(value.value)
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


class GameWindow:
    def __init__(self, title="原神"):
        if os.name != "nt":
            raise RuntimeError("Live game capture requires Windows; dataset work also runs on Linux")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        try:
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
        import psutil
        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _):
            text = ctypes.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, text, 512)
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                name = psutil.Process(pid.value).name().lower()
                if name in {"yuanshen.exe", "genshinimpact.exe"} and self.user.IsWindowVisible(hwnd):
                    matches.append((hwnd, text.value, pid.value))
            except psutil.Error:
                pass
            return True
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.EnumWindows(callback, 0)
        if not matches:
            raise RuntimeError("未发现原神游戏窗口，请进入游戏后开始采集")
        self.hwnd, self.title, self.pid = next((m for m in matches if title in m[1]), matches[0])
        self.initial_rect = self.rect()

    def rect(self):
        rect = wintypes.RECT()
        point = wintypes.POINT(0, 0)
        if not self.user.GetClientRect(self.hwnd, ctypes.byref(rect)):
            raise RuntimeError("Game window closed")
        self.user.ClientToScreen(self.hwnd, ctypes.byref(point))
        return (point.x, point.y, point.x + rect.right, point.y + rect.bottom)

    def active(self):
        return self.user.GetForegroundWindow() == self.hwnd and not self.user.IsIconic(self.hwnd)

    def restore_for_collection(self):
        """One ordinary startup restore of the selected game, after integrity QC.

        This is an opt-in recorder startup feature. It neither acquires
        privileges nor changes Windows foreground/security policy.
        """
        permissions = self.permissions()
        if not permissions["input_permission_qualified"] or permissions["game_elevated"] is None:
            return False
        self.user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.ShowWindowAsync.restype = wintypes.BOOL
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.SetForegroundWindow.restype = wintypes.BOOL
        if self.user.IsIconic(self.hwnd):
            self.user.ShowWindowAsync(self.hwnd, 9)  # SW_RESTORE
        self.user.SetForegroundWindow(self.hwnd)
        return self.active()

    def permissions(self):
        collector, game = process_elevated(os.getpid()), process_elevated(self.pid)
        qualified = collector is True or (collector is False and game is False)
        return {"collector_elevated": collector, "game_elevated": game,
            "input_permission_qualified": qualified, "qualification_scope": "Windows integrity precondition only; live key/mouse smoke still required"}


class ScreenCapture:
    def __init__(self, window, backend="auto"):
        self.window = window
        self.privacy_guard = "foreground_verified_before_and_after_desktop_grab"
        self.rect = window.initial_rect
        self.camera = None
        self.mss = None
        self.diagnostic = ""
        self.allow_fallback = backend == "auto"
        if backend in {"auto", "dxcam"}:
            try:
                import dxcam
                import mss
                with mss.mss() as screen:
                    containing = [(i-1, m) for i, m in enumerate(screen.monitors[1:], 1)
                                  if m["left"] <= self.rect[0] < self.rect[2] <= m["left"]+m["width"]
                                  and m["top"] <= self.rect[1] < self.rect[3] <= m["top"]+m["height"]]
                if not containing:
                    raise RuntimeError("Game window crosses displays")
                idx, monitor = containing[0]
                self.camera = dxcam.create(output_idx=idx, output_color="BGR")
                self.local_rect = tuple(v - monitor["left" if i % 2 == 0 else "top"] for i, v in enumerate(self.rect))
                self.backend = "dxcam"
            except Exception as exc:
                if backend == "dxcam":
                    raise
                self.diagnostic = str(exc)
        if self.camera is None:
            import mss
            self.mss = mss.mss()
            self.backend = "mss"
        self.width = (self.rect[2] - self.rect[0]) // 2 * 2
        self.height = (self.rect[3] - self.rect[1]) // 2 * 2
        if self.width < 64 or self.height < 64:
            raise RuntimeError("Game client area is too small")
        self.last = np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def grab(self):
        if not self.window.active():
            return np.zeros_like(self.last), False, False
        # UAC / minimized fullscreen windows can report temporary client geometry.
        # Inspect geometry only after focus returns; never capture another desktop.
        if self.window.rect() != self.rect:
            raise RuntimeError("游戏窗口位置或大小已改变，请重新开始一个 Episode")
        if self.camera:
            try:
                frame = self.camera.grab(region=self.local_rect)
            except Exception as exc:
                if not self.allow_fallback:
                    raise
                self.diagnostic = f"Runtime DXcam failure; falling back to MSS: {exc}"
                try:
                    self.camera.release()
                except Exception:
                    pass
                self.camera = None
                import mss
                self.mss = mss.mss()
                self.backend = "mss"
                box = dict(left=self.rect[0], top=self.rect[1], width=self.width, height=self.height)
                frame = np.asarray(self.mss.grab(box))[:, :, :3]
        else:
            box = dict(left=self.rect[0], top=self.rect[1], width=self.width, height=self.height)
            frame = np.asarray(self.mss.grab(box))[:, :, :3]
        # Desktop duplication captures the output, not the HWND. Focus can change
        # between the preflight check above and the actual grab, so validate it
        # again before any pixels can enter the mother video. This closes the
        # race without trying to steal focus back from another application.
        if not self.window.active():
            return np.zeros_like(self.last), False, False
        fresh = frame is not None
        if fresh:
            self.last = np.ascontiguousarray(frame[:self.height, :self.width, :3])
        return self.last, True, fresh

    def close(self):
        if self.camera:
            self.camera.release()
        if self.mss:
            self.mss.close()


class SyntheticCapture:
    """Explicitly synthetic fixtures, excluded from real dataset exports."""
    backend = "synthetic"
    diagnostic = "not gameplay"
    width, height = 640, 360
    window = None

    def __init__(self):
        self.index = 0

    def grab(self):
        import cv2
        frame = np.full((self.height, self.width, 3), 35, dtype=np.uint8)
        cv2.putText(frame, "SYNTHETIC - NOT GAMEPLAY", (25, 60), 0, 0.7, (255, 255, 255), 2)
        cv2.circle(frame, (80 + self.index % 450, 170), 25, (40, 200, 90), -1)
        self.index += 1
        return frame, True, True

    def close(self):
        pass


def encoder_args(codec):
    if codec in {"av1_nvenc", "hevc_nvenc"}:
        return ["-c:v", codec, "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "6M", "-maxrate", "12M"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]


def qualify_encoder(ffmpeg, codec="auto"):
    errors = {}
    candidates = ["av1_nvenc", "hevc_nvenc", "libx264"] if codec == "auto" else [codec]
    for candidate in candidates:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "color=size=640x360:rate=30", "-frames:v", "3", *encoder_args(candidate), "-f", "null", "-"]
        result = subprocess.run(cmd, capture_output=True, timeout=20, **hidden_process_options())
        if result.returncode == 0:
            return candidate, errors
        errors[candidate] = result.stderr.decode("utf-8", errors="replace")[-1500:]
    raise RuntimeError(f"No working encoder: {errors}")


class VideoWriter:
    def __init__(self, path, config, width, height):
        self.ffmpeg = find_ffmpeg(config.ffmpeg)
        self.codec, self.qualifications = qualify_encoder(self.ffmpeg, config.codec)
        self.width, self.height = width, height
        self.log = (Path(path).parent / "encoder.log").open("wb")
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-f", "rawvideo",
               "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(config.fps),
               "-i", "pipe:0", "-an", *encoder_args(self.codec), "-pix_fmt", "yuv420p", str(path)]
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=self.log, **hidden_process_options())
        self.frames = 0

    def write(self, frame):
        if frame.shape != (self.height, self.width, 3) or frame.dtype != np.uint8:
            raise ValueError("Video frame shape/dtype mismatch")
        data = memoryview(np.ascontiguousarray(frame)).cast("B")
        while data:
            written = self.process.stdin.write(data)
            if not written:
                raise RuntimeError("Encoder pipe closed")
            data = data[written:]
        self.frames += 1

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("Encoder finalization timed out")
        finally:
            self.log.close()
        if code != 0:
            raise RuntimeError("FFmpeg failed; see encoder.log")
