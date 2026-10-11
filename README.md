# relay

One local endpoint in front of many model backends. relay is an
OpenAI-compatible LLM gateway: it serves `POST /v1/chat/completions`,
`POST /v1/embeddings`, and `GET /v1/models` on 127.0.0.1, then routes
each request to a configured upstream, with ordered fallbacks, circuit
breakers, per-key budgets, request logging, and a small dashboard.

Python, stdlib only. No frameworks, no dependencies, no external assets.

## quickstart

Point two fake upstreams at it. Any OpenAI-compatible server works;
`mockllm` (sibling project) is handy for this:

```bash
# terminal 1 and 2: two fake model servers
python3 mockllm.py --port 8318
python3 mockllm.py --port 8319
```

Write `relay.json`:

```json
{
  "upstreams": [
    {"name": "fast", "base_url": "http://127.0.0.1:8318/v1", "models": ["flash"], "timeout_s": 30},
    {"name": "backup", "base_url": "http://127.0.0.1:8319/v1", "models": ["flash"], "timeout_s": 30}
  ],
  "routes": [
    {"match": "*", "try": ["fast", "backup"]}
  ],
  "pricing": [
    {"match": "*", "in_per_1m": 0.10, "out_per_1m": 0.40}
  ]
}
```

Then:

```bash
python3 relay.py check --config relay.json
python3 relay.py serve --config relay.json --no-auth --port 8321
```

And use it like any OpenAI-compatible API:

```bash
curl http://127.0.0.1:8321/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "flash", "messages": [{"role": "user", "content": "hi"}]}'
```

The response carries an `X-Relay-Upstream` header naming the upstream
that served it. Streaming works: SSE bytes pass through untouched, and
relay captures token usage from the final usage chunk when the upstream
sends one (otherwise it estimates from chars/4).

With no `relay.json` and no `--config`, relay runs a single upstream
from env: `RELAY_UPSTREAM_URL` (default `http://127.0.0.1:8318/v1`) and
`RELAY_UPSTREAM_KEY`.

## relay.json

- `upstreams`: list of `name`, `base_url`, `api_key_env` (env var holding
  the key, optional), `models` (for `/v1/models` aggregation), `weight`
  (primary pick share, must be >= 0, default 1), `timeout_s` (per-request,
  must be > 0).
- `routes`: ordered list of `match` + `try`. Match is exact (`gpt-4o`),
  prefix (`gpt-*`), or catch-all (`*`). First matching route wins; its
  `try` list is the ordered fallback chain.
- `pricing`: list of `match` + `in_per_1m` / `out_per_1m` ($ per million
  tokens). First match wins. Cost = prompt/1M * in + completion/1M * out.
  Embeddings bill prompt tokens at the input rate.
- `log_file`: where request logs go (default `~/.relay/requests.jsonl`).
- `port`: default serve port (default 8321, `--port` overrides).

`relay check` validates the config and GETs each upstream's `/v1/models`,
printing PASS/FAIL per upstream.

## fallbacks and circuit breakers

When upstream weights differ, the primary is picked by smooth weighted
round robin, so weights 3 and 1 send about 3/4 of first tries to the
heavier upstream. The rest stay in route order as the fallback chain, and
a weight of 0 means fallback only. Equal weights keep plain route order.
On 429, 5xx, timeouts, or connection errors, relay records the failure,
waits a short jittered backoff, and tries the next upstream. Other 4xx
responses are forwarded to the client as-is with no retry.

Each upstream has a circuit breaker: 3 consecutive failures trip it, and
tripped upstreams are skipped while a healthy one exists. After 30s a
tripped breaker goes half-open and allows one probe; success closes it,
failure re-opens it. If every upstream is tripped, relay still tries
them rather than failing outright. `/relay/status` shows per-upstream
breaker state, rolling average latency, and request/error counts.

## keys and budgets

By default clients must authenticate with a relay-issued key:

```bash
python3 relay.py keys add --name laptop --daily-cap 5.00
# prints rl_... exactly once. save it; it is never shown again.
python3 relay.py keys list   # names and prefixes only, never full keys
python3 relay.py keys rm --name laptop
```

Keys are stored in `~/.relay/keys.json` as salted sha256 hashes, never
plaintext. Clients send `Authorization: Bearer rl_...`.

`--daily-cap` sets a max USD spend per day from the pricing table. Once
a key reaches its cap, requests get `402` with a JSON error until the
next day. Keys without a cap are unlimited. `--no-auth` on `serve`
disables auth entirely for local dev.

## logging

Every proxied request appends one JSON line with timestamp, key name,
requested model, upstream used, fallback count, latency ms,
prompt/completion/total tokens, estimated cost, and HTTP status. Logs
rotate by size: 5 files of 10MB each.

## admin api and dashboard

- `GET /relay/status` - JSON: version, uptime, totals, per-upstream
  health/breaker/latency/counts, today's per-key spend.
- `POST /relay/reload` - re-reads relay.json without restart. Requires
  `Authorization: Bearer <admin token>` when a token is configured via
  `RELAY_ADMIN_TOKEN` or `serve --admin-token`; open when none is set.
- `GET /relay/` - dashboard: upstream table with health dots, request
  and cost counters, auto-refreshes every 5 seconds. Single HTML file,
  no frameworks.

## cli

```
relay serve [--port 8321] [--config relay.json] [--no-auth] [--admin-token ...]
relay keys add --name NAME [--daily-cap 5.00]
relay keys rm --name NAME
relay keys list
relay check [--config relay.json]
relay --version
```

The server always binds 127.0.0.1.

## layout

- `relay.py` - CLI entry
- `proxy.py` - HTTP server, proxy loop, admin endpoints
- `config.py` - relay.json loading, validation, `check` pings
- `router.py` - model pattern matching, route resolution, pricing math
- `upstream.py` - upstream state, circuit breaker, raw HTTP forwarding
- `keys.py` - key issuance/verification, daily spend caps
- `log.py` - JSONL request log with rotation
- `dashboard.py` - the dashboard HTML
- `test_relay.py` - 96 checks, run with `python3 test_relay.py`

Tests run entirely against fake upstream servers on 127.0.0.1: route
matching, fallback on 500/429/connection-refused, streaming byte
passthrough with usage capture, breaker trip and half-open recovery,
key auth and hashed storage, daily-cap 402s, pricing math, log schema
and rotation, model aggregation, config validation, and admin reload.
No real API keys, no external network.
