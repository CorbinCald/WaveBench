from __future__ import annotations

import pytest

from wavebench import storage
from wavebench.harness import handoff


@pytest.fixture
def desktop(monkeypatch):
    for key in ("SSH_CONNECTION", "SSH_CLIENT", "HERDR_ENV", "HERDR_SESSION", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(handoff.shutil, "which", lambda _: None)


@pytest.mark.parametrize("choice", ["automatic", "laptop", "host"])
def test_destination_persists_without_changing_auto_open(tmp_state_dir, choice):
    storage.save_config({"preview_destination": choice, "auto_open": "after_all"})
    loaded = storage.load_config()
    assert loaded["preview_destination"] == choice
    assert loaded["auto_open"] == "after_all"


async def test_automatic_local_desktop_and_explicit_destinations(desktop, monkeypatch):
    assert await handoff.destination("automatic") == "host"
    assert await handoff.destination("laptop") == "laptop"
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    assert await handoff.destination("automatic") == "laptop"
    assert await handoff.destination("host") == "host"


async def test_headless_host_waits_for_laptop(desktop, monkeypatch):
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setattr(handoff.sys, "platform", "linux")
    assert await handoff.destination("automatic") == "laptop"


@pytest.mark.parametrize("fresh", [True, False])
async def test_persistent_remote_herdr_session_never_falls_back_to_host(
    desktop, monkeypatch, fresh
):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SESSION", "project")
    monkeypatch.setattr(handoff.shutil, "which", lambda _: "/helper")

    async def status(helper, session):
        assert (helper, session) == ("/helper", "project")
        return {"fresh": fresh, "received": 123}

    monkeypatch.setattr(handoff, "client_status", status)
    assert await handoff.destination("automatic") == "laptop"


def test_acknowledgement_requires_current_preview_and_all_ports():
    preview = handoff.RemotePreview()
    preview.record = {"token": "current", "ports": [3000]}
    assert preview.message({"fresh": False}) == "Waiting for laptop connection"
    assert preview.message({"fresh": True, "reviews": [{"token": "old", "ports": [3000]}]}) == (
        "Waiting for laptop to open preview"
    )
    ack = {"token": "current", "ports": [3000], "errors": ["Local port 3000 is unavailable"]}
    assert "unavailable" in preview.message({"fresh": True, "reviews": [ack]})
    ack["errors"] = []
    assert preview.message({"fresh": True, "reviews": [ack]}) == "Opened on connected laptop"
    ack["ports"] = []
    assert (
        preview.message({"fresh": True, "reviews": [ack]}) == "Waiting for laptop to open preview"
    )
