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


def pick_weighted(names, weights, current):
    # smooth weighted round robin pick, current is a mutable dict
    # weight 0 never wins while any weight is positive
    total = float(sum(weights))
    if total <= 0 or not names:
        return None
    best = None
    best_val = None
    for name, w in zip(names, weights):
        current[name] = current.get(name, 0.0) + float(w)
        if best_val is None or current[name] > best_val:
            best = name
            best_val = current[name]
    current[best] -= total
    return best


def weighted_order(names, weights, current):
    # primary picked by weight, the rest keep route order for fallback
    # equal weights keep route order so fallback stays predictable
    ordered = list(names)
    if len(ordered) <= 1:
        return ordered
    if all(float(w) == float(weights[0]) for w in weights):
        return ordered
    primary = pick_weighted(ordered, weights, current)
    if primary is None:
        return ordered
    return [primary] + [n for n in ordered if n != primary]


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
