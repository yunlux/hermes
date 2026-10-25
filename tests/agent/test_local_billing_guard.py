"""Current request-rule and usage contracts (legacy billing_guard was retired).

No live clients, credentials, or billable requests are used. Disabled rules are
intentionally permissive; a menu filter is NOT a billing enforcement boundary.
"""
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest

from agent import usage_ledger as ledger
from hermes_cli import request_guard as guard


def test_explicit_rule_blocks_before_send_and_does_not_mutate_request(tmp_path, monkeypatch):
    config = guard.validate({"version": 1, "enabled": True, "default_action": "allow",
        "rules": [{"id": "cron-block", "action": "block", "source": "cron",
                   "provider": "test-provider", "model": "paid-*"}]})
    monkeypatch.setattr(guard, "load", lambda path=None: config)
    client = NS(base_url="https://example.invalid/v1")
    kwargs = {"model": "paid-model", "messages": [{"role": "user", "content": "fixture"}]}
    before = json.dumps(kwargs, sort_keys=True)
    sent = []
    with pytest.raises(guard.RequestBlocked):
        ledger.recorded_call(client, kwargs, lambda request: sent.append(request),
            provider="test-provider", runtime={"billing_home": str(tmp_path), "billing_source": "cron"})
    assert sent == []
    assert json.dumps(kwargs, sort_keys=True) == before
    assert not (tmp_path / "token_requests.db").exists()


def test_disabled_missing_and_invalid_rules_preserve_existing_allow_semantics(tmp_path):
    for path in [tmp_path / "missing.json", tmp_path / "invalid.json"]:
        if path.name == "invalid.json":
            path.write_text("{broken")
        assert guard.check_request("test-provider", "test-model", "interactive", path=path)["allowed"]
    assert guard.check(dict(guard.DEFAULT), "test-provider", "test-model", "cron")["allowed"]


def test_usage_observations_are_profile_local_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "load", lambda path=None: dict(guard.DEFAULT))
    client = NS(base_url="https://example.invalid/v1")
    response = NS(id="fixture-response", usage=NS(prompt_tokens=20, completion_tokens=3,
        prompt_tokens_details=NS(cached_tokens=15)))
    for name in ["profile-a", "profile-b"]:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        info = ledger.observe_request(client, {"model": "fixture-model"}, provider="openai",
            runtime={"billing_home": str(home), "billing_source": "interactive"})
        assert info["profile"] == name
        ledger.record_response(info, response)
        ledger.record_response(info, response)
        with sqlite3.connect(home / "token_requests.db") as db:
            assert db.execute("select input_tokens,cache_read_tokens,output_tokens from requests").fetchall() == [(5, 15, 3)]
