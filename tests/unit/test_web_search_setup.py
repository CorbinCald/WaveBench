from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from wavebench import web_search
from wavebench.storage import load_config
from wavebench.tui.menus import web_search_menu as menu


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)


def keys(monkeypatch, values):
    values = iter(values)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(menu, "hold_raw", nullcontext)
    monkeypatch.setattr(menu, "_read_key_or_resize", lambda: next(values))


def test_default_disabled_key_storage_and_environment_precedence(monkeypatch):
    assert load_config()["web_search"] == "off"
    assert web_search.configured_search({}) is None
    assert web_search.search_status({"web_search": "on"}) == "Needs setup"
    with pytest.raises(web_search.SearchError, match="needs a Brave"):
        web_search.configured_search({"web_search": "on"})
    web_search.save_brave_key("saved-test-key")
    assert web_search.load_brave_key() == "saved-test-key"
    if os.name == "posix":
        assert Path(web_search.SECRETS_FILE).stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "environment-test-key")
    assert web_search.load_brave_key() == "environment-test-key"
    assert web_search.configured_search({}) is None
    assert web_search.search_status({"web_search": "on"}) == "On (Brave)"
    assert not list(Path().glob(web_search.SECRETS_FILE + ".*"))


def test_setup_masks_validates_and_enables(monkeypatch, capsys):
    tested = []

    async def validate(key):
        tested.append(key)

    monkeypatch.setattr(menu, "validate_brave_key", validate)
    # A key starting with a shortcut character must still paste intact.
    keys(monkeypatch, [*"demo-private-key", "enter"])
    config = {"theme": "default", "web_search": "off"}
    assert menu.interactive_web_search(config) == {**config, "web_search": "on"}
    assert config["web_search"] == "off"
    assert tested == ["demo-private-key"]
    assert web_search.load_brave_key() == "demo-private-key"
    assert "demo-private-key" not in capsys.readouterr().out


def test_failed_validation_retry_cancel_preserves_previous_key(monkeypatch, capsys):
    web_search.save_brave_key("original-test-key")

    async def validate(key):
        raise web_search.SearchError("Brave rejected the API key; check its Search access.")

    monkeypatch.setattr(menu, "validate_brave_key", validate)
    keys(monkeypatch, ["k", *"bad-test-key", "enter", "escape"])
    assert menu.interactive_web_search({"web_search": "on"}) is None
    assert web_search.load_brave_key() == "original-test-key"
    output = capsys.readouterr().out
    assert "rejected" in output
    assert "bad-test-key" not in output


def test_disable_does_not_validate_and_retains_key(monkeypatch):
    web_search.save_brave_key("test-key")
    keys(monkeypatch, ["d"])
    assert menu.interactive_web_search({"web_search": "on"}) == {"web_search": "off"}
    assert web_search.load_brave_key() == "test-key"


def test_environment_key_is_reused_without_persisting(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-env-key")

    async def validate(key):
        assert key == "test-env-key"

    monkeypatch.setattr(menu, "validate_brave_key", validate)
    keys(monkeypatch, ["enter"])
    assert menu.interactive_web_search({}) == {"web_search": "on"}
    assert not Path(web_search.SECRETS_FILE).exists()


def test_setup_cli_works_without_openrouter(monkeypatch):
    from wavebench import __main__ as main

    monkeypatch.setattr(sys, "argv", ["wavebench", "--setup-web-search"])
    monkeypatch.setattr(main, "load_api_key", lambda: pytest.fail("should not require OpenRouter"))
    monkeypatch.setattr(main, "interactive_web_search", lambda cfg: {**cfg, "web_search": "off"})
    main.main()
    assert json.loads(Path(".benchmark_config.json").read_text())["web_search"] == "off"
