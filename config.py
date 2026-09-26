#!/usr/bin/env python3
# relay config: load and validate relay.json, plus `relay check` upstream pings.

import http.client
import json
import os
import time
import urllib.parse


class ConfigError(Exception):
    pass


DEFAULT_PORT = 8321
DEFAULT_BASE_URL = "http://127.0.0.1:8318/v1"


def default_log_path():
    # per-user request log location
    return os.path.join(os.path.expanduser("~"), ".relay", "requests.jsonl")


def config_from_env():
    # single upstream built from env, used when no relay.json exists
    return {
        "upstreams": [
            {
                "name": "default",
                "base_url": os.environ.get("RELAY_UPSTREAM_URL", DEFAULT_BASE_URL),
                "api_key_env": "RELAY_UPSTREAM_KEY",
                "models": [],
                "weight": 1,
                "timeout_s": 30,
            }
        ],
        "routes": [{"match": "*", "try": ["default"]}],
        "pricing": [{"match": "*", "in_per_1m": 0.0, "out_per_1m": 0.0}],
        "log_file": default_log_path(),
        "port": DEFAULT_PORT,
    }


def find_config(explicit_path):
    # explicit path wins, then ./relay.json, then the env-based default
    if explicit_path:
        return explicit_path, load_config(explicit_path)
    local = os.path.join(os.getcwd(), "relay.json")
    if os.path.exists(local):
        return local, load_config(local)
    return None, config_from_env()


def load_config(path):
    # read and validate a relay.json file
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("config file is not valid json: %s" % e)
    errors = validate(data)
    if errors:
        raise ConfigError("invalid config:\n" + "\n".join("  - " + e for e in errors))
    return data


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(data):
    # returns a list of error strings, empty when the config is fine
    errors = []
    if not isinstance(data, dict):
        return ["config must be a json object"]

    upstreams = data.get("upstreams")
    names = set()
    if not isinstance(upstreams, list) or not upstreams:
        errors.append("upstreams must be a non-empty list")
    else:
        for i, u in enumerate(upstreams):
            where = "upstreams[%d]" % i
            if not isinstance(u, dict):
                errors.append("%s must be an object" % where)
                continue
            name = u.get("name")
            if not isinstance(name, str) or not name:
                errors.append("%s needs a non-empty name" % where)
            elif name in names:
                errors.append("%s has a duplicate name: %s" % (where, name))
            else:
                names.add(name)
            base = u.get("base_url", "")
            if not isinstance(base, str) or not base.startswith(("http://", "https://")):
                errors.append("%s needs a base_url starting with http:// or https://" % where)
            if "api_key_env" in u and not isinstance(u["api_key_env"], str):
                errors.append("%s api_key_env must be a string" % where)
            models = u.get("models", [])
            if not isinstance(models, list) or any(not isinstance(m, str) for m in models):
                errors.append("%s models must be a list of strings" % where)
            if "weight" in u and (not _is_num(u["weight"]) or u["weight"] < 0):
                errors.append("%s weight must be a number >= 0" % where)
            if "timeout_s" in u and (not _is_num(u["timeout_s"]) or u["timeout_s"] <= 0):
                errors.append("%s timeout_s must be a number > 0" % where)

    routes = data.get("routes")
    if not isinstance(routes, list) or not routes:
        errors.append("routes must be a non-empty list")
    else:
        for i, r in enumerate(routes):
            where = "routes[%d]" % i
            if not isinstance(r, dict):
                errors.append("%s must be an object" % where)
                continue
            if not isinstance(r.get("match"), str) or not r["match"]:
                errors.append("%s needs a non-empty match pattern" % where)
            tried = r.get("try")
            if not isinstance(tried, list) or not tried:
                errors.append("%s needs a non-empty try list" % where)
            else:
                for n in tried:
                    if n not in names:
                        errors.append("%s references unknown upstream: %s" % (where, n))

    pricing = data.get("pricing", [])
    if not isinstance(pricing, list):
        errors.append("pricing must be a list")
    else:
        for i, p in enumerate(pricing):
            where = "pricing[%d]" % i
            if not isinstance(p, dict):
                errors.append("%s must be an object" % where)
                continue
            if not isinstance(p.get("match"), str) or not p["match"]:
                errors.append("%s needs a non-empty match pattern" % where)
            for k in ("in_per_1m", "out_per_1m"):
                if k in p and (not _is_num(p[k]) or p[k] < 0):
                    errors.append("%s %s must be a number >= 0" % (where, k))

    if "log_file" in data and not isinstance(data["log_file"], str):
        errors.append("log_file must be a string path")
    if "port" in data and (not isinstance(data["port"], int) or not 1 <= data["port"] <= 65535):
        errors.append("port must be an integer between 1 and 65535")

    return errors


def _upstream_key(upstream):
    env = upstream.get("api_key_env")
    if env:
        return os.environ.get(env)
    return None


def check_upstreams(cfg, timeout=10):
    # ping each upstream's /v1/models, returns (name, ok, detail, ms) tuples
    results = []
    for u in cfg["upstreams"]:
        base = u["base_url"].rstrip("/")
        parsed = urllib.parse.urlparse(base)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        start = time.monotonic()
        try:
            headers = {}
            key = _upstream_key(u)
            if key:
                headers["Authorization"] = "Bearer " + key
            conn = conn_cls(parsed.hostname, port, timeout=timeout)
            conn.request("GET", (parsed.path or "") + "/v1/models", headers=headers)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            ms = (time.monotonic() - start) * 1000
            if 200 <= resp.status < 300:
                results.append((u["name"], True, "ok", ms))
            else:
                results.append((u["name"], False, "http %d" % resp.status, ms))
        except Exception as e:
            ms = (time.monotonic() - start) * 1000
            results.append((u["name"], False, "%s: %s" % (type(e).__name__, e), ms))
    return results
