"""The speed toggle can be saved, reloaded, disabled, or cancelled."""

import sys

import pytest

from wavebench import storage
from wavebench.tui.menus import config_menu


@pytest.mark.parametrize("initial,expected", [("off", "on"), ("on", "off")])
@pytest.mark.parametrize("save", [False, True])
def test_fast_providers_setting_persists_only_on_save(
    tmp_state_dir, monkeypatch, capsys, initial, expected, save
):
    storage.save_config({"fast_providers": initial, "theme": "default"})
    keys = iter(["left", "down", "space", "enter" if save else "escape"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(config_menu, "_read_key_or_resize", lambda *_: next(keys))
    current = storage.load_config()
    models, updated = config_menu.interactive_config_menu([], {"Fixture": "test/model"}, current)
    if save:
        assert models["Fixture"] == "test/model"
        storage.save_config(updated)
    else:
        assert (models, updated) == (None, None)
    assert current["fast_providers"] == initial
    assert storage.load_config()["fast_providers"] == (expected if save else initial)
    assert "Fast providers (tokens/sec)" in capsys.readouterr().out
