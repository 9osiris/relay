#!/usr/bin/env python3
# relay upstream: per-upstream state, circuit breaker, raw http forwarding.

import http.client
import os
import threading
import time
import urllib.parse

FAILURES_TO_TRIP = 3
HALF_OPEN_AFTER_S = 30.0
LATENCY_WINDOW = 50


class UpstreamError(Exception):
    # transport-level failure or retryable http status
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class UpstreamState:
    def __init__(self, spec):
        self.name = spec["name"]
        self.base_url = spec["base_url"].rstrip("/")
        self.api_key_env = spec.get("api_key_env")
        self.models = list(spec.get("models", []))
        self.weight = spec.get("weight", 1)
        self.timeout_s = float(spec.get("timeout_s", 30))
        self.half_open_after = HALF_OPEN_AFTER_S
        self.lock = threading.RLock()  # reentrant: snapshot calls breaker_state
        self.consecutive_failures = 0
        self.total_requests = 0
        self.total_errors = 0
        self.latencies = []
        self.breaker_open = False
        self.breaker_opened_at = 0.0

    def api_key(self):
        # resolved per request so env changes apply without restart
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    def breaker_allows(self, now=None):
        # closed breakers always allow; open ones allow a probe after cooldown
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.breaker_open:
                return True
            return now - self.breaker_opened_at >= self.half_open_after

    def breaker_state(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.breaker_open:
                return "closed"
            if now - self.breaker_opened_at >= self.half_open_after:
                return "half-open"
            return "open"

    def record_success(self, latency_ms):
        # any success closes the breaker, including half-open probes
        with self.lock:
            self.consecutive_failures = 0
            self.breaker_open = False
            self.total_requests += 1
            self.latencies.append(latency_ms)
            if len(self.latencies) > LATENCY_WINDOW:
                self.latencies.pop(0)

    def record_client_error(self, latency_ms):
        # upstream answered 4xx: counts as a request, never trips the breaker
        with self.lock:
            self.total_requests += 1
            self.latencies.append(latency_ms)
            if len(self.latencies) > LATENCY_WINDOW:
                self.latencies.pop(0)

    def record_failure(self):
        with self.lock:
            self.consecutive_failures += 1
            self.total_requests += 1
            self.total_errors += 1
            if self.consecutive_failures >= FAILURES_TO_TRIP:
                self.breaker_open = True
                self.breaker_opened_at = time.monotonic()

    def avg_latency_ms(self):
        with self.lock:
            if not self.latencies:
                return 0.0
            return sum(self.latencies) / len(self.latencies)

    def snapshot(self):
        # plain dict for the status endpoint
        with self.lock:
            lat = list(self.latencies)
            return {
                "name": self.name,
                "base_url": self.base_url,
                "weight": self.weight,
                "models": list(self.models),
                "breaker": self.breaker_state(),
                "consecutive_failures": self.consecutive_failures,
                "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
                "requests": self.total_requests,
                "errors": self.total_errors,
            }


def post_raw(state, path, body, timeout=None):
    # returns (status, headers, response, conn); caller reads and closes conn.
    # raises UpstreamError on transport failure.
    parsed = urllib.parse.urlparse(state.base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = state.api_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    full_path = (parsed.path or "") + path
    try:
        conn = conn_cls(host, port, timeout=timeout if timeout is not None else state.timeout_s)
        conn.request("POST", full_path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        rheaders = {k.lower(): v for k, v in resp.getheaders()}
        return status, rheaders, resp, conn
    except (OSError, http.client.HTTPException, TimeoutError) as e:
        raise UpstreamError("%s: %s" % (type(e).__name__, e), retryable=True)
    except Exception as e:
        raise UpstreamError("%s: %s" % (type(e).__name__, e), retryable=True)
