#!/usr/bin/env python3
# relay: openai-compatible llm gateway. see README.md for the full docs.

import argparse
import os
import sys

import config as config_mod
import keys
import proxy

VERSION = "0.1.0"


def cmd_serve(args):
    try:
        config_path, cfg = config_mod.find_config(args.config)
    except config_mod.ConfigError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    port = args.port or cfg.get("port") or config_mod.DEFAULT_PORT
    admin_token = args.admin_token or os.environ.get("RELAY_ADMIN_TOKEN")
    auth_enabled = not args.no_auth
    server, _state = proxy.serve(cfg, config_path, port, auth_enabled, admin_token)
    print("relay %s listening on 127.0.0.1:%d" % (VERSION, port))
    print("config: %s" % (config_path or "(env default)"))
    print("auth: %s" % ("enabled" if auth_enabled else "disabled (--no-auth)"))
    print("dashboard: http://127.0.0.1:%d/relay/" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_keys_add(args):
    try:
        secret = keys.add_key(args.name, args.daily_cap)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    # printed once, never stored in plaintext
    print(secret)
    return 0


def cmd_keys_rm(args):
    if keys.remove_key(args.name):
        print("removed key '%s'" % args.name)
        return 0
    print("error: no such key '%s'" % args.name, file=sys.stderr)
    return 1


def cmd_keys_list(args):
    rows = keys.list_keys()
    if not rows:
        print("no keys")
        return 0
    print("%-20s %-12s %-10s %s" % ("name", "prefix", "daily cap", "created"))
    for r in rows:
        cap = "-" if r["daily_cap"] is None else "$%.2f" % r["daily_cap"]
        print("%-20s %-12s %-10s %s" % (r["name"], r["prefix"] + "...", cap, r["created"]))
    return 0


def cmd_check(args):
    try:
        config_path, cfg = config_mod.find_config(args.config)
    except config_mod.ConfigError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("config ok: %s" % (config_path or "(env default)"))
    print("%d upstream(s), %d route(s)" % (len(cfg["upstreams"]), len(cfg["routes"])))
    ok = True
    for name, passed, detail, ms in config_mod.check_upstreams(cfg):
        print("%-4s %-16s %s (%dms)" % ("PASS" if passed else "FAIL", name, detail, ms))
        ok = ok and passed
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(prog="relay", description="openai-compatible llm gateway")
    p.add_argument("--version", action="version", version="relay " + VERSION)
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the gateway")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--config", default=None, help="path to relay.json")
    s.add_argument("--no-auth", action="store_true", help="disable relay api key auth")
    s.add_argument("--admin-token", default=None, help="token for POST /relay/reload")
    s.set_defaults(func=cmd_serve)

    k = sub.add_parser("keys", help="manage relay api keys")
    ksub = k.add_subparsers(dest="keys_command")

    ka = ksub.add_parser("add", help="issue a new api key (printed once)")
    ka.add_argument("--name", required=True)
    ka.add_argument("--daily-cap", type=float, default=None, help="max usd spend per day")
    ka.set_defaults(func=cmd_keys_add)

    kr = ksub.add_parser("rm", help="revoke an api key")
    kr.add_argument("--name", required=True)
    kr.set_defaults(func=cmd_keys_rm)

    kl = ksub.add_parser("list", help="list key names and prefixes")
    kl.set_defaults(func=cmd_keys_list)

    c = sub.add_parser("check", help="validate config and ping upstreams")
    c.add_argument("--config", default=None, help="path to relay.json")
    c.set_defaults(func=cmd_check)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
