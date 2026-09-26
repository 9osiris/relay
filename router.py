#!/usr/bin/env python3
# relay router: match a requested model to an ordered upstream list,
# and look up pricing by model pattern. first match wins everywhere.


def match_pattern(pattern, model):
    # exact name, prefix "foo*", or catch-all "*"
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return model.startswith(pattern[:-1])
    return model == pattern


def resolve_route(routes, model):
    # first matching route wins, returns its try list in order
    for route in routes or []:
        if match_pattern(route.get("match", ""), model):
            return list(route.get("try", []))
    return []


def price_for(pricing, model):
    # first matching pricing entry wins, default is zero rates
    for entry in pricing or []:
        if match_pattern(entry.get("match", ""), model):
            return (float(entry.get("in_per_1m", 0.0)), float(entry.get("out_per_1m", 0.0)))
    return (0.0, 0.0)


def cost_usd(pricing, model, prompt_tokens, completion_tokens):
    # tokens are priced per million
    in_rate, out_rate = price_for(pricing, model)
    return prompt_tokens / 1_000_000 * in_rate + completion_tokens / 1_000_000 * out_rate
