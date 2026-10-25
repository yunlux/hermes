"""One Codex refresh authority across real temporary profile stores/processes."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import time

import httpx
import pytest

import agent.credential_pool as cp
import hermes_cli.auth as auth
import hermes_cli.auth_codex as codex


def _jwt(account, serial, expiry):
    claims = {"sub": "subject-" + account, "exp": int(expiry), "serial": serial,
              "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return enc({"alg": "none"}) + "." + enc(claims) + ".sig"


def _row(cid="grant", source="manual:device_code", serial="old", expiry=None):
    return {"id": cid, "label": cid, "auth_type": "oauth", "priority": 0, "source": source,
            "access_token": _jwt(cid, serial, expiry or time.time() + 7200),
            "refresh_token": "synthetic-" + cid + "-" + serial}


def _store(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": {"openai-codex": rows}}))


def _rows(path):
    return json.loads(path.read_text())["credential_pool"]["openai-codex"]


@pytest.fixture
def authority(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    path = tmp_path / "root" / "auth.json"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: path)
    monkeypatch.setattr(cp, "_global_auth_file_path", lambda: path)
    return path


def _worker(home, authority, gate, posts, results, force):
    import hermes_constants
    hermes_constants.set_hermes_home_override(home)
    auth._global_auth_store_cache = None
    pool = cp.load_pool("openai-codex")
    entry = next(e for e in pool.entries() if e.id == "grant")

    def post(request):
        assert request.method == "POST"
        with posts.get_lock():
            posts.value += 1
            number = posts.value
        if number != 1:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh_token_reused"})
        # A later grant can have a SHORTER lifetime than the previous one.
        return httpx.Response(200, json={"access_token": _jwt("grant", "new", time.time() + 3600),
                                        "refresh_token": "synthetic-grant-new"})

    @contextmanager
    def client(**kwargs):
        with httpx.Client(transport=httpx.MockTransport(post)) as http_client:
            yield http_client

    codex._codex_http_client = client
    gate.wait(timeout=10)
    refreshed = pool._refresh_entry(entry, force=force)
    results.put((str(home), refreshed.refresh_token if refreshed else None,
                 str(getattr(pool, "_auth_store_path", ""))))


@pytest.mark.parametrize("source", ["manual:device_code", "device_code"])
@pytest.mark.parametrize("force", [False, True])
def test_two_profile_processes_refresh_one_authoritative_grant_once(authority, source, force):
    expiry = time.time() + (7200 if force else 30)
    _store(authority, [_row(source=source, expiry=expiry)])
    homes = [authority.parent / "profiles" / name for name in ("a", "b")]
    for home in homes:
        _store(home / "auth.json", [])
    ctx = multiprocessing.get_context("fork")
    gate, posts, results = ctx.Barrier(2), ctx.Value("i", 0), ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(home, authority, gate, posts, results, force)) for home in homes]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        assert all(worker.exitcode == 0 for worker in workers)
        observed = [results.get(timeout=2) for _ in workers]
        assert posts.value == 1
        assert {result[1] for result in observed} == {"synthetic-grant-new"}
        assert {result[2] for result in observed} == {str(authority)}
        assert _rows(authority)[0]["refresh_token"] == "synthetic-grant-new"
        assert all(_rows(home / "auth.json") == [] for home in homes)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)


@pytest.mark.parametrize("verdict", ["dead", "deleted", "different-principal"])
def test_authority_persists_only_changed_rows_and_never_revives_removed_grants(authority, monkeypatch, verdict):
    initial = [_row(), _row("sibling"), _row("removed")]
    _store(authority, initial)
    pool = cp.CredentialPool("openai-codex", [cp.PooledCredential.from_dict("openai-codex", r) for r in initial],
                             auth_store_path=authority)
    stale = pool.entries()[0]
    sibling = _row("sibling", serial="peer")
    _store(authority, [initial[0], sibling])
    pool._mark_exhausted(stale, 429)
    disk = _rows(authority)
    assert [r["id"] for r in disk] == ["grant", "sibling"]
    assert disk[1] == sibling

    if verdict == "dead":
        target = {**disk[0], "last_status": cp.STATUS_DEAD, "last_status_at": time.time() + 5,
                  "last_error_reason": "refresh_token_reused"}
    elif verdict == "different-principal":
        target = {**_row("someone-else"), "id": "grant"}
    else:
        target = None
    _store(authority, ([target] if target else []) + [sibling])
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *a, **k: pytest.fail("terminal/missing authority POSTed"))
    assert pool.try_refresh_matching(credential_id="grant", api_key_hint=stale.access_token) is None
    if verdict == "dead":
        pool._mark_exhausted(stale, 401, {"reason": "token_expired"})
        assert next(e for e in pool.entries() if e.id == "grant").last_status == cp.STATUS_DEAD
    assert _rows(authority) == ([target] if target else []) + [sibling]


def _mock_endpoint(monkeypatch, callback):
    @contextmanager
    def client(**kwargs):
        with httpx.Client(transport=httpx.MockTransport(callback)) as http_client:
            yield http_client
    monkeypatch.setattr(codex, "_codex_http_client", client)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("linked_singleton", [False, True])
def test_authority_refresh_updates_only_linked_singleton_and_keeps_terminal_dead(
    authority, monkeypatch, caplog, terminal, linked_singleton,
):
    original = _row(source="device_code")
    _store(authority, [original])
    singleton = {key: original[key] for key in ("access_token", "refresh_token")}
    if not linked_singleton:
        singleton = {key: _row("other")[key] for key in singleton}
    store = json.loads(authority.read_text())
    store["providers"] = {"openai-codex": {"tokens": singleton.copy(), "auth_mode": "chatgpt"},
                          "other-provider": {"sentinel": "unchanged"}}
    authority.write_text(json.dumps(store))
    pool = cp.CredentialPool("openai-codex", [cp.PooledCredential.from_dict("openai-codex", original)],
                             auth_store_path=authority)
    posts = []

    def post(request):
        posts.append(request)
        if terminal:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token revoked"})
        return httpx.Response(200, json={"access_token": _jwt("grant", "new", time.time() + 3600),
                                        "refresh_token": "synthetic-grant-new"})

    _mock_endpoint(monkeypatch, post)
    result = pool.try_refresh_matching(credential_id="grant", api_key_hint=original["access_token"])
    on_disk = json.loads(authority.read_text())
    tokens = on_disk["providers"]["openai-codex"]["tokens"]
    assert on_disk["providers"]["other-provider"] == {"sentinel": "unchanged"}
    assert len(posts) == 1
    if terminal:
        assert result is None and _rows(authority)[0]["last_status"] == cp.STATUS_DEAD
        pool._mark_exhausted(pool.entries()[0], 401, {"reason": "token_expired"})
        assert pool.entries()[0].last_status == cp.STATUS_DEAD
        assert pool.try_refresh_matching(credential_id="grant") is None
        assert len(posts) == 1
        assert "hermes auth add openai-codex" in caplog.text
        assert tokens == ({} if linked_singleton else singleton)
    else:
        assert result.refresh_token == "synthetic-grant-new"
        assert tokens == ({"access_token": result.access_token, "refresh_token": result.refresh_token}
                          if linked_singleton else singleton)


def test_authority_refresh_and_concurrent_rotation_have_consistent_lock_order(authority, monkeypatch):
    import threading
    original = _row()
    _store(authority, [original])
    pool = cp.CredentialPool("openai-codex", [cp.PooledCredential.from_dict("openai-codex", original)],
                             auth_store_path=authority)
    stale = pool.entries()[0]
    entered, release, rotating = threading.Event(), threading.Event(), threading.Event()
    errors, outcomes = [], []

    def post(request):
        entered.set()
        assert release.wait(timeout=3)
        return httpx.Response(200, json={"access_token": _jwt("grant", "new", time.time() + 3600),
                                        "refresh_token": "synthetic-grant-new"})

    _mock_endpoint(monkeypatch, post)

    def refresh():
        try:
            outcomes.append(pool._refresh_entry(stale, force=True))
        except BaseException as exc:
            errors.append(exc)

    def rotate():
        try:
            rotating.set()
            pool.mark_exhausted_and_rotate(status_code=401, api_key_hint=stale.access_token,
                                           credential_id=stale.id)
        except BaseException as exc:
            errors.append(exc)

    refresher = threading.Thread(target=refresh, daemon=True)
    rotation = threading.Thread(target=rotate, daemon=True)
    refresher.start()
    assert entered.wait(timeout=3)
    rotation.start()
    assert rotating.wait(timeout=3)
    # Let the competing mutation reach its blocking lock while the token
    # endpoint is held. The transaction must finish once the endpoint returns.
    rotation.join(timeout=0.1)
    release.set()
    refresher.join(timeout=4)
    rotation.join(timeout=4)
    assert not refresher.is_alive() and not rotation.is_alive(), "pool/auth lock inversion deadlocked"
    assert not errors
    assert outcomes[0].refresh_token == "synthetic-grant-new"
    assert _rows(authority)[0]["refresh_token"] == "synthetic-grant-new"


def test_borrowed_pool_claiming_local_grant_follows_new_store_ownership(authority, monkeypatch):
    _store(authority, [_row()])
    original_root = authority.read_bytes()
    home = authority.parent / "profiles" / "owner"
    _store(home / "auth.json", [])
    monkeypatch.setenv("HERMES_HOME", str(home))
    pool = cp.load_pool("openai-codex")
    local_row = _row("local")
    local = pool.add_entry(cp.PooledCredential.from_dict("openai-codex", local_row))
    assert pool._auth_store_path == (home / "auth.json").resolve()
    assert [e.id for e in pool.entries()] == ["local"]
    assert [r["id"] for r in _rows(home / "auth.json")] == ["local"]
    _mock_endpoint(monkeypatch, lambda request: httpx.Response(
        200, json={"access_token": _jwt("local", "new", time.time() + 3600),
                   "refresh_token": "synthetic-local-new"}))
    assert pool._refresh_entry(local, force=True).refresh_token == "synthetic-local-new"
    assert _rows(home / "auth.json")[0]["refresh_token"] == "synthetic-local-new"
    assert authority.read_bytes() == original_root


def test_authority_admin_remove_preserves_sibling_rotated_by_peer(authority):
    initial = [_row(), _row("sibling")]
    _store(authority, initial)
    pool = cp.CredentialPool("openai-codex", [cp.PooledCredential.from_dict("openai-codex", r) for r in initial],
                             auth_store_path=authority)
    peer = _row("sibling", serial="peer")
    _store(authority, [initial[0], peer])
    assert pool.remove_index(1).id == "grant"
    assert _rows(authority) == [peer]


@pytest.mark.parametrize("expired,reset_elapsed,quota_restored", [
    (True, False, False), (False, False, False),
    (False, False, True), (True, True, False),
])
def test_authority_quota_probe_never_refreshes_outside_transaction(
        authority, monkeypatch, expired, reset_elapsed, quota_restored):
    now = time.time()
    original = _row(expiry=now + (-30 if expired else 3600))
    original.update(last_status=cp.STATUS_EXHAUSTED, last_status_at=now,
                    last_error_code=429, last_error_reason="usage_limit_reached",
                    last_error_reset_at=now + (-1 if reset_elapsed else 3600))
    _store(authority, [original])
    before = authority.read_bytes()
    pool = cp.CredentialPool("openai-codex", [cp.PooledCredential.from_dict("openai-codex", original)],
                             auth_store_path=authority)
    probes, posts = [], []
    from contextlib import contextmanager
    original_lock = auth._auth_store_lock
    original_refresh = auth._refresh_expired_codex_probe_token
    lock_depth = [0]

    @contextmanager
    def checked_lock(*args, **kwargs):
        assert kwargs.get("target_path") == authority
        with original_lock(*args, **kwargs):
            lock_depth[0] += 1
            try:
                yield
            finally:
                lock_depth[0] -= 1

    def checked_refresh(*args):
        assert lock_depth[0] > 0, "quota refresh escaped its owner transaction"
        return original_refresh(*args)

    monkeypatch.setattr(auth, "_auth_store_lock", checked_lock)
    monkeypatch.setattr(auth, "_refresh_expired_codex_probe_token", checked_refresh)
    monkeypatch.setattr(auth, "_probe_codex_quota_restored",
                        lambda *args, **kwargs: probes.append(args[0]) or quota_restored)

    def post(request):
        posts.append(request.method)
        return httpx.Response(200, json={"access_token": _jwt("grant", "new", now + 3600),
                                        "refresh_token": "synthetic-grant-new"})

    _mock_endpoint(monkeypatch, post)
    selected = pool.select()
    assert len(probes) == int(not reset_elapsed)
    assert posts == (["POST"] if expired else [])
    assert bool(selected) == (quota_restored or reset_elapsed)
    if selected:
        assert _rows(authority)[0]["last_status"] == cp.STATUS_OK
        if reset_elapsed:
            assert selected.refresh_token == "synthetic-grant-new"
    elif expired:
        row = _rows(authority)[0]
        assert row["refresh_token"] == "synthetic-grant-new"
        assert row["last_status"] == cp.STATUS_EXHAUSTED
    else:
        assert authority.read_bytes() == before
