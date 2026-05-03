"""Small arrow-key TTS output browser/player."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from wavebench.tui import styles as _styles
from wavebench.tui.input import _read_key_or_resize
from wavebench.tui.styles import S, _box_bot, _box_row, _box_top, _truncate, _tw

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".pcm"}
_NATIVE_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".pcm"}
_RAW_PCM_SAMPLE_RATE = 24_000
_RAW_PCM_CHANNELS = 1
_RAW_PCM_SAMPLE_WIDTH = 2
_PULSE_SAMPLE_S16LE = 3
_PULSE_STREAM_PLAYBACK = 1
_SFM_READ = 0x10
_MPG123_OK = 0
_MPG123_DONE = -12
_MPG123_NEW_FORMAT = -11
_MPG123_MONO = 1
_MPG123_STEREO = 2
_MPG123_ENC_SIGNED_16 = 0x0D0
_MPG123_READ_BYTES = 8192
_ENCODED_PLAYBACK_RATE = 44_100
_ENCODED_PLAYBACK_CHANNELS = 2


class _PulseSampleSpec(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


class _SndFileInfo(ctypes.Structure):
    _fields_ = [
        ("frames", ctypes.c_int64),
        ("samplerate", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("format", ctypes.c_int),
        ("sections", ctypes.c_int),
        ("seekable", ctypes.c_int),
    ]


@dataclass(frozen=True)
class _DecodedPcm:
    data: bytes
    sample_rate: int
    channels: int


class _NativeAudioHandle:
    """Small wrapper that keeps miniaudio objects alive until playback stops."""

    def __init__(self, device: Any, stream: Any) -> None:
        self.device = device
        self.stream = stream

    def stop(self) -> None:
        self.stream = None
        self.device.close()


class _ThreadedAudioHandle:
    """Managed native playback running on a background thread."""

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self.thread = thread
        self.stop_event = stop_event

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)


def _load_miniaudio() -> Any | None:
    try:
        import miniaudio
    except Exception:
        return None
    return miniaudio


def _load_pulse_simple() -> Any | None:
    lib_path = ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError:
        return None

    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(_PulseSampleSpec),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.pa_simple_drain.restype = ctypes.c_int
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    lib.pa_simple_free.restype = None
    return lib


def _load_sndfile() -> Any | None:
    lib_path = ctypes.util.find_library("sndfile") or "libsndfile.so.1"
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError:
        return None

    lib.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_SndFileInfo)]
    lib.sf_open.restype = ctypes.c_void_p
    lib.sf_readf_short.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int64]
    lib.sf_readf_short.restype = ctypes.c_int64
    lib.sf_close.argtypes = [ctypes.c_void_p]
    lib.sf_close.restype = ctypes.c_int
    return lib


def _load_mpg123() -> Any | None:
    lib_path = ctypes.util.find_library("mpg123") or "libmpg123.so.0"
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError:
        return None

    try:
        lib.mpg123_init.restype = ctypes.c_int
        lib.mpg123_new.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.mpg123_new.restype = ctypes.c_void_p
        lib.mpg123_format_none.argtypes = [ctypes.c_void_p]
        lib.mpg123_format_none.restype = ctypes.c_int
        lib.mpg123_format.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_int, ctypes.c_int]
        lib.mpg123_format.restype = ctypes.c_int
        lib.mpg123_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.mpg123_open.restype = ctypes.c_int
        lib.mpg123_getformat.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_long),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.mpg123_getformat.restype = ctypes.c_int
        lib.mpg123_read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.mpg123_read.restype = ctypes.c_int
        lib.mpg123_close.argtypes = [ctypes.c_void_p]
        lib.mpg123_close.restype = ctypes.c_int
        lib.mpg123_delete.argtypes = [ctypes.c_void_p]
        lib.mpg123_delete.restype = None
        lib.mpg123_rates.argtypes = [
            ctypes.POINTER(ctypes.POINTER(ctypes.c_long)),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.mpg123_rates.restype = None
    except AttributeError:
        return None

    if lib.mpg123_init() != _MPG123_OK:
        return None
    return lib


def _decode_sndfile(filepath: str) -> _DecodedPcm | None:
    lib = _load_sndfile()
    if lib is None:
        return None

    info = _SndFileInfo()
    try:
        handle = lib.sf_open(os.fsencode(filepath), _SFM_READ, ctypes.byref(info))
    except Exception:
        return None
    if not handle:
        return None

    chunks: list[bytes] = []
    try:
        if info.samplerate <= 0 or info.channels <= 0:
            return None
        chunk_frames = max(1, min(info.samplerate, 48_000))
        buffer = (ctypes.c_int16 * (chunk_frames * info.channels))()
        while True:
            frames_read = lib.sf_readf_short(handle, buffer, chunk_frames)
            if frames_read <= 0:
                break
            chunks.append(ctypes.string_at(buffer, frames_read * info.channels * _RAW_PCM_SAMPLE_WIDTH))
    except Exception:
        return None
    finally:
        lib.sf_close(handle)

    data = b"".join(chunks)
    if not data:
        return None
    return _DecodedPcm(data=data, sample_rate=info.samplerate, channels=info.channels)


def _mpg123_rates(lib: Any) -> list[int]:
    fallback = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]
    rates_ptr = ctypes.POINTER(ctypes.c_long)()
    count = ctypes.c_size_t(0)
    try:
        lib.mpg123_rates(ctypes.byref(rates_ptr), ctypes.byref(count))
    except Exception:
        return fallback
    if not rates_ptr or count.value <= 0:
        return fallback
    return [int(rates_ptr[idx]) for idx in range(count.value)]


def _configure_mpg123_signed16(lib: Any, handle: Any) -> bool:
    try:
        if lib.mpg123_format_none(handle) != _MPG123_OK:
            return False
        configured = False
        for rate in _mpg123_rates(lib):
            ret = lib.mpg123_format(
                handle,
                rate,
                _MPG123_MONO | _MPG123_STEREO,
                _MPG123_ENC_SIGNED_16,
            )
            configured = configured or ret == _MPG123_OK
        return configured
    except Exception:
        return False


def _mpg123_format(lib: Any, handle: Any) -> tuple[int, int, int] | None:
    sample_rate = ctypes.c_long(0)
    channels = ctypes.c_int(0)
    encoding = ctypes.c_int(0)
    try:
        ret = lib.mpg123_getformat(
            handle,
            ctypes.byref(sample_rate),
            ctypes.byref(channels),
            ctypes.byref(encoding),
        )
    except Exception:
        return None
    if ret != _MPG123_OK:
        return None
    if sample_rate.value <= 0 or channels.value <= 0:
        return None
    if encoding.value != _MPG123_ENC_SIGNED_16:
        return None
    return int(sample_rate.value), int(channels.value), int(encoding.value)


def _decode_mpg123(filepath: str) -> _DecodedPcm | None:
    lib = _load_mpg123()
    if lib is None:
        return None

    error = ctypes.c_int(0)
    try:
        handle = lib.mpg123_new(None, ctypes.byref(error))
    except Exception:
        return None
    if not handle:
        return None

    try:
        if not _configure_mpg123_signed16(lib, handle):
            return None
        if lib.mpg123_open(handle, os.fsencode(filepath)) != _MPG123_OK:
            return None
        fmt = _mpg123_format(lib, handle)
        if fmt is None:
            return None
        sample_rate, channels, _encoding = fmt

        chunks: list[bytes] = []
        buffer = (ctypes.c_ubyte * _MPG123_READ_BYTES)()
        while True:
            bytes_read = ctypes.c_size_t(0)
            ret = lib.mpg123_read(
                handle,
                buffer,
                _MPG123_READ_BYTES,
                ctypes.byref(bytes_read),
            )
            if bytes_read.value:
                chunks.append(ctypes.string_at(buffer, bytes_read.value))
            if ret == _MPG123_DONE:
                break
            if ret == _MPG123_NEW_FORMAT:
                new_fmt = _mpg123_format(lib, handle)
                if new_fmt is None or new_fmt[:2] != (sample_rate, channels):
                    return None
                continue
            if ret != _MPG123_OK:
                return None
    except Exception:
        return None
    finally:
        try:
            lib.mpg123_close(handle)
        finally:
            lib.mpg123_delete(handle)

    data = b"".join(chunks)
    if not data:
        return None
    return _DecodedPcm(data=data, sample_rate=sample_rate, channels=channels)


def _decode_encoded_audio(filepath: str) -> _DecodedPcm | None:
    if os.path.splitext(filepath)[1].lower() == ".mp3":
        decoded = _decode_mpg123(filepath)
        if decoded is not None:
            return decoded
    return _decode_sndfile(filepath)


def _pulse_pcm_worker(
    lib: Any,
    pulse_handle: Any,
    pcm_data: bytes,
    chunk_bytes: int,
    stop_event: threading.Event,
) -> None:
    error = ctypes.c_int(0)
    offset = 0
    try:
        while offset < len(pcm_data) and not stop_event.is_set():
            end = min(len(pcm_data), offset + chunk_bytes)
            chunk = pcm_data[offset:end]
            if lib.pa_simple_write(pulse_handle, chunk, len(chunk), ctypes.byref(error)) < 0:
                return
            offset = end
        if not stop_event.is_set():
            lib.pa_simple_drain(pulse_handle, ctypes.byref(error))
    finally:
        lib.pa_simple_free(pulse_handle)


def _start_pulse_data(
    pcm_data: bytes,
    sample_rate: int,
    channels: int,
) -> _ThreadedAudioHandle | None:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return None
    if not pcm_data or sample_rate <= 0 or channels <= 0:
        return None

    lib = _load_pulse_simple()
    if lib is None:
        return None

    error = ctypes.c_int(0)
    spec = _PulseSampleSpec(_PULSE_SAMPLE_S16LE, sample_rate, channels)
    pulse_handle = lib.pa_simple_new(
        None,
        b"WaveBench",
        _PULSE_STREAM_PLAYBACK,
        None,
        b"TTS output",
        ctypes.byref(spec),
        None,
        None,
        ctypes.byref(error),
    )
    if not pulse_handle:
        return None

    frame_bytes = channels * _RAW_PCM_SAMPLE_WIDTH
    chunk_bytes = max(frame_bytes, frame_bytes * (sample_rate // 10))
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_pulse_pcm_worker,
        args=(lib, pulse_handle, pcm_data, chunk_bytes, stop_event),
        daemon=True,
    )
    thread.start()
    return _ThreadedAudioHandle(thread, stop_event)


def _start_pulse_pcm(filepath: str) -> _ThreadedAudioHandle | None:
    try:
        with open(filepath, "rb") as fh:
            pcm_data = fh.read()
    except OSError:
        return None
    return _start_pulse_data(pcm_data, _RAW_PCM_SAMPLE_RATE, _RAW_PCM_CHANNELS)


def _start_pulse_decoded_audio(filepath: str) -> _ThreadedAudioHandle | None:
    decoded = _decode_encoded_audio(filepath)
    if decoded is None:
        return None
    return _start_pulse_data(decoded.data, decoded.sample_rate, decoded.channels)


def _start_native_audio(filepath: str) -> Any | None:
    """Start audio through WaveBench-native backends when possible."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in _NATIVE_AUDIO_EXTENSIONS:
        return None

    if ext == ".pcm":
        pulse_handle = _start_pulse_pcm(filepath)
        if pulse_handle is not None:
            return pulse_handle
    else:
        pulse_handle = _start_pulse_decoded_audio(filepath)
        if pulse_handle is not None:
            return pulse_handle

    miniaudio = _load_miniaudio()
    if miniaudio is None:
        return None

    device = None
    try:
        if ext == ".pcm":
            with open(filepath, "rb") as fh:
                pcm_data = fh.read()
            if not pcm_data:
                return None
            stream = miniaudio.stream_raw_pcm_memory(
                pcm_data,
                nchannels=_RAW_PCM_CHANNELS,
                sample_width=_RAW_PCM_SAMPLE_WIDTH,
            )
            device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=_RAW_PCM_CHANNELS,
                sample_rate=_RAW_PCM_SAMPLE_RATE,
                app_name="WaveBench",
            )
        else:
            stream = miniaudio.stream_file(
                filepath,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=_ENCODED_PLAYBACK_CHANNELS,
                sample_rate=_ENCODED_PLAYBACK_RATE,
            )
            device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=_ENCODED_PLAYBACK_CHANNELS,
                sample_rate=_ENCODED_PLAYBACK_RATE,
                app_name="WaveBench",
            )
        device.start(stream)
        return _NativeAudioHandle(device, stream)
    except Exception:
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        return None


def _audio_player_command(filepath: str) -> list[str] | None:
    """Return a fallback platform command for non-TTS files."""
    if os.path.splitext(filepath)[1].lower() in _AUDIO_EXTENSIONS:
        return None

    if sys.platform == "darwin":
        afplay = shutil.which("afplay")
        if afplay:
            return [afplay, filepath]
        open_bin = shutil.which("open")
        return [open_bin, filepath] if open_bin else None

    if sys.platform == "win32":
        return None

    for candidate in ("mpv", "ffplay", "xdg-open"):
        path = shutil.which(candidate)
        if not path:
            continue
        if candidate == "mpv":
            return [path, "--really-quiet", "--no-terminal", filepath]
        if candidate == "ffplay":
            return [path, "-nodisp", "-autoexit", "-loglevel", "quiet", filepath]
        return [path, filepath]
    return None


def _managed_player(cmd: list[str]) -> bool:
    """True when the launched process is the actual audio player."""
    return os.path.basename(cmd[0]) in {"afplay", "mpv", "ffplay"}


def _stop_audio(proc: Any | None) -> None:
    """Stop a managed audio process if it is still running."""
    if proc is None:
        return

    stop = getattr(proc, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            pass
        return

    poll = getattr(proc, "poll", None)
    if not callable(poll) or poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_audio(filepath: str) -> tuple[bool, Any | None]:
    """Start playback and return (launched, managed_handle_or_none)."""
    native_handle = _start_native_audio(filepath)
    if native_handle is not None:
        return True, native_handle

    try:
        if os.path.splitext(filepath)[1].lower() in _AUDIO_EXTENSIONS:
            return False, None
        if sys.platform == "win32":
            os.startfile(filepath)  # type: ignore[attr-defined]
            return True, None
        cmd = _audio_player_command(filepath)
        if not cmd:
            return False, None
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, proc if _managed_player(cmd) else None
    except (OSError, FileNotFoundError):
        return False, None


def _tts_items(output_dir: str, results: dict[str, Any]) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for model_name, info in results.items():
        if info.get("status") != "success" or not info.get("file"):
            continue
        filename = str(info["file"])
        path = os.path.join(output_dir, filename)
        ext = os.path.splitext(path)[1].lower()
        if ext in _AUDIO_EXTENSIONS and os.path.exists(path):
            items.append((model_name, filename, path))
    return items


def browse_tts_outputs(output_dir: str, results: dict[str, Any]) -> None:
    """Open a tiny TUI for navigating and playing TTS outputs.

    ↑/↓ changes selection, Enter/Space plays the selected file, Esc/Tab exits.
    The browser is intentionally skipped outside an interactive terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return

    items = _tts_items(output_dir, results)
    if not items:
        return

    cursor = 0
    current_proc: Any | None = None
    status = f"{S.DIM}Enter/Space to play · Esc to close{S.RST}"

    def render() -> None:
        nonlocal status
        term = shutil.get_terminal_size((80, 24))
        w = _tw() - 4
        inner = w - 4
        max_rows = max(1, term.lines - 6)
        start = min(max(0, cursor - max_rows + 1), max(0, len(items) - max_rows))
        visible_items = items[start : start + max_rows]

        sys.stdout.write("\033[H")
        buf: list[str] = []
        buf.append(_box_top("TTS Outputs", w) + "\033[K\n")
        for offset, (model_name, filename, _path) in enumerate(visible_items):
            idx = start + offset
            marker = f"{_styles.ACCENT_HI}▸{S.RST}" if idx == cursor else " "
            name = f"{S.BOLD}{model_name}{S.RST}" if idx == cursor else model_name
            file_s = _truncate(filename, max(12, inner - len(model_name) - 8))
            row = f"{marker} {name}  {S.DIM}{file_s}{S.RST}"
            buf.append(_box_row(row, w) + "\033[K\n")
        if len(items) > max_rows:
            end = start + len(visible_items)
            buf.append(_box_row(f"{S.DIM}showing {start + 1}-{end} of {len(items)}{S.RST}", w) + "\033[K\n")
        else:
            buf.append(_box_row("", w) + "\033[K\n")
        buf.append(_box_row(status, w) + "\033[K\n")
        buf.append(
            _box_row(f"{S.DIM}↑↓ navigate · Enter/Space play · Esc/Tab close{S.RST}", w)
            + "\033[K\n"
        )
        buf.append(_box_bot(w) + "\033[K")
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        render()
        while True:
            key = _read_key_or_resize()
            if key in ("escape", "tab", "ctrl-c"):
                return
            if key in ("up", "left"):
                cursor = (cursor - 1) % len(items)
            elif key in ("down", "right"):
                cursor = (cursor + 1) % len(items)
            elif key in ("enter", "space"):
                _model_name, filename, path = items[cursor]
                _stop_audio(current_proc)
                launched, current_proc = _start_audio(path)
                if launched:
                    status = f"{S.HGRN}Playing{S.RST} {filename}"
                else:
                    status = f"{S.HRED}No native audio backend found{S.RST}"
            render()
    finally:
        _stop_audio(current_proc)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
