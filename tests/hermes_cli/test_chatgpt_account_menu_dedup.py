"""Soma-only menu de-duplication, including the ATM distributable mirror."""
import importlib.util
from pathlib import Path
import pytest
from hermes_cli import config, free_model_filter


@pytest.fixture
def menu(monkeypatch, tmp_path):
    # A fork test must neither import a sibling checkout nor inspect the real home.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return free_model_filter


@pytest.mark.parametrize("profile", ["soma", "other", "default"])
@pytest.mark.parametrize("available", [(), ("chatgpt-cn",), ("chatgpt-com",), ("chatgpt-cn", "chatgpt-com")])
@pytest.mark.parametrize("id_key", ["slug", "provider_id"])
def test_menu_scope_and_plugin_fallback(menu, monkeypatch, profile, available, id_key):
    home = Path.home() / ".hermes"
    if profile != "default":
        home = home / "profiles" / profile
    monkeypatch.setattr(config, "get_hermes_home", lambda: home)
    rows = [{id_key: slug, "models": ["gpt-6.1-sol"]}
            for slug in ("openai-codex", *available)]
    original = [dict(row) for row in rows]
    ids = [row[id_key] for row in menu.filter_provider_rows(rows, snapshot={})]
    hide = profile == "soma" and len(available) == 2
    assert ("openai-codex" not in ids) == hide
    assert rows == original
    assert menu.free_model_ids("openai-codex", ["gpt-6.1-sol"], snapshot={}) == ["gpt-6.1-sol"]


def test_empty_account_row_keeps_native(menu, monkeypatch):
    monkeypatch.setattr(config, "get_hermes_home", lambda: Path.home() / ".hermes/profiles/soma")
    rows = [{"slug": slug, "models": models} for slug, models in
            [("openai-codex", ["model"]), ("chatgpt-cn", ["model"]), ("chatgpt-com", [])]]
    assert "openai-codex" in [r["slug"] for r in menu.filter_provider_rows(rows, snapshot={})]
