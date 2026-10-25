"""Offline grant-identity contract; every credential below is synthetic.

Run with HOME and HERMES_HOME already redirected before importing Hermes.
No production auth/config/log fixture is used. Network attempts fail the case.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import socket
import time

import httpx
import pytest

import agent.credential_pool as cp
import hermes_cli.auth as auth
import hermes_constants


PROVIDER = "openai-codex"


def _jwt(serial: str, *, account: str = "synthetic-owner") -> str:
    claims = {
        "sub": "synthetic-subject-" + account,
        "exp": int(time.time() + 7200),
        "serial": serial,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return encode({"alg": "none"}) + "." + encode(claims) + ".synthetic-signature"


def _row(cid: str, serial: str, *, source: str = "device_code", status: str = "ok") -> dict:
    return {
        "id": cid,
        "label": cid,
        "auth_type": "oauth",
        "priority": 0,
        "source": source,
        "access_token": _jwt(serial),
        "refresh_token": "synthetic-refresh-" + serial,
        "last_status": status,
        "last_status_at": time.time() - 30 if status != "ok" else None,
    }


def _pair(row) -> tuple:
    if isinstance(row, dict):
        return row.get("access_token") or "", row.get("refresh_token") or ""
    return row.access_token or "", row.refresh_token or ""


def _write(path: Path, rows: list, *, singleton: dict | None = None) -> None:
    providers = {}
    if singleton is not None:
        providers[PROVIDER] = {
            "tokens": {
                "access_token": singleton.get("access_token", ""),
                "refresh_token": singleton.get("refresh_token", ""),
            },
            "last_refresh": time.time(),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "providers": providers,
        "credential_pool": {PROVIDER: rows},
    }))
    auth._global_auth_store_cache = None


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _disk_rows(path: Path) -> list:
    return _read(path)["credential_pool"][PROVIDER]


def _disk_tokens(path: Path) -> dict:
    return _read(path)["providers"][PROVIDER]["tokens"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    # Distinct from Path.home()/.hermes so Hermes' pytest seat belt also
    # recognizes this explicit temporary root as an isolated store.
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "synthetic-profile"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_REAL_HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    monkeypatch.setattr(cp, "_load_config_safe", lambda: {})
    monkeypatch.setattr(cp, "get_pool_strategy", lambda *_args, **_kwargs: cp.STRATEGY_FILL_FIRST)
    monkeypatch.setattr(cp, "_seed_from_env", lambda *_args, **_kwargs: (False, set()))
    monkeypatch.setattr(auth, "heal_forked_single_use_oauth_grants", lambda *_args, **_kwargs: None)

    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("offline regression attempted network or refresh")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", forbidden)
    monkeypatch.setattr(cp.CredentialPool, "_post_tokens_refresh", forbidden)
    monkeypatch.setattr(cp.CredentialPool, "_probe_codex_quota_restored", forbidden, raising=False)

    def activate(home: Path):
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        hermes_constants.set_hermes_home_override(str(home))
        global_path = None if home == root else root / "auth.json"
        monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_path)
        monkeypatch.setattr(cp, "_global_auth_file_path", lambda: global_path)
        auth._global_auth_store_cache = None

    activate(root)
    yield root / "auth.json", profile / "auth.json", activate
    hermes_constants.set_hermes_home_override(None)
    auth._global_auth_store_cache = None
    assert not attempts, "all test operations must remain offline"


def _pool(path: Path, rows: list):
    return cp.CredentialPool(PROVIDER, [cp.PooledCredential.from_dict(PROVIDER, r) for r in rows],
                             auth_store_path=path)


def test_root_load_preserves_two_same_source_same_principal_grants(isolated):
    authority, _profile, _activate = isolated
    first, second = _row("first", "first"), _row("second", "second")
    _write(authority, [first, second], singleton=second)
    pool = cp.load_pool(PROVIDER)
    assert {r.id for r in pool.entries()} == {"first", "second"}
    assert {r["id"]: _pair(r) for r in _disk_rows(authority)} == {
        "first": _pair(first), "second": _pair(second),
    }
    assert pool._auth_store_path == authority.resolve()


@pytest.mark.parametrize("source", ["device_code", "manual:device_code"])
@pytest.mark.parametrize("status", ["exhausted", "dead"])
def test_borrowed_cold_sync_uses_exact_row_instead_of_singleton(isolated, source, status):
    authority, profile, activate = isolated
    stale = _row("first", "first-old", source=source, status=status)
    newer = _row("first", "first-new", source=source)
    unrelated = _row("second", "different-grant")
    _write(authority, [stale], singleton=unrelated)
    _write(profile, [], singleton=unrelated)
    activate(profile.parent)
    pool = _pool(authority, [stale])
    _write(authority, [newer], singleton=unrelated)
    observed = pool._resync_stale_entry(pool.entries()[0])
    assert observed is not None and observed.id == "first"
    assert _pair(observed) == _pair(newer)
    assert observed.last_status == "ok"
    assert _pair(_disk_rows(authority)[0]) == _pair(newer)
    assert _disk_rows(profile) == []


@pytest.mark.parametrize("status", ["exhausted", "dead"])
def test_borrowed_deleted_row_is_forgotten_and_not_revived(isolated, status):
    authority, profile, activate = isolated
    stale = _row("removed", "removed-old", status=status)
    unrelated = _row("second", "different-grant")
    _write(authority, [stale], singleton=unrelated)
    _write(profile, [], singleton=unrelated)
    activate(profile.parent)
    pool = _pool(authority, [stale])
    pool._current_id = "removed"
    _write(authority, [], singleton=unrelated)
    observed = pool._resync_stale_entry(pool.entries()[0])
    assert observed is None
    assert pool.entries() == []
    assert pool.current() is None
    pool._persist()
    assert _disk_rows(authority) == []
    assert _disk_rows(profile) == []


def test_root_admin_add_explicitly_inserts_new_grant(isolated):
    authority, _profile, _activate = isolated
    first = _row("first", "first", source="manual:device_code")
    second = _row("second", "second", source="manual:device_code")
    _write(authority, [first])
    pool = _pool(authority, [first])
    pool.add_entry(cp.PooledCredential.from_dict(PROVIDER, second))
    assert {r["id"] for r in _disk_rows(authority)} == {"first", "second"}
    assert {r.id for r in pool.entries()} == {"first", "second"}
    assert pool._auth_store_path == authority.resolve()


@pytest.mark.parametrize("collision", ["id", "pair"])
def test_rejected_admin_add_leaves_runtime_and_disk_unchanged(isolated, collision):
    authority, _profile, _activate = isolated
    first = _row("first", "first", source="manual:device_code")
    incoming = (_row("first", "different-grant", source="manual:device_code")
                if collision == "id" else dict(first, id="second", label="second"))
    _write(authority, [first])
    pool = _pool(authority, [first])
    with pytest.raises(ValueError):
        pool.add_entry(cp.PooledCredential.from_dict(PROVIDER, incoming))
    assert [r.id for r in pool.entries()] == ["first"]
    assert _pair(pool.entries()[0]) == _pair(first)
    assert len(_disk_rows(authority)) == 1
    assert _pair(_disk_rows(authority)[0]) == _pair(first)


@pytest.mark.parametrize("loaded", [False, True])
def test_runtime_without_baseline_cannot_insert_grant(isolated, loaded):
    authority, _profile, _activate = isolated
    first = _row("first", "first", source="manual:device_code")
    orphan = _row("orphan", "orphan", source="manual:device_code")
    _write(authority, [first])
    pool = cp.load_pool(PROVIDER) if loaded else _pool(authority, [first])
    pool._entries.append(cp.PooledCredential.from_dict(PROVIDER, orphan))
    pool._persist()
    assert [r["id"] for r in _disk_rows(authority)] == ["first"]
    assert [r.id for r in pool.entries()] == ["first"]


def test_singleton_new_pair_is_added_once_without_overwriting_other_grants(isolated):
    authority, _profile, _activate = isolated
    first, second, fresh = _row("first", "first"), _row("second", "second"), _row("login", "fresh-login")
    _write(authority, [first, second], singleton=fresh)
    cp.load_pool(PROVIDER)
    once = _disk_rows(authority)
    cp.load_pool(PROVIDER)
    twice = _disk_rows(authority)
    assert len(once) == len(twice) == 3
    assert {r["id"] for r in once} == {r["id"] for r in twice}
    by_id = {r["id"]: r for r in twice}
    assert _pair(by_id["first"]) == _pair(first)
    assert _pair(by_id["second"]) == _pair(second)
    assert sum(_pair(r) == _pair(fresh) for r in twice) == 1


def test_singleton_matching_manual_pair_does_not_create_another_grant(isolated):
    authority, _profile, _activate = isolated
    manual = _row("manual", "manual", source="manual:device_code")
    _write(authority, [manual], singleton=manual)
    cp.load_pool(PROVIDER)
    cp.load_pool(PROVIDER)
    rows = _disk_rows(authority)
    assert len(rows) == 1 and rows[0]["id"] == "manual"
    assert rows[0]["source"] == "manual:device_code"
    assert _pair(rows[0]) == _pair(manual)


@pytest.mark.parametrize("mismatch", ["none", "access", "refresh", "both"])
@pytest.mark.parametrize("operation", ["rotate", "dead"])
def test_linked_singleton_writeback_requires_both_old_tokens(isolated, mismatch, operation):
    authority, _profile, _activate = isolated
    old = _row("first", "old")
    singleton = dict(old)
    if mismatch in {"access", "both"}:
        singleton["access_token"] = _jwt("unrelated-access")
    if mismatch in {"refresh", "both"}:
        singleton["refresh_token"] = "synthetic-unrelated-refresh"
    _write(authority, [old], singleton=singleton)
    pool = _pool(authority, [old])
    entry = pool.entries()[0]
    if operation == "rotate":
        new = _row("first", "new")
        pool._adopt(entry, access_token=new["access_token"], refresh_token=new["refresh_token"])
        expected = _pair(new) if mismatch == "none" else _pair(singleton)
        assert _pair(_disk_rows(authority)[0]) == _pair(new)
    else:
        pool._adopt(entry, last_status="dead", last_status_at=time.time())
        expected = ("", "") if mismatch == "none" else _pair(singleton)
        assert _disk_rows(authority)[0]["last_status"] == "dead"
    assert _pair(_disk_tokens(authority)) == expected


@pytest.mark.parametrize("authority_has_reset", [False, True])
def test_cold_reset_marker_is_read_from_explicit_authority(isolated, authority_has_reset):
    authority, profile, activate = isolated
    stale = _row("first", "old", status="exhausted")
    now = time.time()
    stale["last_status_at"] = now - 30
    authoritative = dict(stale)
    if authority_has_reset:
        authoritative["status_cleared_at"] = now - 10
    ambient = dict(stale, status_cleared_at=now + 100)
    _write(authority, [authoritative])
    _write(profile, [ambient])
    activate(profile.parent)
    pool = _pool(authority, [stale])
    assert pool._reset_cleared_after(pool.entries()[0]) == (now - 10 if authority_has_reset else None)


def _borrowed_pool_for_claim(isolated):
    authority, profile, activate = isolated
    borrowed = _row("borrowed", "borrowed", source="manual:device_code")
    _write(authority, [borrowed])
    _write(profile, [])
    activate(profile.parent)
    pool = _pool(authority, [borrowed])
    pool._borrowed_root_ids = {borrowed["id"]}
    pool._current_id = borrowed["id"]
    return authority, profile, pool, borrowed


def test_profile_claim_failure_before_write_preserves_borrowed_runtime(isolated, monkeypatch):
    authority, profile, pool, borrowed = _borrowed_pool_for_claim(isolated)
    incoming = _row("incoming", "incoming", source="manual:device_code")
    authority_bytes, profile_bytes = authority.read_bytes(), profile.read_bytes()
    before_rows = dict(pool._auth_store_rows)

    def fail_before_save(_store, target_path=None):
        assert target_path == profile.resolve()
        raise OSError("synthetic failure before replacement")

    monkeypatch.setattr(auth, "_save_auth_store", fail_before_save)
    with pytest.raises(OSError, match="synthetic failure before replacement"):
        pool.add_entry(cp.PooledCredential.from_dict(PROVIDER, incoming))
    assert authority.read_bytes() == authority_bytes
    assert profile.read_bytes() == profile_bytes
    assert [entry.id for entry in pool.entries()] == [borrowed["id"]]
    assert _pair(pool.entries()[0]) == _pair(borrowed)
    assert pool._auth_store_path == authority.resolve()
    assert pool._auth_store_rows == before_rows
    assert pool._borrowed_root_ids == {borrowed["id"]}
    assert pool.current().id == borrowed["id"]


def test_profile_claim_preserves_concurrent_local_rows_and_transfers_ownership(isolated):
    authority, profile, pool, _borrowed = _borrowed_pool_for_claim(isolated)
    peer = _row("peer", "peer", source="manual:device_code")
    incoming = _row("incoming", "incoming", source="manual:device_code")
    authority_bytes = authority.read_bytes()
    # A peer claims the profile after this runtime borrowed the root snapshot.
    _write(profile, [peer])
    added = pool.add_entry(cp.PooledCredential.from_dict(PROVIDER, incoming))
    expected = {"peer": _pair(peer), "incoming": _pair(incoming)}
    assert {row["id"]: _pair(row) for row in _disk_rows(profile)} == expected
    assert {entry.id: _pair(entry) for entry in pool.entries()} == expected
    assert set(pool._auth_store_rows) == set(expected)
    assert pool._auth_store_path == profile.resolve()
    assert pool._borrowed_root_ids == set()
    assert pool.current() is None
    # Subsequent writes follow the claimed profile, preserving the root grant.
    pool._adopt(added, last_status="exhausted", last_status_at=time.time())
    assert next(row for row in _disk_rows(profile) if row["id"] == "incoming")["last_status"] == "exhausted"
    assert authority.read_bytes() == authority_bytes


def test_profile_claim_failure_after_write_reconciles_committed_ownership(isolated, monkeypatch):
    authority, profile, pool, _borrowed = _borrowed_pool_for_claim(isolated)
    incoming = _row("incoming", "incoming", source="manual:device_code")
    authority_bytes = authority.read_bytes()
    save = auth._save_auth_store

    def save_then_fail(store, target_path=None):
        assert target_path == profile.resolve()
        save(store, target_path=target_path)
        raise OSError("synthetic failure after replacement")

    monkeypatch.setattr(auth, "_save_auth_store", save_then_fail)
    with pytest.raises(OSError, match="synthetic failure after replacement"):
        pool.add_entry(cp.PooledCredential.from_dict(PROVIDER, incoming))
    assert {row["id"]: _pair(row) for row in _disk_rows(profile)} == {"incoming": _pair(incoming)}
    assert {entry.id: _pair(entry) for entry in pool.entries()} == {"incoming": _pair(incoming)}
    assert pool._auth_store_path == profile.resolve()
    assert set(pool._auth_store_rows) == {"incoming"}
    assert pool._borrowed_root_ids == set()
    assert pool.current() is None
    assert authority.read_bytes() == authority_bytes
