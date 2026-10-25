"""Optional request rules shared with ATM. Missing/invalid/disabled config allows requests."""
from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
import re

DEFAULT = {"version": 1, "enabled": False, "default_action": "allow", "rules": []}
SOURCES = ("*", "interactive", "cron", "discord", "telegram", "slack", "matrix", "web", "api", "unknown")
log = logging.getLogger(__name__)


class RequestBlocked(RuntimeError):
    failure_reason = "request_guard"


def validate(value):
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("enabled"), bool)
            or value.get("default_action") != "allow"
            or not isinstance(value.get("rules"), list) or len(value["rules"]) > 100):
        raise ValueError("guard-config-invalid")
    result = {"version": 1, "enabled": value["enabled"], "default_action": "allow", "rules": []}
    ids = set()
    for rule in value["rules"]:
        if (not isinstance(rule, dict) or set(rule) - {"id", "action", "provider", "model", "source"}
                or not isinstance(rule.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rule["id"])
                or rule["id"] in ids or rule.get("action") not in ("allow", "block")
                or rule.get("source", "*") not in SOURCES):
            raise ValueError("guard-rule-invalid")
        clean = {"id": rule["id"], "action": rule["action"], "source": rule.get("source", "*")}
        for key in ("provider", "model"):
            pattern = rule.get(key) or "*"
            if (not isinstance(pattern, str) or len(pattern) > 256
                    or not re.fullmatch(r"[A-Za-z0-9_.*?/:+@-]+", pattern)):
                raise ValueError("guard-pattern-invalid")
            clean[key] = pattern
        ids.add(rule["id"])
        result["rules"].append(clean)
    return result


def load(path=None):
    target = Path(path or Path.home() / ".config/token-savings/request-guard.json")
    try:
        return validate(json.loads(target.read_text()))
    except FileNotFoundError:
        return dict(DEFAULT)
    except (OSError, ValueError, TypeError):
        log.warning("Request rules unavailable; using default allow")
        return dict(DEFAULT)


def check(config, provider, model, source):
    if not config["enabled"]:
        return {"allowed": True, "action": "allow", "rule_id": None, "reason": "guard-disabled"}
    for rule in config["rules"]:
        if (fnmatch.fnmatchcase(provider, rule["provider"])
                and fnmatch.fnmatchcase(model, rule["model"])
                and rule["source"] in ("*", source)):
            return {"allowed": rule["action"] == "allow", "action": rule["action"],
                    "rule_id": rule["id"], "reason": "matched-user-rule"}
    return {"allowed": True, "action": "allow", "rule_id": None, "reason": "default-allow"}


def check_request(provider, model, source, *, path=None):
    verdict = check(load(path), str(provider), str(model), str(source))
    if not verdict["allowed"]:
        raise RequestBlocked(f"Request blocked by your rule {verdict['rule_id']}: {provider}/{model} from {source}")
    return verdict
