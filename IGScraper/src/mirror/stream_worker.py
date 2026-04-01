"""
MirrorStreamWorker — scrcpy v2.4 protocol (fixed)
===================================================
Key fix: after reading the 64+12 byte handshake, the client must send
a 1-byte "dummy" acknowledgement before the server starts video.
Also: adb forward is kept alive until AFTER the decode loop exits.
"""

from __future__ import annotations

import datetime
import os
import re
import socket
import struct
import subprocess
import sys
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

# ── Windows: suppress console windows ────────────────────────────────────────
_WINDOWS_NO_WINDOW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
)

DEBUG_LOG = True
LOG_PATH  = Path.home() / "mirror_debug.log"

try:
    import av
    _AV_AVAILABLE = True
    _AV_VERSION   = getattr(av, "__version__", "unknown")
except ImportError as _av_err:
    _AV_AVAILABLE = False
    _AV_VERSION   = f"MISSING: {_av_err}"
except Exception as _av_err:
    # DLL load failure on Windows shows up as OSError/Exception, not ImportError
    _AV_AVAILABLE = False
    _AV_VERSION   = f"DLL ERROR: {_av_err}"

_SERVER_JAR         = Path(__file__).parent / "assets" / "scrcpy-server.jar"
_DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
_SCRCPY_VERSION     = "2.4"
_SOCKET_NAME        = "scrcpy"
_MAX_SIZE           = 720
_BITRATE            = 2_000_000
_MAX_FPS            = 30


# ── Logger ───────────────────────────────────────────────────────────────────

class _Logger:
    def __init__(self, path: Path, enabled: bool):
        self._enabled = enabled
        self._path    = path
        self._lock    = threading.Lock()
        if enabled:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"=== mirror_debug.log started {datetime.datetime.now()} ===\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"PyAV available: {_AV_AVAILABLE}  version: {_AV_VERSION}\n")
                f.write(f"server jar: {_SERVER_JAR.exists()}  ({_SERVER_JAR})\n\n")

    def log(self, msg: str):
        ts   = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if self._enabled:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

_log = _Logger(LOG_PATH, DEBUG_LOG)


# ── ADB helpers ──────────────────────────────────────────────────────────────

def _adb(serial: str, *args: str) -> list[str]:
    return ["adb", "-s", serial, *args]


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    _log.log(f"  RUN: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **_WINDOWS_NO_WINDOW)
        if r.stdout.strip():
            _log.log(f"  STDOUT: {r.stdout.strip()[:300]}")
        if r.stderr.strip():
            _log.log(f"  STDERR: {r.stderr.strip()[:300]}")
        _log.log(f"  RC: {r.returncode}")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        _log.log("  TIMEOUT")
        return -1, "", "timeout"
    except FileNotFoundError as e:
        _log.log(f"  FileNotFoundError: {e}")
        return -1, "", str(e)


def _check_device(serial: str) -> bool:
    _log.log(f"CHECK: device {serial}")
    rc, out, _ = _run(["adb", "devices"])
    for line in out.splitlines()[1:]:
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[0] == serial and parts[1] == "device":
            _log.log("CHECK: found ✓")
            return True
    _log.log("CHECK: NOT found ✗")
    return False


def _push_server(serial: str) -> bool:
    _log.log("PUSH: pushing scrcpy-server.jar")
    rc, _, _ = _run(_adb(serial, "push", str(_SERVER_JAR), _DEVICE_SERVER_PATH), timeout=30)
    _log.log(f"PUSH: {'ok ✓' if rc == 0 else 'FAILED ✗'}")
    return rc == 0


def _forward_port(serial: str, local_port: int) -> bool:
    _log.log(f"FORWARD: tcp:{local_port} → localabstract:{_SOCKET_NAME}")
    rc, _, _ = _run(_adb(serial, "forward", f"tcp:{local_port}", f"localabstract:{_SOCKET_NAME}"))
    _log.log(f"FORWARD: {'ok ✓' if rc == 0 else 'FAILED ✗'}")
    return rc == 0


def _remove_forward(serial: str, local_port: int):
    _run(_adb(serial, "forward", "--remove", f"tcp:{local_port}"), timeout=3)


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception as e:
            _log.log(f"  recv error after {len(buf)}/{n} bytes: {e}")
            return None
        if not chunk:
            _log.log(f"  recv EOF after {len(buf)}/{n} bytes")
            return None
        buf += chunk
    return buf


# ── Worker ───────────────────────────────────────────────────────────────────

class MirrorStreamWorker(QThread):

    frame_ready   = pyqtSignal(QImage)
    state_changed = pyqtSignal(str)
    fps_updated   = pyqtSignal(float)

    def __init__(self, serial: str, parent=None):
        super().__init__(parent)
        self.serial          = serial
        self._stop_requested = False
        self._server_proc: Optional[subprocess.Popen] = None

    def request_stop(self):
        _log.log("STOP requested")
        self._stop_requested = True
        self._kill_server()

    stop = request_stop

    def run(self):
        try:
            self._run_safe()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            _log.log(f"FATAL UNCAUGHT in run(): {e}\n{tb}")
            try:
                self.state_changed.emit(f"error:Fatal crash — {e} (see ~/mirror_debug.log)")
            except Exception:
                pass

    def _run_safe(self):
        _log.log(f"=== run() START serial={self.serial} ===")

        if not _AV_AVAILABLE:
            self.state_changed.emit(f"error:PyAV unavailable — {_AV_VERSION}")
            return

        self.state_changed.emit("connecting")

        if not _check_device(self.serial):
            self.state_changed.emit(f"error:Device {self.serial} not found")
            return

        if not _SERVER_JAR.exists():
            self.state_changed.emit(f"error:scrcpy-server.jar missing at {_SERVER_JAR}")
            return

        if not _push_server(self.serial):
            self.state_changed.emit("error:Failed to push scrcpy-server.jar")
            return

        local_port = _find_free_port()
        _log.log(f"Using local port {local_port}")

        if not _forward_port(self.serial, local_port):
            self.state_changed.emit("error:adb forward failed")
            return

        # Keep forward alive for the full session — only remove after decode ends
        try:
            self._run_session(local_port)
        finally:
            _log.log("Session ended, removing forward")
            _remove_forward(self.serial, local_port)
            self._kill_server()

        if not self._stop_requested:
            _log.log("Emitting disconnected")
            self.state_changed.emit("disconnected")

        _log.log("=== run() END ===")

    def _run_session(self, local_port: int):
        # Launch the scrcpy server on device
        server_cmd = _adb(self.serial, "shell",
            f"CLASSPATH={_DEVICE_SERVER_PATH}",
            "app_process", "/",
            "com.genymobile.scrcpy.Server",
            _SCRCPY_VERSION,
            "log_level=debug",
            f"max_size={_MAX_SIZE}",
            f"video_bit_rate={_BITRATE}",
            f"max_fps={_MAX_FPS}",
            "tunnel_forward=true",
            "send_frame_meta=true",
            "control=false",
            "audio=false",
            "show_touches=false",
            "stay_awake=false",
            "power_off_on_close=false",
            "clipboard_autosync=false",
        )
        _log.log(f"Launching server: {' '.join(server_cmd)}")

        try:
            self._server_proc = subprocess.Popen(
                server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_WINDOWS_NO_WINDOW,
            )
        except Exception as e:
            self.state_changed.emit(f"error:Cannot launch server — {e}")
            return

        # Give the server time to bind its socket
        time.sleep(0.8)

        if self._server_proc.poll() is not None:
            out = self._server_proc.stdout.read(2000).decode(errors="replace")
            err = self._server_proc.stderr.read(2000).decode(errors="replace")
            _log.log(f"Server died immediately! stdout={out!r} stderr={err!r}")
            self.state_changed.emit(f"error:scrcpy server crashed — {err[:200] or out[:200]}")
            return

        # Connect video socket
        _log.log("Connecting video socket...")
        video_sock = self._connect_socket(local_port, timeout=5.0)
        if video_sock is None:
            # Dump server stderr before giving up
            try:
                err = self._server_proc.stderr.read(2000).decode(errors="replace")
                _log.log(f"Server stderr: {err!r}")
            except Exception:
                pass
            self.state_changed.emit("error:Could not connect to scrcpy video socket")
            return

        _log.log("Video socket connected ✓")

        # ── scrcpy v2.4 handshake ──────────────────────────────────────────
        # Per server.c connect_and_read_byte(): read 1 confirmation byte first,
        # then device_read_info() reads 64-byte device name + 12-byte codec info.
        # 1) Read 1 dummy byte (server liveness confirmation)
        _log.log("Reading 1 dummy byte...")
        dummy = _recv_exactly(video_sock, 1)
        if dummy is None:
            self.state_changed.emit("error:No dummy byte from scrcpy server")
            video_sock.close()
            return
        _log.log(f"Dummy byte: 0x{dummy.hex()}")

        # 2) Read 64-byte device name
        _log.log("Reading device name (64 bytes)...")
        device_name_raw = _recv_exactly(video_sock, 64)
        if device_name_raw is None:
            self.state_changed.emit("error:No device name in handshake")
            video_sock.close()
            return
        device_name_str = device_name_raw.rstrip(b"\x00").decode(errors="replace")
        _log.log(f"Device name: {device_name_str!r}")

        # 3) Read 12-byte codec info (codec_id u32 + width u32 + height u32)
        _log.log("Reading codec info (12 bytes)...")
        codec_raw = _recv_exactly(video_sock, 12)
        if codec_raw is None:
            self.state_changed.emit("error:No codec info in handshake")
            video_sock.close()
            return
        codec_id, width, height = struct.unpack(">III", codec_raw)
        _log.log(f"Codec: id=0x{codec_id:08x}  resolution={width}x{height}")
        _log.log("Handshake complete - starting frame pump")

        # ── Pipe socket → decoder via queue ───────────────────────────────
        # Using a queue instead of an OS pipe so each .put() is exactly one
        # complete scrcpy frame and the decoder gets it with exact boundaries.
        # None sentinel signals end-of-stream.
        frame_queue = queue.Queue(maxsize=8)

        bytes_pumped  = [0]
        frames_pumped = [0]

        def _pump():
            _log.log("PUMP: thread started")
            try:
                while not self._stop_requested:
                    # 12-byte frame header: pts(8 bytes BE) + size(4 bytes BE)
                    hdr = _recv_exactly(video_sock, 12)
                    if hdr is None:
                        _log.log("PUMP: header read failed — socket closed")
                        break

                    pts  = struct.unpack(">Q", hdr[:8])[0]
                    size = struct.unpack(">I", hdr[8:12])[0]
                    _log.log(f"PUMP: frame pts={pts} size={size}")

                    if size == 0:
                        _log.log("PUMP: zero-size frame, ending")
                        break

                    data = _recv_exactly(video_sock, size)
                    if data is None:
                        _log.log("PUMP: data read failed")
                        break

                    frames_pumped[0] += 1
                    bytes_pumped[0]  += size

                    try:
                        frame_queue.put(data, timeout=5)
                    except queue.Full:
                        _log.log("PUMP: queue full, dropping frame")

            except Exception as e:
                _log.log(f"PUMP: exception: {e}")
            finally:
                _log.log(f"PUMP: thread ending — {frames_pumped[0]} frames, {bytes_pumped[0]} bytes")
                try:
                    frame_queue.put(None, timeout=2)  # EOF sentinel
                except Exception:
                    pass

        pump_thread = threading.Thread(target=_pump, daemon=True, name="scrcpy-pump")
        pump_thread.start()

        # ── Decode loop ────────────────────────────────────────────────────
        self._decode_h264(frame_queue)

        pump_thread.join(timeout=3)
        try:
            video_sock.close()
        except Exception:
            pass

    def _decode_h264(self, frame_queue: 'queue.Queue'):
        """
        Decode raw H.264 NAL units from `pipe` using a manually-opened
        CodecContext — bypassing av.open() / container / demuxer entirely.

        Why: av.open(pipe, format="h264") uses FFmpeg's h264 raw-bitstream
        demuxer, which:
          • opens the codec context lazily (is_open=False at config time,
            so thread_type / options are silently discarded)
          • parses NAL units incorrectly when they arrive in small chunks,
            producing packets that are never flagged as keyframes so the
            decoder never starts
          • has no way to receive PTS/DTS from the scrcpy frame header

        Direct CodecContext approach:
          1. Open the codec BEFORE feeding any data → is_open=True
          2. Build one av.Packet per scrcpy frame (already framed by the pump)
          3. packet.decode() on a frame-aligned packet works reliably
        """
        _log.log("DECODE: opening direct CodecContext (h264)...")
        self.state_changed.emit("streaming")

        fps_frames  = 0
        fps_ts      = time.monotonic()
        codec_ctx   = None
        pkt_count   = 0
        frame_count = 0

        try:
            codec = av.codec.Codec("h264", "r")
            codec_ctx = av.codec.CodecContext.create(codec)
            # Set options BEFORE open — context is not yet open here,
            # but create() + open() is the correct PyAV lifecycle.
            codec_ctx.thread_type  = "NONE"
            codec_ctx.thread_count = 1
            codec_ctx.options      = {
                "flags":  "low_delay",
                "flags2": "fast",
            }
            codec_ctx.open()
            _log.log(f"DECODE: CodecContext open={codec_ctx.is_open} "
                     f"thread_type={codec_ctx.thread_type} "
                     f"thread_count={codec_ctx.thread_count}")

            # The pump writes raw scrcpy frame payloads into `pipe`.
            # Each write() is one complete scrcpy frame = one or more
            # complete NAL units.  We read them back in the same chunks
            # the pump wrote (pipe OS buffer keeps them together for
            # moderate frame sizes).
            #
            # We accumulate a bytearray and carve out NAL units by the
            # Annex-B start code (00 00 00 01 or 00 00 01) so each
            # av.Packet contains exactly one complete access unit.
            def _decode_packet(raw: bytes):
                nonlocal frame_count, fps_frames, fps_ts
                pkt        = av.Packet(raw)
                frames_out = 0
                try:
                    for frame in codec_ctx.decode(pkt):
                        frame_count += 1
                        frames_out  += 1
                        if self._stop_requested:
                            return frames_out
                        arr  = frame.to_ndarray(format="rgb24")
                        fh, fw, ch = arr.shape
                        qimg = QImage(
                            arr.tobytes(), fw, fh, ch * fw,
                            QImage.Format.Format_RGB888,
                        ).copy()
                        self.frame_ready.emit(qimg)
                        fps_frames += 1
                        now = time.monotonic()
                        if now - fps_ts >= 1.0:
                            fps = fps_frames / (now - fps_ts)
                            _log.log(f"DECODE: {fps:.1f} fps (frames={frame_count})")
                            self.fps_updated.emit(fps)
                            fps_frames = 0
                            fps_ts     = now
                except Exception as e:
                    # av.AVError / av.error.InvalidDataError hierarchy varies by
                    # PyAV version — catch all and just skip non-decodable packets
                    # (e.g. SPS/PPS config NALs that scrcpy sends as the first frame)
                    _log.log(f"DECODE: skipping packet ({type(e).__name__}: {e})")
                return frames_out

            _log.log("DECODE: entering queue loop")
            while not self._stop_requested:
                try:
                    raw = frame_queue.get(timeout=2)
                except queue.Empty:
                    # No data yet — check stop flag and retry
                    continue
                if raw is None:
                    _log.log("DECODE: EOF sentinel received")
                    break

                pkt_count += 1
                frames_out = _decode_packet(raw)
                _log.log(f"DECODE: frame#{pkt_count} {len(raw)}B → {frames_out} decoded")

                if self._stop_requested:
                    break

            # Flush decoder — pass None to drain buffered frames
            _log.log("DECODE: flushing decoder...")
            try:
                for frame in codec_ctx.decode(None):
                    frame_count += 1
                    arr  = frame.to_ndarray(format="rgb24")
                    fh, fw, ch = arr.shape
                    qimg = QImage(
                        arr.tobytes(), fw, fh, ch * fw,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self.frame_ready.emit(qimg)
            except Exception as e:
                _log.log(f"DECODE: flush error: {e}")
            _log.log(f"DECODE: done — total frames={frame_count}")

        except Exception as e:
            _log.log(f"DECODE: exception: {e}")
            import traceback
            _log.log(traceback.format_exc())
            if not self._stop_requested:
                self.state_changed.emit(f"error:Decode error — {e}")
        finally:
            _log.log("DECODE: loop exited")
            if codec_ctx:
                try:
                    codec_ctx.close()
                except Exception:
                    pass
            # (pipe replaced by queue — nothing to close here)

    def _connect_socket(self, port: int, timeout: float) -> Optional[socket.socket]:
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            if self._stop_requested:
                return None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(("127.0.0.1", port))
                s.settimeout(None)
                _log.log(f"Socket connected on attempt {attempts + 1} ✓")
                return s
            except (ConnectionRefusedError, OSError):
                attempts += 1
                time.sleep(0.2)
        _log.log(f"Socket connect failed after {attempts} attempts")
        return None

    def _kill_server(self):
        proc, self._server_proc = self._server_proc, None
        if proc and proc.poll() is None:
            _log.log("Killing server process")
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()