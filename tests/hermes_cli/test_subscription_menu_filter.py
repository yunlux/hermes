"""Native ChatGPT and user-requested Go routes survive the free menu overlay."""
import pytest
from hermes_cli import free_model_filter as menu, models


def test_authenticated_picker_row_retains_subscription_and_availability():
    offered = ["subscription-model"]
    row = {"slug": "openai-codex", "models": offered,
           "unavailable_models": offered, "source": "hermes"}
    go = {"slug": "opencode-go", "models": offered, "unavailable_models": offered}
    rows = menu.filter_provider_rows([
        row, {"slug": "openai", "models": offered},
        go,
        {**row, "base_url": "https://api.openai.com/v1"},
    ], snapshot={"providers": {}})
    assert len(rows) == 2
    assert rows[0]["models"] == offered
    assert rows[0]["unavailable_models"] == offered
    assert rows[1]["unavailable_models"] == offered
    assert menu.free_model_ids("openai-codex", offered,
        base_url="https://chatgpt.com/backend-api/codex", snapshot={}) == offered


@pytest.mark.parametrize("provider", ["openai-codex", "opencode-go"])
def test_subscription_menu_delegates_to_native_account_catalog_and_cache(monkeypatch, provider):
    offered = ["account-offered-model"]
    calls = []
    def native(provider, **kwargs):
        calls.append((provider, kwargs))
        return offered
    monkeypatch.setattr(models, "_unfiltered_provider_model_ids", native)
    monkeypatch.setattr(models, "_unfiltered_cached_provider_model_ids", native)
    monkeypatch.setattr(menu, "read_snapshot", lambda: {})
    assert models.provider_model_ids(provider, force_refresh=True) is offered
    assert models.cached_provider_model_ids(provider, ttl_seconds=17,
        non_blocking=True) is offered
    assert calls == [(provider, {"force_refresh": True}),
                     (provider, {"force_refresh": False,
                      "ttl_seconds": 17, "non_blocking": True})]
    assert models.provider_model_ids("openai") == []


def test_free_menu_does_not_hide_models_without_tools(monkeypatch):
    data = {"providers": {"openrouter": ["free-text"]},
            "model_metadata": {"openrouter": {"free-text": {"tools": False}}}}
    monkeypatch.setattr(menu, "read_snapshot", lambda: data)
    assert models.provider_model_ids("openrouter") == ["free-text"]
    assert menu.filter_provider_rows([{"slug": "openrouter", "models": ["free-text", "paid"]}],
                                     snapshot=data)[0]["models"] == ["free-text"]
