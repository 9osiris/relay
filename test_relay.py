#!/usr/bin/env python3
# relay tests: 60+ checks against fake upstreams on 127.0.0.1.
# run with: python3 test_relay.py

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as config_mod
import keys
import log as log_mod
import proxy
import router
import upstream as upstream_mod

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: %s" % name)


# isolate key storage from any real ~/.relay
KEYDIR = tempfile.mkdtemp(prefix="relay-test-keys-")
keys.KEY_FILE = os.path.join(KEYDIR, "keys.json")


# fake upstream servers

class FakeUpstream:
    def __init__(self, handler_fn):
        self.handler_fn = handler_fn
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _run(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                body = self.rfile.read(length) if length else b""
                with outer.lock:
                    outer.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                            "body": body,
                        }
                    )
                    n = len(outer.requests)
                status, payload, ctype = outer.handler_fn(n, body, self.path)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _run
            do_GET = _run

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def chat_payload(text="hello", prompt_t=10, completion_t=5):
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": completion_t,
            "total_tokens": prompt_t + completion_t,
        },
    }


def emb_payload(prompt_t=8):
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "m",
        "usage": {"prompt_tokens": prompt_t, "total_tokens": prompt_t},
    }


def sse_bytes(with_usage=True):
    chunks = [
        'data: {"id":"1","object":"chat.completion.chunk","choices":[{"delta":{"content":"hel"},"index":0}]}',
        'data: {"id":"1","object":"chat.completion.chunk","choices":[{"delta":{"content":"lo"},"index":0}]}',
    ]
    if with_usage:
        chunks.append(
            'data: {"id":"1","object":"chat.completion.chunk","choices":[],"usage":'
            '{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
        )
    chunks.append("data: [DONE]")
    return ("\n".join(chunks) + "\n").encode("utf-8")


def ok_chat(n, body, path):
    return 200, json.dumps(chat_payload()).encode(), "application/json"


def ok_models(n, body, path):
    return 200, json.dumps({"object": "list", "data": []}).encode(), "application/json"


def up_spec(name, port, models=None, key_env=None):
    d = {
        "name": name,
        "base_url": "http://127.0.0.1:%d/v1" % port,
        "models": models or [],
        "weight": 1,
        "timeout_s": 5,
    }
    if key_env:
        d["api_key_env"] = key_env
    return d


def make_cfg(upstreams, routes=None, pricing=None):
    if routes is None:
        routes = [{"match": "*", "try": [u["name"] for u in upstreams]}]
    if pricing is None:
        pricing = [{"match": "*", "in_per_1m": 2.0, "out_per_1m": 4.0}]
    return {
        "upstreams": upstreams,
        "routes": routes,
        "pricing": pricing,
        "log_file": tempfile.mktemp(prefix="relay-test-log-", suffix=".jsonl"),
        "port": 8321,
    }


_servers = []


def start_relay(cfg, auth_enabled=True, admin_token=None):
    state = proxy.RelayState(cfg, None, admin_token, auth_enabled)
    logger = log_mod.RequestLogger(cfg["log_file"], max_bytes=10**9)
    handler = proxy.make_handler(state, logger)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _servers.append(server)
    return server, port, state, logger


def http_request(port, method, path, obj=None, key=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if obj is not None or raw is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else None)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    rheaders = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, rheaders, data


def last_log(logpath, timeout=5):
    # the handler thread logs after the response is sent, so poll briefly
    deadline = time.time() + timeout
    while True:
        try:
            with open(logpath, encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
            if lines:
                return json.loads(lines[-1])
        except FileNotFoundError:
            pass
        if time.time() > deadline:
            raise AssertionError("no log line appeared in %s" % logpath)
        time.sleep(0.05)


def log_entries(logpath):
    with open(logpath, encoding="utf-8") as f:
        return [json.loads(l) for l in f.read().splitlines() if l.strip()]


# router

routes = [
    {"match": "gpt-4o", "try": ["a"]},
    {"match": "gpt-*", "try": ["b", "c"]},
    {"match": "*", "try": ["d"]},
]
check("router exact match", router.resolve_route(routes, "gpt-4o") == ["a"])
check("router prefix match", router.resolve_route(routes, "gpt-4o-mini") == ["b", "c"])
check("router catch-all", router.resolve_route(routes, "llama-3") == ["d"])
check("router first match wins", router.resolve_route(routes, "gpt-4o") == ["a"])
check("router no match empty", router.resolve_route([{"match": "zzz", "try": ["a"]}], "gpt-4o") == [])
check("router prefix does not match mid-string", router.resolve_route(routes, "xgpt-4") == ["d"])
check("router star matches everything", router.match_pattern("*", "anything-at-all"))
check("router try order preserved", router.resolve_route(routes, "gpt-x") == ["b", "c"])

# config validation

good = {
    "upstreams": [{"name": "a", "base_url": "http://127.0.0.1:1/v1", "models": ["m"]}],
    "routes": [{"match": "*", "try": ["a"]}],
    "pricing": [{"match": "*", "in_per_1m": 1.0, "out_per_1m": 2.0}],
}
check("config valid passes", config_mod.validate(good) == [])
bad = dict(good, upstreams=[])
check("config empty upstreams fails", len(config_mod.validate(bad)) > 0)
dup = dict(good, upstreams=good["upstreams"] + [{"name": "a", "base_url": "http://x/v1"}])
check("config duplicate names fails", any("duplicate" in e for e in config_mod.validate(dup)))
badroute = dict(good, routes=[{"match": "*", "try": ["nope"]}])
check("config unknown upstream fails", len(config_mod.validate(badroute)) > 0)
badurl = dict(good, upstreams=[{"name": "a", "base_url": "ftp://x/v1"}])
check("config bad base_url fails", len(config_mod.validate(badurl)) > 0)
badtimeout = dict(good, upstreams=[{"name": "a", "base_url": "http://x/v1", "timeout_s": 0}])
check("config zero timeout fails", len(config_mod.validate(badtimeout)) > 0)
badprice = dict(good, pricing=[{"match": "*", "in_per_1m": -1.0}])
check("config negative pricing fails", len(config_mod.validate(badprice)) > 0)
emptytry = dict(good, routes=[{"match": "*", "try": []}])
check("config empty try fails", len(config_mod.validate(emptytry)) > 0)
envcfg = config_mod.config_from_env()
check(
    "config env default single upstream",
    len(envcfg["upstreams"]) == 1 and envcfg["routes"] == [{"match": "*", "try": ["default"]}],
)

# pricing

pricing = [
    {"match": "gpt-4o", "in_per_1m": 5.0, "out_per_1m": 15.0},
    {"match": "gpt-*", "in_per_1m": 1.0, "out_per_1m": 2.0},
    {"match": "*", "in_per_1m": 0.5, "out_per_1m": 0.5},
]
check("pricing exact pattern", router.price_for(pricing, "gpt-4o") == (5.0, 15.0))
check("pricing prefix pattern", router.price_for(pricing, "gpt-4o-mini") == (1.0, 2.0))
check(
    "pricing cost math",
    abs(router.cost_usd(pricing, "gpt-4o-mini", 1_000_000, 1_000_000) - 3.0) < 1e-9,
)

# basic proxy

up_a = FakeUpstream(ok_chat)
cfg = make_cfg([up_spec("a", up_a.port, models=["m1"])])
srv, port, state, logger = start_relay(cfg, auth_enabled=True)
secret = keys.add_key("tester-%d" % port, None)
status, headers, data = http_request(
    port, "POST", "/v1/chat/completions",
    {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}, key=secret,
)
check("proxy chat 200", status == 200)
check("proxy X-Relay-Upstream header", headers.get("x-relay-upstream") == "a")
check("proxy body matches upstream", json.loads(data)["choices"][0]["message"]["content"] == "hello")
status, _, _ = http_request(
    port, "POST", "/v1/chat/completions",
    {"model": "m1", "messages": []},
)
check("proxy auth missing rejected", status == 401)
status, _, data = http_request(
    port, "POST", "/v1/chat/completions",
    {"model": "m1", "messages": []}, key="rl_bogus",
)
check("proxy bad key rejected", status == 401)

# no route and bad json

cfg2 = make_cfg([up_spec("a", up_a.port)], routes=[{"match": "aaa*", "try": ["a"]}])
srv2, port2, _, _ = start_relay(cfg2, auth_enabled=False)
status, _, data = http_request(port2, "POST", "/v1/chat/completions", {"model": "zzz", "messages": []})
check("proxy no route 400", status == 400 and b"no route" in data)
status, _, _ = http_request(port2, "POST", "/v1/chat/completions", raw=b"not json{")
check("proxy bad json 400", status == 400)

# fallback: 500 then success

def fail500_then_ok(n, body, path):
    if n == 1:
        return 500, b"boom", "text/plain"
    return ok_chat(n, body, path)

up_b1 = FakeUpstream(fail500_then_ok)
up_b2 = FakeUpstream(ok_chat)
cfg3 = make_cfg(
    [up_spec("a", up_b1.port), up_spec("b", up_b2.port)],
    routes=[{"match": "*", "try": ["a", "b"]}],
)
srv3, port3, state3, _ = start_relay(cfg3, auth_enabled=False)
status, headers, data = http_request(
    port3, "POST", "/v1/chat/completions", {"model": "m", "messages": []}
)
check("fallback 500 then success", status == 200)
check("fallback header names second upstream", headers.get("x-relay-upstream") == "b")
entry = last_log(cfg3["log_file"])
check("fallback count logged", entry["fallbacks"] == 1 and entry["upstream"] == "b")

# fallback: 429 then success

def fail429_then_ok(n, body, path):
    if n == 1:
        return 429, b"slow down", "text/plain"
    return ok_chat(n, body, path)

up_c1 = FakeUpstream(fail429_then_ok)
up_c2 = FakeUpstream(ok_chat)
cfg4 = make_cfg(
    [up_spec("a", up_c1.port), up_spec("b", up_c2.port)],
    routes=[{"match": "*", "try": ["a", "b"]}],
)
srv4, port4, _, _ = start_relay(cfg4, auth_enabled=False)
status, headers, _ = http_request(port4, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("fallback 429 then success", status == 200 and headers.get("x-relay-upstream") == "b")

# fallback: connection refused then success

import socket

s = socket.socket()
s.bind(("127.0.0.1", 0))
dead_port = s.getsockname()[1]
s.close()
up_d2 = FakeUpstream(ok_chat)
cfg5 = make_cfg(
    [up_spec("dead", dead_port), up_spec("b", up_d2.port)],
    routes=[{"match": "*", "try": ["dead", "b"]}],
)
srv5, port5, _, _ = start_relay(cfg5, auth_enabled=False)
status, headers, _ = http_request(port5, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("fallback conn refused then success", status == 200 and headers.get("x-relay-upstream") == "b")

# all fail -> 502

cfg6 = make_cfg([up_spec("dead", dead_port)], routes=[{"match": "*", "try": ["dead"]}])
srv6, port6, _, _ = start_relay(cfg6, auth_enabled=False)
status, _, data = http_request(port6, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("proxy all fail 502", status == 502 and b"error" in data)

# upstream 400 forwarded without retry

def always400(n, body, path):
    return 400, json.dumps({"error": {"message": "bad request"}}).encode(), "application/json"

up_e = FakeUpstream(always400)
up_e2 = FakeUpstream(ok_chat)
cfg7 = make_cfg(
    [up_spec("a", up_e.port), up_spec("b", up_e2.port)],
    routes=[{"match": "*", "try": ["a", "b"]}],
)
srv7, port7, _, _ = start_relay(cfg7, auth_enabled=False)
status, headers, data = http_request(port7, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("proxy 400 forwarded as-is", status == 400 and b"bad request" in data)
check("proxy 400 no retry", len(up_e.requests) == 1 and len(up_e2.requests) == 0)
check("proxy 400 keeps upstream header", headers.get("x-relay-upstream") == "a")

# streaming

def sse_ok(n, body, path):
    return 200, sse_bytes(with_usage=True), "text/event-stream"

def sse_no_usage(n, body, path):
    return 200, sse_bytes(with_usage=False), "text/event-stream"

up_s = FakeUpstream(sse_ok)
cfg8 = make_cfg([up_spec("a", up_s.port)])
srv8, port8, _, _ = start_relay(cfg8, auth_enabled=False)
status, headers, data = http_request(
    port8, "POST", "/v1/chat/completions",
    {"model": "m", "messages": [], "stream": True},
)
check("stream 200 event-stream", status == 200 and headers.get("content-type") == "text/event-stream")
check("stream bytes passthrough untouched", data == sse_bytes(with_usage=True))
entry = last_log(cfg8["log_file"])
check(
    "stream usage captured",
    entry["prompt_tokens"] == 10 and entry["completion_tokens"] == 5,
)

up_s2 = FakeUpstream(sse_no_usage)
cfg9 = make_cfg([up_spec("a", up_s2.port)])
srv9, port9, _, _ = start_relay(cfg9, auth_enabled=False)
status, _, _ = http_request(
    port9, "POST", "/v1/chat/completions",
    {"model": "m", "messages": [], "stream": True},
)
entry = last_log(cfg9["log_file"])
check("stream no usage estimated", entry["completion_tokens"] > 0 and entry["prompt_tokens"] >= 1)

# embeddings

def ok_emb(n, body, path):
    return 200, json.dumps(emb_payload()).encode(), "application/json"

up_emb = FakeUpstream(ok_emb)
cfg10 = make_cfg([up_spec("a", up_emb.port)])
srv10, port10, _, _ = start_relay(cfg10, auth_enabled=False)
status, headers, data = http_request(
    port10, "POST", "/v1/embeddings", {"model": "e", "input": "hello world"}
)
check("embeddings proxied", status == 200 and b"embedding" in data)
check("embeddings upstream header", headers.get("x-relay-upstream") == "a")
entry = last_log(cfg10["log_file"])
check("embeddings usage logged", entry["prompt_tokens"] == 8 and entry["completion_tokens"] == 0)

# models aggregation

up_m1 = FakeUpstream(ok_chat)
up_m2 = FakeUpstream(ok_chat)
cfg11 = make_cfg([up_spec("a", up_m1.port, models=["x", "y"]), up_spec("b", up_m2.port, models=["y", "z"])])
srv11, port11, _, _ = start_relay(cfg11, auth_enabled=False)
status, _, data = http_request(port11, "GET", "/v1/models")
ids = sorted(m["id"] for m in json.loads(data)["data"])
check("models aggregated deduped", ids == ["x", "y", "z"])
owned = {m["id"]: m["owned_by"] for m in json.loads(data)["data"]}
check("models owned_by first upstream", owned == {"x": "a", "y": "a", "z": "b"})

# circuit breaker

def always500(n, body, path):
    return 500, b"boom", "text/plain"

up_f1 = FakeUpstream(always500)
up_f2 = FakeUpstream(ok_chat)
cfg12 = make_cfg(
    [up_spec("a", up_f1.port), up_spec("b", up_f2.port)],
    routes=[{"match": "*", "try": ["a", "b"]}],
)
srv12, port12, state12, _ = start_relay(cfg12, auth_enabled=False)
for _ in range(3):
    http_request(port12, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("breaker trips after 3 failures", state12.by_name["a"].breaker_state() == "open")
status, headers, _ = http_request(port12, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check(
    "breaker skips tripped upstream",
    headers.get("x-relay-upstream") == "b" and len(up_f1.requests) == 3,
)

# half-open recovery

def fail3_then_ok(n, body, path):
    if n <= 3:
        return 500, b"boom", "text/plain"
    return ok_chat(n, body, path)

up_g = FakeUpstream(fail3_then_ok)
cfg13 = make_cfg([up_spec("a", up_g.port)])
srv13, port13, state13, _ = start_relay(cfg13, auth_enabled=False)
state13.by_name["a"].half_open_after = 0.15
for _ in range(3):
    http_request(port13, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("breaker open before half-open", state13.by_name["a"].breaker_state() == "open")
time.sleep(0.25)
status, _, _ = http_request(port13, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("half-open probe success closes", status == 200 and state13.by_name["a"].breaker_state() == "closed")

# half-open probe failure re-opens

up_h = FakeUpstream(always500)
cfg14 = make_cfg([up_spec("a", up_h.port)])
srv14, port14, state14, _ = start_relay(cfg14, auth_enabled=False)
state14.by_name["a"].half_open_after = 0.15
for _ in range(3):
    http_request(port14, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
time.sleep(0.25)
http_request(port14, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check(
    "half-open probe failure re-opens",
    state14.by_name["a"].breaker_state() == "open"
    and state14.by_name["a"].consecutive_failures == 4,
)

# status shows breaker and latency

status, _, data = http_request(port12, "GET", "/relay/status")
obj = json.loads(data)
ups = {u["name"]: u for u in obj["upstreams"]}
check("status breaker visible", ups["a"]["breaker"] == "open")
check("status latency tracked", ups["b"]["avg_latency_ms"] > 0 and ups["b"]["requests"] >= 4)

# keys

secret_a = keys.add_key("alpha-%d" % port, None)
check("keys add verifies", keys.verify(secret_a) is not None)
check("keys wrong secret rejected", keys.verify("rl_nonexistent_secret") is None)
listed = keys.list_keys()
names = [k["name"] for k in listed]
check("keys list has names", any("alpha-" in n for n in names))
check("keys list hides full secret", secret_a not in json.dumps(listed))
with open(keys.KEY_FILE, encoding="utf-8") as f:
    stored = f.read()
check("keys no plaintext in storage", secret_a not in stored and "salt" in stored and "hash" in stored)
check("keys rm missing returns false", keys.remove_key("definitely-not-a-key") is False)
check("keys rm real works", keys.remove_key([k["name"] for k in listed if "alpha-" in k["name"]][0]) is True)

# daily cap -> 402

up_cap = FakeUpstream(ok_chat)
cfg15 = make_cfg([up_spec("a", up_cap.port)])
srv15, port15, _, _ = start_relay(cfg15, auth_enabled=True)
capped = keys.add_key("capped-%d" % port15, 0.00001)
status, _, _ = http_request(
    port15, "POST", "/v1/chat/completions",
    {"model": "m", "messages": []}, key=capped,
)
check("proxy under cap allowed", status == 200)
status, _, data = http_request(
    port15, "POST", "/v1/chat/completions",
    {"model": "m", "messages": []}, key=capped,
)
check("proxy over cap 402", status == 402 and b"cap" in data)
check("proxy spend recorded", keys.spend_today("capped-%d" % port15) > 0)

# --no-auth mode

srv16, port16, _, _ = start_relay(cfg15, auth_enabled=False)
status, _, _ = http_request(port16, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("proxy no-auth allows", status == 200)

# log schema and cost math

entry = last_log(cfg15["log_file"])
for field in ("timestamp", "key", "model", "upstream", "fallbacks", "latency_ms",
              "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "status"):
    check("log has field %s" % field, field in entry)
expected = 10 / 1_000_000 * 2.0 + 5 / 1_000_000 * 4.0
check("log cost math", abs(entry["cost_usd"] - round(expected, 6)) < 1e-9)
check("log key name recorded", any(
    e["key"] == "capped-%d" % port15 for e in log_entries(cfg15["log_file"])
))

# log rotation

rpath = tempfile.mktemp(prefix="relay-rot-", suffix=".jsonl")
rlog = log_mod.RequestLogger(rpath, max_bytes=300, keep=3)
for i in range(20):
    rlog.log({"i": i, "pad": "x" * 60})
check("log rotation keeps 3", os.path.exists(rpath + ".3") and not os.path.exists(rpath + ".4"))

# admin: status shape

status, _, data = http_request(port15, "GET", "/relay/status")
obj = json.loads(data)
check(
    "status shape",
    obj["version"] == "0.1.0"
    and "uptime_s" in obj
    and isinstance(obj["upstreams"], list)
    and isinstance(obj["keys_today"], list),
)
check(
    "status keys spend visible",
    any(k["spend_usd"] > 0 for k in obj["keys_today"]),
)

# admin: reload

cfgpath = tempfile.mktemp(prefix="relay-cfg-", suffix=".json")
up_r1 = FakeUpstream(ok_chat)
up_r2 = FakeUpstream(ok_chat)
with open(cfgpath, "w", encoding="utf-8") as f:
    json.dump(make_cfg([up_spec("a", up_r1.port), up_spec("b", up_r2.port)],
                       routes=[{"match": "*", "try": ["a"]}],), f)
rcfg = config_mod.load_config(cfgpath)
rstate = proxy.RelayState(rcfg, cfgpath, "admintoken123", True)
rlogger = log_mod.RequestLogger(tempfile.mktemp(suffix=".jsonl"), max_bytes=10**9)
rhandler = proxy.make_handler(rstate, rlogger)
rserver = ThreadingHTTPServer(("127.0.0.1", 0), rhandler)
rserver.daemon_threads = True
rport = rserver.server_address[1]
threading.Thread(target=rserver.serve_forever, daemon=True).start()
_servers.append(rserver)
reload_key = keys.add_key("reload-key", None)
status, headers, _ = http_request(rport, "POST", "/v1/chat/completions", {"model": "m", "messages": []}, key=reload_key)
check("reload before routes to a", headers.get("x-relay-upstream") == "a")
status, _, _ = http_request(rport, "POST", "/relay/reload")
check("reload without token 401", status == 401)
with open(cfgpath, "w", encoding="utf-8") as f:
    json.dump(make_cfg([up_spec("a", up_r1.port), up_spec("b", up_r2.port)],
                       routes=[{"match": "*", "try": ["b"]}],), f)
status, _, data = http_request(
    rport, "POST", "/relay/reload", key="admintoken123"
)
check("reload with token ok", status == 200 and json.loads(data)["ok"] is True)
status, headers, _ = http_request(rport, "POST", "/v1/chat/completions", {"model": "m", "messages": []}, key=reload_key)
check("reload applied new route", headers.get("x-relay-upstream") == "b")

# dashboard

status, headers, data = http_request(rport, "GET", "/relay/")
check(
    "dashboard html served",
    status == 200 and b"text/html" in headers.get("content-type", "").encode()
    and b"setInterval" in data and b"relay" in data,
)

# check_upstreams

up_chk = FakeUpstream(ok_models)
chk_cfg = make_cfg([up_spec("chk", up_chk.port)])
results = config_mod.check_upstreams(chk_cfg)
check("check_upstreams pass", results[0][1] is True and results[0][2] == "ok")

# api_key_env: no header sent when unset

up_k = FakeUpstream(ok_chat)
cfgk = make_cfg([up_spec("a", up_k.port)])
srvk, portk, _, _ = start_relay(cfgk, auth_enabled=False)
http_request(portk, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
check("no auth header without api_key_env", "authorization" not in up_k.requests[0]["headers"])

# version

out = subprocess.run(
    [sys.executable, "relay.py", "--version"], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
)
check("cli version", out.stdout.strip() == "relay 0.1.0")

# cleanup

for s_ in _servers:
    try:
        s_.shutdown()
        s_.server_close()
    except Exception:
        pass
for u in (up_a, up_b1, up_b2, up_c1, up_c2, up_d2, up_e, up_e2, up_s, up_s2, up_emb,
          up_m1, up_m2, up_f1, up_f2, up_g, up_h, up_cap, up_r1, up_r2, up_chk, up_k):
    try:
        u.close()
    except Exception:
        pass

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
