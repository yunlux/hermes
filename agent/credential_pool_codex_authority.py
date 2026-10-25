"""Codex pool rows whose token authority is one explicitly pinned auth store.

A profile borrowing a single-use grant must lock, re-read and commit to its
owner's file.  It may update existing rows, never recreate a removed grant or
flush unrelated stale token pairs back over another process's rotation.
"""
from __future__ import annotations

from dataclasses import replace

import hermes_cli.auth as auth


def _pair(row):
    return row.get("access_token") or "", row.get("refresh_token") or ""


def _same_principal(before, after):
    from agent.credential_pool import _codex_principal_identity
    old = _codex_principal_identity(before.get("access_token"))
    new = _codex_principal_identity(after.get("access_token"))
    # The stable row id owns opaque/legacy tokens. When both JWTs identify a
    # principal, reusing that id for another account must fail closed.
    return old is None or new is None or old == new


def _forget(pool, entry):
    with pool._lock:
        pool._entries = [e for e in pool._entries if e.id != entry.id]
        if pool._current_id == entry.id:
            pool._current_id = None
        pool._auth_store_rows.pop(entry.id, None)


def sync_authority_entry(pool, entry):
    """Re-read this exact row. Caller holds the authority's path-keyed lock."""
    from agent.credential_pool import PooledCredential
    store = auth._load_auth_store(pool._auth_store_path)
    rows = (store.get("credential_pool") or {}).get(pool.provider) or []
    raw = next((r for r in rows if isinstance(r, dict) and r.get("id") == entry.id), None)
    if raw is None or not _same_principal(entry.to_dict(), raw) or not any(_pair(raw)):
        _forget(pool, entry)
        return None
    stored = PooledCredential.from_dict(pool.provider, raw)
    pool._replace_entry(entry, stored)
    pool._auth_store_rows[stored.id] = stored.to_dict()
    return stored


def persist_authority_rows(pool, *, removed_ids=(), status_cleared_ids=(), added_ids=()):
    """Compare-and-update dirty rows; disk owns deletion, identity and newer pairs."""
    from agent.credential_pool import PooledCredential, STATUS_DEAD
    cleared = set(status_cleared_ids or ())
    added = set(added_ids or ())
    with auth._auth_store_lock(target_path=pool._auth_store_path):
        store = auth._load_auth_store(pool._auth_store_path)
        rows = (store.get("credential_pool") or {}).get(pool.provider) or []
        disk_by_id = {r.get("id"): r for r in rows if isinstance(r, dict) and r.get("id")}
        removed = set()
        for cid in removed_ids or ():
            baseline, disk = pool._auth_store_rows.get(cid), disk_by_id.get(cid)
            if baseline is not None and disk is not None and _same_principal(baseline, disk):
                if _pair(baseline) == _pair(disk):
                    removed.add(cid)
                    pool._auth_store_rows.pop(cid, None)
        updates = {}
        for entry in list(pool._entries):
            incoming = entry.to_dict()
            baseline = pool._auth_store_rows.get(entry.id)
            disk = disk_by_id.get(entry.id)
            if entry.id in added:
                if disk is not None:
                    raise ValueError("Codex credential id already exists; reload before adding")
                if any(_pair(row) == _pair(incoming) for row in disk_by_id.values()):
                    raise ValueError("Codex login is already present in the authority pool")
                updates[entry.id] = incoming
                continue
            if baseline is None or disk is None or not _same_principal(baseline, disk):
                _forget(pool, entry)
                continue
            if incoming == baseline:
                continue
            # Our baseline is a compare-and-swap fence, not a timestamp/expiry
            # heuristic: a peer's grant can be newer but expire sooner.
            if _pair(disk) != _pair(baseline):
                stored = PooledCredential.from_dict(pool.provider, disk)
                pool._replace_entry(entry, stored)
                pool._auth_store_rows[entry.id] = stored.to_dict()
                continue
            merged = auth._merge_disk_cooldown_state(
                incoming, None if entry.id in cleared else disk, pool.provider)
            if (disk.get("last_status") == STATUS_DEAD and _pair(incoming) == _pair(disk)
                    and entry.id not in cleared):
                merged = {**merged, **{key: disk.get(key) for key in auth._POOL_STATUS_FIELDS}}
            updates[entry.id] = merged
            _update_matching_singleton(pool, store, entry, baseline, merged)
        if updates or removed:
            store.setdefault("credential_pool", {})[pool.provider] = [
                updates.get(row.get("id"), row) if isinstance(row, dict) else row
                for row in rows if not isinstance(row, dict) or row.get("id") not in removed
            ] + [row for cid, row in updates.items() if cid not in disk_by_id]
            auth._save_auth_store(store, target_path=pool._auth_store_path)
            for cid, row in updates.items():
                stored = PooledCredential.from_dict(pool.provider, row)
                current = next((e for e in pool._entries if e.id == cid), None)
                if current is not None:
                    pool._replace_entry(current, stored)
                pool._auth_store_rows[cid] = stored.to_dict()


def claim_profile_entry(pool, entry):
    """Commit an independent profile login before exposing its new ownership."""
    from agent.credential_pool import CredentialPool, PooledCredential
    path = auth._auth_file_path().resolve()
    with auth._auth_store_lock(target_path=path):
        store = auth._load_auth_store(path)
        rows = (store.get("credential_pool") or {}).get(pool.provider) or []
        pending = CredentialPool(pool.provider, [
            PooledCredential.from_dict(pool.provider, row) for row in rows
        ], auth_store_path=path)
        try:
            added = pending.add_entry(entry)
        finally:
            # Atomic replacement may have succeeded before a directory fsync
            # reports an I/O error. Reconcile ownership from disk in either case.
            try:
                saved = auth._load_auth_store(path)
                saved_rows = (saved.get("credential_pool") or {}).get(pool.provider) or []
            except Exception:
                pool._entries = []
                pool._current_id = None
                raise
            if any(row.get("id") == entry.id and _pair(row) == _pair(entry.to_dict())
                   for row in saved_rows):
                pool._entries = sorted([
                    PooledCredential.from_dict(pool.provider, row) for row in saved_rows
                ], key=lambda item: item.priority)
                pool._auth_store_path = path
                pool._auth_store_rows = {item.id: item.to_dict() for item in pool._entries}
                pool._borrowed_root_ids = set()
                pool._current_id = None
        return added


def _update_matching_singleton(pool, store, entry, baseline, merged):
    """Commit a linked singleton in the same write; a distinct grant stays untouched."""
    state = (store.get("providers") or {}).get(pool.provider)
    if not isinstance(state, dict):
        return
    tokens = state.get("tokens")
    if not isinstance(tokens, dict) or not baseline.get("refresh_token") or _pair(tokens) != _pair(baseline):
        return
    from agent.credential_pool import PooledCredential, STATUS_DEAD
    if merged.get("last_status") == STATUS_DEAD:
        tokens.pop("access_token", None)
        tokens.pop("refresh_token", None)
    elif _pair(merged) != _pair(baseline):
        pool._apply_entry_to_singleton_state(PooledCredential.from_dict(pool.provider, merged), state)


def refresh_authority_entry(pool, entry, *, force):
    """One authority transaction: exact-row sync -> optional POST -> durable row update."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, STATUS_DEAD, _MARK_OK, logger
    with auth._auth_store_lock(timeout_seconds=pool._single_use_refresh_lock_timeout(),
                               target_path=pool._auth_store_path):
        synced = sync_authority_entry(pool, entry)
        if synced is None or synced.last_status == STATUS_DEAD:
            return None
        if synced.auth_type != AUTH_TYPE_OAUTH or not synced.refresh_token:
            if force:
                pool._mark_exhausted(synced, None)
            return None
        if (synced.access_token and synced.access_token != entry.access_token
                and not auth._codex_access_token_is_expiring(synced.access_token, 0)):
            return synced
        try:
            updated = replace(pool._post_tokens_refresh(synced), **_MARK_OK)
        except Exception as exc:
            if auth._is_terminal_codex_oauth_refresh_error(exc):
                logger.warning(
                    "Codex OAuth refresh token is terminally invalid (%s); the credential leaves rotation. "
                    "Re-run 'hermes auth add openai-codex' to sign in again.", exc)
                pool._mark_dead_refresh_grant(synced, exc)
            else:
                pool._mark_exhausted(synced, None)
            return None
        pool._replace_entry(synced, updated)
        pool._persist(status_cleared_ids=[updated.id])
        return pool._find(lambda item: item.id == updated.id)
