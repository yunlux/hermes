"""Best-effort request usage recording, with no billing admission or cost enforcement."""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import uuid
from urllib.parse import urlparse

log = logging.getLogger(__name__)
_EXECUTION = contextvars.ContextVar("hermes_usage_execution", default=None)


def execution(agent=None, runtime=None):
    from hermes_constants import get_hermes_home
    from gateway.session_context import get_session_env
    runtime, bound = runtime or {}, _EXECUTION.get() or {}
    db = getattr(agent, "_session_db", None)
    home = str(Path(db.db_path).parent) if db is not None else str(runtime.get("billing_home") or bound.get("home") or get_hermes_home())
    sid = str(getattr(agent, "session_id", "") or runtime.get("session_id") or bound.get("session_id") or "")
    source_value = runtime.get("billing_source") or bound.get("source") or getattr(agent, "platform", "") or get_session_env("HERMES_SESSION_SOURCE", "") or get_session_env("HERMES_SESSION_PLATFORM", "") or "interactive"
    source = str(getattr(source_value, "value", source_value))
    if get_session_env("HERMES_CRON_SESSION", "") == "1" or sid.startswith("cron_"):
        source = "cron"
    return {"source": source, "home": home, "session_id": sid,
            "profile": Path(home).name if Path(home).parent.name == "profiles" else "default"}


@contextlib.contextmanager
def execution_scope(agent):
    token = _EXECUTION.set(execution(agent))
    try:
        yield
    finally:
        _EXECUTION.reset(token)


def _physical_client(client):
    for _ in range(5):
        if str(getattr(client, "base_url", "") or ""):
            return client
        inner = getattr(client, "_client", None)
        if inner is None or inner is client:
            break
        client = inner
    return client


def _ledger(info, **update):
    """One row per request ID; duplicate observations overwrite usage rather than add."""
    try:
        path = Path(info["home"]) / "token_requests.db"
        with sqlite3.connect(path, timeout=3) as db:
            os.chmod(path, 0o600)
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, created_at REAL, source TEXT, profile TEXT,
                session_id TEXT, task TEXT, provider TEXT, endpoint TEXT, model TEXT,
                outcome TEXT, response_id TEXT, input_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0)""")
            db.execute("INSERT OR IGNORE INTO requests(request_id,created_at,source,profile,session_id,task,provider,endpoint,model,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       tuple(info[k] for k in ("request_id", "created_at", "source", "profile", "session_id", "task", "provider", "endpoint", "model", "outcome")))
            fields = [(k, v) for k, v in update.items() if k in {"outcome", "response_id", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"}]
            if fields:
                db.execute("UPDATE requests SET " + ",".join(k + "=?" for k, _ in fields) + " WHERE request_id=?",
                           [v for _, v in fields] + [info["request_id"]])
    except (OSError, sqlite3.Error):
        log.warning("Request usage ledger unavailable; request continues")


def observe_request(client, kwargs, *, agent=None, provider="", api_mode="", task="main", runtime=None):
    ctx = execution(agent, runtime)
    physical = _physical_client(client)
    route = urlparse(str(getattr(physical, "base_url", "") or getattr(agent, "base_url", "") or ""))
    host = route.hostname or ""
    try:
        if route.port:
            host += f":{route.port}"
    except ValueError:
        pass
    info = dict(ctx, request_id=uuid.uuid4().hex, created_at=datetime.now(timezone.utc).timestamp(), task=task,
                provider=str(provider or getattr(agent, "provider", "") or ""),
                endpoint=f"{route.scheme}://{host}{route.path}".rstrip("/"),
                model=str(kwargs.get("model") or getattr(agent, "model", "") or ""), outcome="sent")
    from hermes_cli.request_guard import check_request
    check_request(info["provider"], info["model"], info["source"])
    _ledger(info)
    if agent is not None:
        agent._usage_request = info
    return info


def record_response(info, response, *, api_mode="chat_completions"):
    if not info or getattr(response, "usage", None) is None:
        return
    try:
        from agent.usage_pricing import normalize_usage
        usage = normalize_usage(response.usage, provider=info["provider"], api_mode=api_mode)
        _ledger(info, outcome="completed", response_id=str(getattr(response, "id", "") or ""),
                **{k: getattr(usage, k) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")})
    except Exception:
        log.warning("Request usage could not be normalized; response continues")


def failed(info, exc):
    _ledger(info, outcome="quota" if getattr(exc, "status_code", None) in (402, 429) else "failed")


def recorded_call(client, kwargs, callback, *, agent=None, provider="", api_mode="", task="main", runtime=None):
    info = observe_request(client, kwargs, agent=agent, provider=provider, api_mode=api_mode, task=task, runtime=runtime)
    try:
        response = callback(kwargs)
    except Exception as exc:
        failed(info, exc)
        raise
    record_response(info, response, api_mode=api_mode)
    return RecordedStream(response, info, api_mode) if kwargs.get("stream") is True and hasattr(response, "__next__") else response


class RecordedStream:
    def __init__(self, stream, info, api_mode):
        self.stream, self.info, self.api_mode = stream, info, api_mode
    def __iter__(self):
        return self
    def __next__(self):
        try:
            item = next(self.stream)
        except StopIteration:
            raise
        except Exception as exc:
            failed(self.info, exc)
            raise
        record_response(self.info, item, api_mode=self.api_mode)
        if getattr(item, "type", "") == "response.completed":
            record_response(self.info, item.response, api_mode=self.api_mode)
        return item
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def close(self):
        close = getattr(self.stream, "close", None)
        if close:
            return close()


async def recorded_async_call(client, kwargs, callback, **route):
    info = observe_request(client, kwargs, **route)
    try:
        response = await callback(kwargs)
    except Exception as exc:
        failed(info, exc)
        raise
    record_response(info, response, api_mode=route.get("api_mode", ""))
    return response
