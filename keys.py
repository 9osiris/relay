#!/usr/bin/env python3
# relay keys: issue and verify relay api keys, track per-key daily spend caps.
# secrets are never stored in plaintext, only salted sha256 hashes.

import hashlib
import json
import os
import secrets
import threading
import time

KEY_PREFIX = "rl_"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".relay", "keys.json")

_lock = threading.Lock()


def _today():
    return time.strftime("%Y-%m-%d")


def _load():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("keys", [])
    data.setdefault("spend", {})
    return data


def _save(data):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    tmp = KEY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, KEY_FILE)


def _hash(salt, secret):
    return hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()


def add_key(name, daily_cap=None):
    # returns the plaintext key exactly once; only hash and salt are stored
    with _lock:
        data = _load()
        if any(k["name"] == name for k in data["keys"]):
            raise ValueError("key name already exists: %s" % name)
        secret = KEY_PREFIX + secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        data["keys"].append(
            {
                "name": name,
                "salt": salt,
                "hash": _hash(salt, secret),
                "prefix": secret[:10],
                "daily_cap": daily_cap,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        _save(data)
        return secret


def remove_key(name):
    # returns True when a key was removed
    with _lock:
        data = _load()
        before = len(data["keys"])
        data["keys"] = [k for k in data["keys"] if k["name"] != name]
        if len(data["keys"]) != before:
            _save(data)
            return True
        return False


def list_keys():
    # full secrets are never listed, only prefixes
    with _lock:
        data = _load()
        return [
            {
                "name": k["name"],
                "prefix": k.get("prefix", ""),
                "daily_cap": k.get("daily_cap"),
                "created": k.get("created", ""),
            }
            for k in data["keys"]
        ]


def verify(secret):
    # returns the key record when the secret matches, else None
    if not secret or not secret.startswith(KEY_PREFIX):
        return None
    with _lock:
        data = _load()
        for k in data["keys"]:
            if _hash(k["salt"], secret) == k["hash"]:
                return dict(k)
    return None


def spend_today(name):
    with _lock:
        data = _load()
        return float(data["spend"].get(_today(), {}).get(name, 0.0))


def record_spend(name, amount):
    with _lock:
        data = _load()
        day = data["spend"].setdefault(_today(), {})
        day[name] = float(day.get(name, 0.0)) + float(amount)
        _save(data)


def over_cap(record):
    # True when the key has a daily cap and already reached it
    cap = record.get("daily_cap")
    if cap is None:
        return False
    return spend_today(record["name"]) >= float(cap)
