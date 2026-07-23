"""Estimated USD cost per model, based on Anthropic's public API price list.

This is ONLY an estimate applied to local token counts — it has nothing to do
with the actual Claude Code Pro subscription bill, which is a flat monthly fee.
Prices drift over time; verify against https://www.anthropic.com/pricing if the
numbers here look stale.

Cache write/read aren't published as flat rates — they're multiples of the base
input price (cache write ~1.25x, cache read ~0.1x), which is what's used below.
"""

from datetime import datetime, timezone

# (input $/MTok, output $/MTok)
_BASE_PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Claude Sonnet 5 has introductory pricing through 2026-08-31, then reverts to
# standard Sonnet pricing ($3.00 / $15.00) from 2026-09-01 onward.
_SONNET_5_INTRO_CUTOFF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _sonnet_5_pricing() -> tuple[float, float]:
    if datetime.now(timezone.utc) < _SONNET_5_INTRO_CUTOFF:
        return (2.00, 10.00)
    return (3.00, 15.00)


# Fallback used when a model id doesn't match anything known (keeps the widget
# from crashing or silently reporting $0 on a new/renamed model).
_DEFAULT_PRICING = (3.00, 15.00)

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def _base_pricing_for(model: str) -> tuple[float, float]:
    model = (model or "").lower()
    if "sonnet-5" in model:
        return _sonnet_5_pricing()
    for key, prices in _BASE_PRICING.items():
        if key in model:
            return prices
    return _DEFAULT_PRICING


def estimate_cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimated USD cost for one usage entry, at public API list price."""
    input_price, output_price = _base_pricing_for(model)
    cost = 0.0
    cost += (input_tokens / 1_000_000) * input_price
    cost += (output_tokens / 1_000_000) * output_price
    cost += (cache_creation_tokens / 1_000_000) * input_price * CACHE_WRITE_MULTIPLIER
    cost += (cache_read_tokens / 1_000_000) * input_price * CACHE_READ_MULTIPLIER
    return cost
