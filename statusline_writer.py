#!/usr/bin/env python
"""Registered as Claude Code's "statusLine" command.

Claude Code invokes this on every prompt render and passes a JSON blob on
stdin. Since v2.1.x, that blob includes rate_limits.five_hour and
.seven_day (used_percentage + resets_at) for Pro/Max plans. This script's
only job is to cache that data locally so tray_widget.py can read it
without ever calling the network itself.

Must never throw — a crash here would break Claude Code's own status line.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from i18n import t

CACHE_PATH = Path.home() / ".claude" / "usage-cache.json"


def _pct(window: dict) -> str:
    value = window.get("used_percentage")
    return f"{value:.0f}%" if isinstance(value, (int, float)) else "?"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    rate_limits = payload.get("rate_limits") or {}
    five_hour = rate_limits.get("five_hour") or {}
    seven_day = rate_limits.get("seven_day") or {}

    if five_hour or seven_day:
        cache = {
            "source": "statusline",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "five_hour": {
                "used_percentage": five_hour.get("used_percentage"),
                "resets_at": five_hour.get("resets_at"),
            },
            "seven_day": {
                "used_percentage": seven_day.get("used_percentage"),
                "resets_at": seven_day.get("resets_at"),
            },
        }
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass

    model_name = ((payload.get("model") or {}).get("display_name")) or ""
    status_line = f"{model_name}  {t('five_hour_label')}:{_pct(five_hour)}  {t('weekly_label')}:{_pct(seven_day)}".strip()
    print(status_line)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("")
