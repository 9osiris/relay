#!/usr/bin/env python3
# relay proxy: openai-compatible gateway with routing, fallback,
# key auth and budgets, request logging, admin api, dashboard.

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as config_mod
import keys
import log as log_mod
import router
import upstream as upstream_mod
from dashboard import DASHBOARD_HTML

VERSION = "0.1.0"
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


def _backoff():
    # small jittered pause before trying the next upstream
    time.sleep(random.uniform(0.05, 0.15))


def parse_sse_usage(buf):
    # last usage object found in an sse byte stream, or None
    usage = None
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    return usage


def parse_json_usage(data):
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
        return obj["usage"]
    return None


def _estimate_chat_usage(req_body, resp_text):
    # chars/4 fallback when the upstream sends no usage block
    try:
        prompt_chars = len(json.dumps(req_body.get("messages", [])))
    except (TypeError, ValueError):
        prompt_chars = 0
    return {
        "prompt_tokens": max(1, prompt_chars // 4),
        "completion_tokens": max(0, len(resp_text or "") // 4),
    }


def _estimate_embeddings_usage(req_body):
    try:
        text = json.dumps(req_body.get("input", ""))
    except (TypeError, ValueError):
        text = ""
    return {"prompt_tokens": max(1, len(text) // 4), "completion_tokens": 0}


class RelayState:
    def __init__(self, cfg, config_path, admin_token, auth_enabled):
        self.config_path = config_path
        self.admin_token = admin_token
        self.auth_enabled = auth_enabled
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.requests_total = 0
        self.cost_total = 0.0
        self.upstreams = []
        self.routes = []
        self.pricing = []
        self.by_name = {}
        self.load(cfg)

    def load(self, cfg):
        # rebuild upstream states from a fresh config
        with self.lock:
            self.upstreams = [upstream_mod.UpstreamState(u) for u in cfg["upstreams"]]
            self.routes = cfg["routes"]
            self.pricing = cfg.get("pricing", [])
            self.by_name = {u.name: u for u in self.upstreams}

    def reload(self):
        # re-read relay.json from disk without restarting
        if self.config_path:
            cfg = config_mod.load_config(self.config_path)
        else:
            cfg = config_mod.config_from_env()
        self.load(cfg)

    def ordered_upstreams(self, names):
        with self.lock:
            return [self.by_name[n] for n in names if n in self.by_name]

    def record_served(self, cost):
        with self.lock:
            self.requests_total += 1
            self.cost_total += cost


def make_handler(state, logger):
    class RelayHandler(BaseHTTPRequestHandler):
        server_version = "Relay/" + VERSION

        def log_message(self, *args):
            pass

        # small helpers

        def _send_json(self, status, obj, extra_headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _api_error(self, status, message):
            self._send_json(
                status,
                {"error": {"message": message, "type": "relay_error", "code": status}},
            )

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            return self.rfile.read(length) if length > 0 else b""

        def _auth(self):
            # returns (key record, None) or (None, (status, message))
            if not state.auth_enabled:
                return {"name": "none", "daily_cap": None}, None
            header = self.headers.get("Authorization", "")
            if not header.lower().startswith("bearer "):
                return None, (401, "missing or malformed authorization header")
            rec = keys.verify(header[7:].strip())
            if rec is None:
                return None, (401, "invalid api key")
            if keys.over_cap(rec):
                return None, (402, "daily spend cap exceeded for key '%s'" % rec["name"])
            return rec, None

        # routing

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/models":
                self._handle_models()
            elif path == "/relay/status":
                self._handle_status()
            elif path == "/relay/" or path == "/relay":
                self._send_html()
            else:
                self._api_error(404, "not found: %s" % path)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/chat/completions":
                self._handle_proxy(path, kind="chat")
            elif path == "/v1/embeddings":
                self._handle_proxy(path, kind="embeddings")
            elif path == "/relay/reload":
                self._handle_reload()
            else:
                self._api_error(404, "not found: %s" % path)

        def _send_html(self):
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_models(self):
            # aggregate configured model lists across upstreams, deduped
            seen = {}
            for up in state.ordered_upstreams([u.name for u in state.upstreams]):
                for m in up.models:
                    if m == "*" or m in seen:
                        continue
                    seen[m] = up.name
            data = [{"id": m, "object": "model", "owned_by": seen[m]} for m in seen]
            self._send_json(200, {"object": "list", "data": data})

        def _handle_status(self):
            upstreams = [u.snapshot() for u in state.ordered_upstreams([u.name for u in state.upstreams])]
            key_rows = []
            for k in keys.list_keys():
                key_rows.append(
                    {
                        "name": k["name"],
                        "spend_usd": round(keys.spend_today(k["name"]), 6),
                        "daily_cap": k["daily_cap"],
                    }
                )
            with state.lock:
                total = state.requests_total
                cost = state.cost_total
            self._send_json(
                200,
                {
                    "version": VERSION,
                    "uptime_s": int(time.time() - state.start_time),
                    "requests_total": total,
                    "cost_total_usd": round(cost, 6),
                    "upstreams": upstreams,
                    "keys_today": key_rows,
                },
            )

        def _handle_reload(self):
            # admin token required only when one is configured
            if state.admin_token:
                header = self.headers.get("Authorization", "")
                if header != "Bearer " + state.admin_token:
                    self._api_error(401, "invalid or missing admin token")
                    return
            try:
                state.reload()
            except config_mod.ConfigError as e:
                self._api_error(400, str(e))
                return
            self._send_json(200, {"ok": True})

        # proxy core

        def _handle_proxy(self, path, kind):
            raw = self._read_body()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._api_error(400, "request body must be json")
                return
            if not isinstance(body, dict):
                self._api_error(400, "request body must be a json object")
                return
            model = body.get("model", "")
            key_rec, err = self._auth()
            if err:
                status, message = err
                self._api_error(status, message)
                return
            names = router.resolve_route(state.routes, model)
            ordered = state.ordered_upstreams(names)
            if not ordered:
                self._api_error(400, "no route for model '%s'" % model)
                return
            # skip tripped breakers unless every upstream is tripped
            now = time.monotonic()
            candidates = [u for u in ordered if u.breaker_allows(now)] or ordered
            stream = kind == "chat" and bool(body.get("stream"))
            started = time.monotonic()
            fallbacks = 0
            last_error = "all upstreams failed"
            for up in candidates:
                req_start = time.monotonic()
                try:
                    status, rheaders, http_resp, conn = upstream_mod.post_raw(up, path, raw)
                except upstream_mod.UpstreamError as e:
                    up.record_failure()
                    last_error = str(e)
                    fallbacks += 1
                    _backoff()
                    continue
                if status in RETRYABLE_STATUSES:
                    try:
                        http_resp.read()
                    finally:
                        conn.close()
                    up.record_failure()
                    last_error = "upstream %s returned http %d" % (up.name, status)
                    fallbacks += 1
                    _backoff()
                    continue
                latency_ms = (time.monotonic() - req_start) * 1000
                if 200 <= status < 300:
                    up.record_success(latency_ms)
                    use_stream = stream
                else:
                    # 4xx from upstream is a client error: forward it, no retry
                    up.record_client_error(latency_ms)
                    use_stream = False
                try:
                    self._forward(http_resp, conn, up, status, rheaders, use_stream)
                except (BrokenPipeError, ConnectionResetError):
                    conn.close()
                    return
                usage = self._usage_for(kind, body, http_resp, use_stream)
                prompt_t = int(usage.get("prompt_tokens", 0))
                completion_t = int(usage.get("completion_tokens", 0))
                cost = router.cost_usd(state.pricing, model, prompt_t, completion_t)
                if state.auth_enabled and key_rec["name"] != "none":
                    keys.record_spend(key_rec["name"], cost)
                state.record_served(cost)
                logger.log(
                    {
                        "key": key_rec["name"],
                        "model": model,
                        "upstream": up.name,
                        "fallbacks": fallbacks,
                        "latency_ms": round((time.monotonic() - started) * 1000, 1),
                        "prompt_tokens": prompt_t,
                        "completion_tokens": completion_t,
                        "total_tokens": prompt_t + completion_t,
                        "cost_usd": round(cost, 6),
                        "status": status,
                    }
                )
                return
            self._api_error(502, last_error)

        def _forward(self, http_resp, conn, up, status, rheaders, stream):
            # store the accumulated body on the response for usage parsing
            if stream:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Relay-Upstream", up.name)
                self.end_headers()
                buf = bytearray()
                try:
                    while True:
                        chunk = http_resp.read(8192)
                        if not chunk:
                            break
                        buf += chunk
                        self.wfile.write(chunk)
                finally:
                    conn.close()
                http_resp.relay_body = bytes(buf)
                self.close_connection = True
            else:
                try:
                    data = http_resp.read()
                finally:
                    conn.close()
                http_resp.relay_body = data
                self.send_response(status)
                self.send_header(
                    "Content-Type", rheaders.get("content-type", "application/json")
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Relay-Upstream", up.name)
                self.end_headers()
                self.wfile.write(data)

        def _usage_for(self, kind, req_body, http_resp, stream):
            # prefer the upstream usage block, else estimate from chars/4
            body = getattr(http_resp, "relay_body", b"")
            if kind == "embeddings":
                usage = parse_json_usage(body)
                if usage:
                    return {
                        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                        "completion_tokens": 0,
                    }
                return _estimate_embeddings_usage(req_body)
            if stream:
                usage = parse_sse_usage(body)
                if usage:
                    return {
                        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                        "completion_tokens": int(usage.get("completion_tokens", 0)),
                    }
                return _estimate_chat_usage(req_body, body.decode("utf-8", "replace"))
            usage = parse_json_usage(body)
            if usage:
                return {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                }
            try:
                text = json.loads(body).get("choices", [{}])[0].get("message", {}).get("content", "")
            except (json.JSONDecodeError, TypeError, IndexError, AttributeError):
                text = ""
            return _estimate_chat_usage(req_body, text)

    return RelayHandler


def serve(cfg, config_path, port, auth_enabled, admin_token):
    state = RelayState(cfg, config_path, admin_token, auth_enabled)
    logger = log_mod.RequestLogger(cfg.get("log_file") or config_mod.default_log_path())
    handler = make_handler(state, logger)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, state
