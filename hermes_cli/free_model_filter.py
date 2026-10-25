"""Free/local model menus with native ChatGPT and OpenCode Go list exceptions.

The shared snapshot contains only public model identifiers and evidence timestamps.
Run ``python -m hermes_cli.free_model_filter --refresh`` to update it without credentials.
Native Hermes authentication and account catalogs govern the ChatGPT login route;
These visibility exceptions do not establish entitlement or make paid routes free.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

URLS = {
    "openrouter": "https://openrouter.ai/api/v1/models",
    "nous": "https://inference-api.nousresearch.com/v1/models",
    "opencode-zen": "https://opencode.ai/zen/v1/models",
    "opencode-go": "https://opencode.ai/zen/go/v1/models",
    "opencode-metadata": "https://models.opencode.ai/api.json",
}


def snapshot_path():
    return Path.home() / ".config/token-savings/model-filter.json"


def read_snapshot(path=None):
    try:
        value = json.loads(Path(path or snapshot_path()).read_text())
        return value if isinstance(value, dict) and value.get("version") == 1 else {}
    except (OSError, ValueError, TypeError):
        return {}


def zero_priced(pricing, required=("prompt", "completion")):
    """All advertised monetary fields must be finite zero; missing required fields are unknown."""
    if not isinstance(pricing, dict) or not all(k in pricing for k in required):
        return False
    try:
        return bool(pricing) and all(not isinstance(v, bool) and Decimal(str(v)).is_finite()
                                     and Decimal(str(v)) == 0 for v in pricing.values())
    except (InvalidOperation, ValueError, TypeError):
        return False


def _provider_key(provider, base_url=""):
    slug = str(provider or "").strip().lower()
    aliases = {"opencode": "opencode-zen", "zen": "opencode-zen", "go": "opencode-go"}
    slug = aliases.get(slug, slug)
    host = urlparse(str(base_url or "")).hostname
    if host == "openrouter.ai":
        return "openrouter"
    if host == "inference-api.nousresearch.com":
        return "nous"
    if host == "opencode.ai":
        return "opencode-go" if "/zen/go" in base_url else "opencode-zen"
    return slug


def _is_local(provider, base_url):
    if str(provider or "").lower() in {"ollama", "llamacpp", "lmstudio", "lm-studio"}:
        return True
    return urlparse(str(base_url or "")).hostname in {"localhost", "127.0.0.1", "::1"}


def is_chatgpt_subscription_route(provider, base_url=""):
    """Recognize the native login route, whose account admission remains upstream."""
    if str(provider or "").strip().lower() not in {"openai-codex", "chatgpt-cn", "chatgpt-com"}:
        return False
    if not base_url:
        return True
    url = urlparse(str(base_url))
    return (url.scheme == "https" and url.hostname == "chatgpt.com"
            and url.path.rstrip("/") == "/backend-api/codex")


def is_unfiltered_menu_route(provider, base_url=""):
    """User-selected list exceptions; account admission remains native."""
    return (is_chatgpt_subscription_route(provider, base_url)
            or _provider_key(provider, base_url) == "opencode-go")


def free_model_ids(provider, model_ids, *, base_url="", snapshot=None, require_tools=False):
    """Filter offered IDs only; explicit model/provider requests remain untouched."""
    if is_unfiltered_menu_route(provider, base_url):
        return model_ids
    if _is_local(provider, base_url):
        return [str(mid) for mid in model_ids if not str(mid).lower().endswith(":cloud")]
    data = snapshot if snapshot is not None else read_snapshot()
    key = _provider_key(provider, base_url)
    allowed = set(data.get("providers", {}).get(key, []))
    if require_tools:
        metadata = data.get("model_metadata", {}).get(key, {})
        allowed = {mid for mid in allowed if metadata.get(mid, {}).get("tools") is not False}
    return [mid for mid in model_ids if str(mid) in allowed]


def filter_provider_rows(rows, *, snapshot=None):
    data = snapshot if snapshot is not None else read_snapshot()
    out = []
    for row in rows:
        models = free_model_ids(row.get("provider_id") or row.get("slug"), row.get("models") or [],
                                base_url=row.get("api_url") or row.get("base_url") or "", snapshot=data)
        if not models:
            continue
        item = dict(row, models=models, total_models=len(models))
        if (isinstance(row.get("unavailable_models"), list)
                and not is_unfiltered_menu_route(row.get("provider_id") or row.get("slug"),
                    row.get("api_url") or row.get("base_url") or "")):
            item["unavailable_models"] = []
        out.append(item)
    # Menu-only de-duplication: never alter authentication or explicit routing.
    # Both usable account rows must be present; a missing/disabled plugin keeps
    # the native aggregate entry as the compatibility fallback.
    try:
        from hermes_cli.config import get_hermes_home
        home = get_hermes_home()
    except ImportError:  # Standalone ATM review mirror.
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    account_ids = {row.get("provider_id") or row.get("slug") for row in out}
    if (Path(home).absolute() == Path.home() / ".hermes/profiles/soma"
            and {"chatgpt-cn", "chatgpt-com"}.issubset(account_ids)):
        out = [row for row in out
               if (row.get("provider_id") or row.get("slug")) != "openai-codex"]
    return out


def _fetch(url, timeout):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    return data, datetime.now(timezone.utc).isoformat()


def _write_snapshot(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh_catalog(path=None, *, timeout=20, fetcher=None):
    """Refresh public sources independently. Failed sources keep old IDs and verification dates.

    ``path`` and ``fetcher`` are injection seams for offline tests and ATM integration.
    """
    path = Path(path or snapshot_path())
    result = read_snapshot(path)
    result.setdefault("version", 1)
    result["mode"] = "filter-only"
    result.setdefault("providers", {})
    result.setdefault("sources", {})
    result.setdefault("model_metadata", {})
    result.setdefault("unfiltered_providers", {})
    successes, errors = {}, {}
    fetcher = fetcher or _fetch
    with ThreadPoolExecutor(max_workers=len(URLS)) as pool:
        future_keys = {pool.submit(fetcher, url, timeout): key for key, url in URLS.items()}
        for future in as_completed(future_keys):
            key = future_keys[future]
            try:
                payload, checked_at = future.result()
                if not isinstance(payload, dict):
                    raise ValueError("invalid catalog")
                if key == "opencode-metadata":
                    if not isinstance(payload.get("opencode", {}).get("models"), dict):
                        raise ValueError("invalid metadata")
                elif not isinstance(payload.get("data"), list):
                    raise ValueError("invalid model list")
                successes[key] = (payload, checked_at)
            except Exception as exc:
                errors[key] = type(exc).__name__
    for key in ("openrouter", "nous"):
        if key not in successes:
            continue
        payload, checked_at = successes[key]
        rows = [row for row in payload["data"] if isinstance(row, dict)
                and row.get("billing_mode") != "subscription" and zero_priced(row.get("pricing"))]
        result["providers"][key] = [str(row["id"]) for row in rows if row.get("id")]
        result["model_metadata"][key] = {
            str(row["id"]): {"tools": "tools" in row.get("supported_parameters", []),
                             "context_length": row.get("context_length"),
                             "pricing": row.get("pricing"),
                             "architecture": row.get("architecture"),
                             "billing_mode": row.get("billing_mode")}
            for row in rows if row.get("id")}
        result["sources"][key] = {"url": URLS[key], "verified_at": checked_at, "count": len(payload["data"])}
    metadata_success = successes.get("opencode-metadata")
    if metadata_success:
        payload, checked_at = metadata_success
        metadata = payload["opencode"]["models"]
        for key in ("opencode-zen",):
            if key not in successes:
                continue
            live_payload, live_at = successes[key]
            ids = [str(row["id"]) for row in live_payload["data"] if isinstance(row, dict) and row.get("id")]
            zero_ids = [mid for mid in ids
                        if zero_priced(metadata.get(mid, {}).get("cost"), ("input", "output"))]
            result["providers"][key] = zero_ids
            result["model_metadata"][key] = {
                mid: {"tools": metadata[mid].get("tool_call"),
                      "context_length": metadata[mid].get("limit", {}).get("context")}
                for mid in result["providers"][key]}
            result["sources"][key] = {"url": URLS[key], "verified_at": live_at, "count": len(ids),
                                      "pricing_verified_at": checked_at}
        result["sources"]["opencode-metadata"] = {"url": URLS["opencode-metadata"], "verified_at": checked_at, "count": len(metadata)}
    if "opencode-go" in successes:
        live_payload, live_at = successes["opencode-go"]
        rows = [row for row in live_payload["data"] if isinstance(row, dict) and row.get("id")]
        ids = [str(row["id"]) for row in rows]
        # Kept separately so zero-price metadata on a Go route never implies free access.
        result["unfiltered_providers"]["opencode-go"] = ids
        previous = result["model_metadata"].get("opencode-go", {})
        metadata = metadata_success[0].get("opencode-go", {}).get("models", {}) if metadata_success else {}
        if metadata_success:
            metadata = {**metadata_success[0]["opencode"]["models"], **metadata}
        result["model_metadata"]["opencode-go"] = {}
        for row in rows:
            mid = str(row["id"])
            item = dict(previous.get(mid, {}))
            if mid in metadata:
                item.update(tools=metadata[mid].get("tool_call"),
                            context_length=metadata[mid].get("limit", {}).get("context"),
                            cost=metadata[mid].get("cost"))
            if row.get("billing_mode") is not None:
                item["billing_mode"] = row["billing_mode"]
            if row.get("pricing") is not None:
                item["pricing"] = row["pricing"]
            item["visibility_exception"] = "user-requested-unfiltered-go"
            result["model_metadata"]["opencode-go"][mid] = item
        result["sources"]["opencode-go"] = {
            **result["sources"].get("opencode-go", {}),
            "url": URLS["opencode-go"], "verified_at": live_at, "count": len(ids)}
        if metadata_success:
            result["sources"]["opencode-go"]["pricing_verified_at"] = metadata_success[1]
        result.get("excluded_providers", {}).pop("opencode-go", None)
    # Record failures separately; they never refresh the corresponding successful evidence date.
    result["providers"]["opencode-go"] = []
    result["errors"] = errors
    result["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_snapshot(path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    data = refresh_catalog(args.path) if args.refresh else read_snapshot(args.path)
    print(json.dumps({"counts": {key: len(ids) for key, ids in data.get("providers", {}).items()},
                      "unfiltered_counts": {key: len(ids) for key, ids in data.get("unfiltered_providers", {}).items()},
                      "errors": data.get("errors", {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
