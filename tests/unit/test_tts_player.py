"""Unit tests for the TTS output browser helpers."""

from __future__ import annotations

import subprocess

from wavebench.tui import tts_player


def test_tts_items_filters_successful_audio_outputs(tmp_path) -> None:
    (tmp_path / "a.mp3").write_bytes(b"audio")
    (tmp_path / "b.md").write_text("not audio")
    (tmp_path / "missing.mp3").write_bytes(b"audio")
    (tmp_path / "missing.mp3").unlink()

    results = {
        "ok_audio": {"status": "success", "file": "a.mp3"},
        "ok_text": {"status": "success", "file": "b.md"},
        "failed_audio": {"status": "failed", "file": "a.mp3"},
        "missing_audio": {"status": "success", "file": "missing.mp3"},
    }

    assert tts_player._tts_items(str(tmp_path), results) == [
        ("ok_audio", "a.mp3", str(tmp_path / "a.mp3"))
    ]


def test_browse_tts_outputs_navigates_with_arrows_and_plays_selected_file(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "first.mp3").write_bytes(b"audio-a")
    (tmp_path / "second.mp3").write_bytes(b"audio-b")
    results = {
        "first": {"status": "success", "file": "first.mp3"},
        "second": {"status": "success", "file": "second.mp3"},
    }

    class FakeTty:
        def __init__(self) -> None:
            self.output = ""

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> None:
            self.output += text

        def flush(self) -> None:
            pass

    fake_stdout = FakeTty()
    fake_stdin = FakeTty()
    keys = iter(["right", "enter", "escape"])
    started: list[str] = []
    stopped: list[object | None] = []
    fake_proc = object()

    monkeypatch.setattr(tts_player.sys, "stdin", fake_stdin)
    monkeypatch.setattr(tts_player.sys, "stdout", fake_stdout)
    monkeypatch.setattr(tts_player, "_read_key_or_resize", lambda: next(keys))
    monkeypatch.setattr(
        tts_player, "_start_audio", lambda path: (started.append(path) or True, fake_proc)
    )
    monkeypatch.setattr(tts_player, "_stop_audio", lambda proc: stopped.append(proc))

    tts_player.browse_tts_outputs(str(tmp_path), results)

    assert started == [str(tmp_path / "second.mp3")]
    assert stopped == [None, fake_proc]
    assert "\033[?1049h" in fake_stdout.output  # entered alt screen
    assert "\033[?1049l" in fake_stdout.output  # restored main screen


def test_browse_tts_outputs_scrolls_when_outputs_exceed_screen(
    tmp_path,
    monkeypatch,
) -> None:
    results = {}
    for idx in range(5):
        filename = f"voice-{idx}.mp3"
        (tmp_path / filename).write_bytes(b"audio")
        results[f"voice{idx}"] = {"status": "success", "file": filename}

    class FakeTty:
        def __init__(self) -> None:
            self.output = ""

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> None:
            self.output += text

        def flush(self) -> None:
            pass

    fake_stdout = FakeTty()
    fake_stdin = FakeTty()
    keys = iter(["down", "down", "down", "enter", "escape"])
    started: list[str] = []
    fake_proc = object()

    monkeypatch.setattr(tts_player.sys, "stdin", fake_stdin)
    monkeypatch.setattr(tts_player.sys, "stdout", fake_stdout)
    monkeypatch.setattr(tts_player.shutil, "get_terminal_size", lambda fallback: tts_player.os.terminal_size((80, 8)))
    monkeypatch.setattr(tts_player, "_read_key_or_resize", lambda: next(keys))
    monkeypatch.setattr(
        tts_player, "_start_audio", lambda path: (started.append(path) or True, fake_proc)
    )
    monkeypatch.setattr(tts_player, "_stop_audio", lambda proc: None)

    tts_player.browse_tts_outputs(str(tmp_path), results)

    assert started == [str(tmp_path / "voice-3.mp3")]
    assert "showing 3-4 of 5" in fake_stdout.output


def test_start_audio_uses_native_pulse_for_gemini_pcm(tmp_path, monkeypatch) -> None:
    pcm = tmp_path / "gemini.pcm"
    pcm.write_bytes(b"\x01\x02" * 8)
    events = []

    class FakePulse:
        @staticmethod
        def pa_simple_new(
            _server,
            name,
            direction,
            _dev,
            stream_name,
            sample_spec,
            _channel_map,
            _buffer_attr,
            _error,
        ):
            spec = sample_spec._obj
            events.append(("new", (name, direction, stream_name, spec.format, spec.rate, spec.channels)))
            return "pulse-handle"

        @staticmethod
        def pa_simple_write(_handle, data, size, _error) -> int:
            events.append(("write", (data[:size], size)))
            return 0

        @staticmethod
        def pa_simple_drain(_handle, _error) -> int:
            events.append(("drain", None))
            return 0

        @staticmethod
        def pa_simple_free(handle) -> None:
            events.append(("free", handle))

    class FakeThread:
        def __init__(self, target, args, daemon) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            events.append(("thread", self.daemon))
            self.target(*self.args)

        def join(self, timeout: float) -> None:
            events.append(("join", timeout))

    monkeypatch.setattr(tts_player, "_load_pulse_simple", lambda: FakePulse)
    monkeypatch.setattr(tts_player.threading, "Thread", FakeThread)
    monkeypatch.setattr(tts_player, "_load_miniaudio", lambda: None)

    launched, handle = tts_player._start_audio(str(pcm))

    assert launched is True
    assert handle is not None
    tts_player._stop_audio(handle)
    assert events == [
        ("new", (b"WaveBench", 1, b"TTS output", 3, 24_000, 1)),
        ("thread", True),
        ("write", (b"\x01\x02" * 8, 16)),
        ("drain", None),
        ("free", "pulse-handle"),
        ("join", 0.5),
    ]


def test_start_audio_uses_native_pulse_for_decoded_mp3(tmp_path, monkeypatch) -> None:
    mp3 = tmp_path / "openai.mp3"
    mp3.write_bytes(b"ID3audio")
    events = []

    class FakePulse:
        @staticmethod
        def pa_simple_new(
            _server,
            name,
            direction,
            _dev,
            stream_name,
            sample_spec,
            _channel_map,
            _buffer_attr,
            _error,
        ):
            spec = sample_spec._obj
            events.append(("new", (name, direction, stream_name, spec.format, spec.rate, spec.channels)))
            return "pulse-handle"

        @staticmethod
        def pa_simple_write(_handle, data, size, _error) -> int:
            events.append(("write", (data[:size], size)))
            return 0

        @staticmethod
        def pa_simple_drain(_handle, _error) -> int:
            events.append(("drain", None))
            return 0

        @staticmethod
        def pa_simple_free(handle) -> None:
            events.append(("free", handle))

    class FakeThread:
        def __init__(self, target, args, daemon) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            events.append(("thread", self.daemon))
            self.target(*self.args)

        def join(self, timeout: float) -> None:
            events.append(("join", timeout))

    monkeypatch.setattr(tts_player, "_load_pulse_simple", lambda: FakePulse)
    monkeypatch.setattr(tts_player.threading, "Thread", FakeThread)
    monkeypatch.setattr(tts_player, "_decode_mpg123", lambda path: None)
    monkeypatch.setattr(
        tts_player,
        "_decode_sndfile",
        lambda path: tts_player._DecodedPcm(b"\x01\x02" * 8, 24_000, 1),
    )
    monkeypatch.setattr(tts_player, "_load_miniaudio", lambda: None)

    launched, handle = tts_player._start_audio(str(mp3))

    assert launched is True
    assert handle is not None
    tts_player._stop_audio(handle)
    assert events == [
        ("new", (b"WaveBench", 1, b"TTS output", 3, 24_000, 1)),
        ("thread", True),
        ("write", (b"\x01\x02" * 8, 16)),
        ("drain", None),
        ("free", "pulse-handle"),
        ("join", 0.5),
    ]


def test_decode_encoded_audio_prefers_mpg123_for_mp3(monkeypatch) -> None:
    calls = []

    def fake_mpg123(path: str):
        calls.append(("mpg123", path))
        return tts_player._DecodedPcm(b"full", 22_050, 1)

    def fake_sndfile(path: str):
        calls.append(("sndfile", path))
        return tts_player._DecodedPcm(b"short", 22_050, 1)

    monkeypatch.setattr(tts_player, "_decode_mpg123", fake_mpg123)
    monkeypatch.setattr(tts_player, "_decode_sndfile", fake_sndfile)

    decoded = tts_player._decode_encoded_audio("voxtral.mp3")

    assert decoded == tts_player._DecodedPcm(b"full", 22_050, 1)
    assert calls == [("mpg123", "voxtral.mp3")]


def test_decode_encoded_audio_falls_back_to_sndfile_without_mpg123(monkeypatch) -> None:
    calls = []

    def fake_sndfile(path: str):
        calls.append(("sndfile", path))
        return tts_player._DecodedPcm(b"audio", 24_000, 1)

    monkeypatch.setattr(tts_player, "_decode_mpg123", lambda path: None)
    monkeypatch.setattr(tts_player, "_decode_sndfile", fake_sndfile)

    decoded = tts_player._decode_encoded_audio("openai.mp3")

    assert decoded == tts_player._DecodedPcm(b"audio", 24_000, 1)
    assert calls == [("sndfile", "openai.mp3")]


def test_start_audio_uses_native_miniaudio_fallback_for_encoded_audio(tmp_path, monkeypatch) -> None:
    mp3 = tmp_path / "openai.mp3"
    mp3.write_bytes(b"ID3audio")
    events = []

    class FakeFormat:
        SIGNED16 = "s16"

    class FakeDevice:
        def __init__(self, **kwargs) -> None:
            events.append(("device", kwargs))

        def start(self, stream) -> None:
            events.append(("start", stream))

        def close(self) -> None:
            events.append(("close", None))

    class FakeMiniaudio:
        SampleFormat = FakeFormat
        PlaybackDevice = FakeDevice

        @staticmethod
        def stream_file(path, **kwargs):
            events.append(("file", (path, kwargs)))
            return "encoded-stream"

    monkeypatch.setattr(tts_player, "_start_pulse_decoded_audio", lambda path: None)
    monkeypatch.setattr(tts_player, "_load_miniaudio", lambda: FakeMiniaudio)

    launched, handle = tts_player._start_audio(str(mp3))

    assert launched is True
    assert handle is not None
    tts_player._stop_audio(handle)
    assert events == [
        (
            "file",
            (
                str(mp3),
                {"output_format": "s16", "nchannels": 2, "sample_rate": 44_100},
            ),
        ),
        (
            "device",
            {
                "output_format": "s16",
                "nchannels": 2,
                "sample_rate": 44_100,
                "app_name": "WaveBench",
            },
        ),
        ("start", "encoded-stream"),
        ("close", None),
    ]


def test_play_audio_keeps_native_handle_until_next_play(monkeypatch) -> None:
    handles = []

    class FakeHandle:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    def fake_start_audio(filepath: str):
        handle = FakeHandle()
        handles.append((filepath, handle))
        return True, handle

    monkeypatch.setattr(tts_player, "_LAST_PLAYBACK_HANDLE", None)
    monkeypatch.setattr(tts_player, "_start_audio", fake_start_audio)

    assert tts_player.play_audio("first.mp3") is True
    assert tts_player.play_audio("second.mp3") is True

    assert handles[0][1].stopped is True
    assert handles[1][1].stopped is False


def test_audio_player_command_does_not_use_external_player_for_mp3(monkeypatch) -> None:
    monkeypatch.setattr(tts_player.sys, "platform", "linux")
    monkeypatch.setattr(tts_player.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert tts_player._audio_player_command("out.mp3") is None


def test_audio_player_command_does_not_xdg_open_raw_pcm(monkeypatch) -> None:
    monkeypatch.setattr(tts_player.sys, "platform", "linux")
    monkeypatch.setattr(
        tts_player.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None,
    )

    assert tts_player._audio_player_command("out.pcm") is None


def test_play_audio_returns_false_when_no_player(monkeypatch) -> None:
    monkeypatch.setattr(tts_player, "_LAST_PLAYBACK_HANDLE", None)
    monkeypatch.setattr(tts_player, "_load_miniaudio", lambda: None)
    monkeypatch.setattr(tts_player.sys, "platform", "linux")
    monkeypatch.setattr(tts_player.shutil, "which", lambda name: None)

    assert tts_player.play_audio("out.mp3") is False


def test_start_audio_does_not_launch_external_app_for_tts_audio(monkeypatch) -> None:
    called = []

    def fake_audio_player_command(path: str):
        called.append(path)
        return ["/usr/bin/xdg-open", path]

    def fake_popen(*args, **kwargs):
        raise AssertionError("external player should not be launched")

    monkeypatch.setattr(tts_player, "_start_native_audio", lambda path: None)
    monkeypatch.setattr(tts_player.sys, "platform", "linux")
    monkeypatch.setattr(tts_player, "_audio_player_command", fake_audio_player_command)
    monkeypatch.setattr(tts_player.subprocess, "Popen", fake_popen)

    launched, proc = tts_player._start_audio("out.mp3")

    assert launched is False
    assert proc is None
    assert called == []


def test_stop_audio_terminates_running_process() -> None:
    class FakeProcess:
        terminated = False
        killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> None:
            raise subprocess.TimeoutExpired("fake", timeout)

        def kill(self) -> None:
            self.killed = True

    proc = FakeProcess()
    tts_player._stop_audio(proc)  # type: ignore[arg-type]

    assert proc.terminated is True
    assert proc.killed is True
