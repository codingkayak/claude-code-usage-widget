#!/usr/bin/env python
"""Windows tray widget for Claude Code Pro usage (5h + weekly quota, monthly USD estimate).

Threading model: Tkinter must own the main thread (the one hard requirement
across platforms), pystray's Icon.run() blocks in its own thread, and a
background worker thread does the periodic refresh. All three talk through
plain module-level state plus a Queue for the "open panel" signal, which is
enough at this scale without extra locking (GIL covers simple dict reads).
"""

import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
from winotify import Notification

import pricing
from i18n import t

OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # same public client_id Claude Code CLI itself uses

CLAUDE_HOME = Path.home() / ".claude"
CACHE_PATH = CLAUDE_HOME / "usage-cache.json"
CREDITS_CACHE_PATH = CLAUDE_HOME / "usage-credits-cache.json"
STATE_PATH = CLAUDE_HOME / "usage-widget-state.json"
CREDENTIALS_PATH = CLAUDE_HOME / ".credentials.json"
PROJECTS_DIR = CLAUDE_HOME / "projects"
LOCK_PATH = CLAUDE_HOME / "usage-widget.lock"
DEBUG_LOG_PATH = CLAUDE_HOME / "usage-widget-debug.log"

FAST_INTERVAL_SECONDS = 20
COST_RESCAN_EVERY_N_TICKS = 15  # ~5 min at a 20s tick
STALE_MINUTES_STATUSLINE = 20  # statusline is a free push during active sessions, trust it longer
STALE_MINUTES_FALLBACK = 2  # teste: precisa bater com NETWORK_POLL_INTERVAL_SECONDS abaixo
NOTIFY_THRESHOLDS = (80, 95)

GREEN = (34, 197, 94)
YELLOW = (234, 179, 8)
RED = (239, 68, 68)
GRAY = (110, 110, 110)
CLAUDE_ORANGE = (218, 119, 86)  # #DA7756, cor de marca do Claude
BG_DARK = (38, 38, 36)  # #262624, cinza-escuro quente do tema dark do Claude
CLAUDE_ORANGE_HEX = "#%02x%02x%02x" % CLAUDE_ORANGE
BG_DARK_HEX = "#%02x%02x%02x" % BG_DARK

_lock_handle = None
ui_queue: "queue.Queue[str]" = queue.Queue()
state_lock = threading.Lock()
latest = {
    "five_hour": {"used_percentage": None, "resets_at": None},
    "seven_day": {"used_percentage": None, "resets_at": None},
    "monthly_cost_usd": 0.0,  # CEC$ — estimativa a preço de API sobre os tokens locais
    "credits": {"enabled": False, "used_dollars": 0.0, "percent": None, "cap_dollars": None},  # CR$ — real, vem da conta
    "degraded": False,
    "auth_expired": False,
}
notified = {
    "five_hour": {"resets_at": None, "levels": set()},
    "seven_day": {"resets_at": None, "levels": set()},
}


def log_debug(message: str) -> None:
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Single instance guard
# ---------------------------------------------------------------------------

def acquire_single_instance_lock() -> None:
    global _lock_handle
    try:
        _lock_handle = open(LOCK_PATH, "x")
        return
    except FileExistsError:
        pass

    try:
        LOCK_PATH.unlink()
    except PermissionError:
        print(t("lock_already_running"))
        sys.exit(0)
    except Exception:
        pass

    try:
        _lock_handle = open(LOCK_PATH, "x")
    except Exception:
        print(t("lock_already_running"))
        sys.exit(0)


def release_single_instance_lock() -> None:
    global _lock_handle
    try:
        if _lock_handle:
            _lock_handle.close()
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rate limits: statusline cache, falling back to the undocumented usage API
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _is_stale(cache: dict) -> bool:
    written_at_iso = cache.get("written_at")
    if not written_at_iso:
        return True
    try:
        written_at = datetime.fromisoformat(written_at_iso.replace("Z", "+00:00"))
    except Exception:
        return True
    age_minutes = (datetime.now(timezone.utc) - written_at).total_seconds() / 60
    threshold = STALE_MINUTES_FALLBACK if cache.get("source") == "fallback-api" else STALE_MINUTES_STATUSLINE
    return age_minutes > threshold


def _extract_pct(window: dict) -> float | None:
    for key in ("used_percentage", "utilization", "percentage"):
        value = window.get(key)
        if isinstance(value, (int, float)):
            return value * 100 if value <= 1 else value
    return None


def _extract_resets_at(window: dict) -> str | None:
    for key in ("resets_at", "reset_at", "resetsAt"):
        if key in window:
            return window[key]
    return None


def _find_window(payload: dict, name: str) -> dict:
    candidates = [payload, payload.get("rate_limits") or {}, payload.get("data") or {}]
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get(name), dict):
            return candidate[name]
    return {}


# An earlier version let get_rate_limits() and get_credits() each independently
# decide "am I stale, retry the network" with no memory of failed attempts. Two
# pollers hitting the same endpoint every 20s tick, with no cooldown on failure,
# tripped Anthropic's own rate limit (HTTP 429). Fixed by making this ONE shared
# call, at most once every NETWORK_POLL_INTERVAL_SECONDS — success or failure,
# the next attempt always waits the full interval. Nice and calm.

NETWORK_POLL_INTERVAL_SECONDS = 2 * 60  # teste: ver se 2 min já é agressivo demais pro endpoint
_next_attempt_allowed_at = 0.0


def _refresh_oauth_token() -> bool:
    """Uses the refreshToken already in .credentials.json to get a fresh
    accessToken, the same OAuth refresh grant Claude Code itself uses. The
    server rotates the refresh token on every use (single-use), so the new
    tokens MUST be persisted immediately — leaving the old ones in the file
    would hand Claude Code's own next refresh attempt a dead refresh_token."""
    creds = _read_json(CREDENTIALS_PATH)
    oauth = (creds or {}).get("claudeAiOauth") or {}
    refresh_token = oauth.get("refreshToken")
    if not creds or not refresh_token:
        log_debug("token refresh: no refreshToken available in credentials file")
        return False

    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        log_debug(f"token refresh: request failed: {exc}")
        return False

    new_access_token = data.get("access_token")
    if not new_access_token:
        log_debug(f"token refresh: unexpected response shape: {data!r}"[:500])
        return False

    now_ms = int(time.time() * 1000)
    oauth["accessToken"] = new_access_token
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if isinstance(data.get("expires_in"), (int, float)):
        oauth["expiresAt"] = now_ms + int(data["expires_in"] * 1000)
    if isinstance(data.get("refresh_token_expires_in"), (int, float)):
        oauth["refreshTokenExpiresAt"] = now_ms + int(data["refresh_token_expires_in"] * 1000)
    if isinstance(data.get("scope"), str):
        oauth["scopes"] = data["scope"].split(" ")
    creds["claudeAiOauth"] = oauth

    try:
        backup_path = CREDENTIALS_PATH.parent / (CREDENTIALS_PATH.name + ".bak")
        backup_path.write_text(CREDENTIALS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        tmp_path = CREDENTIALS_PATH.parent / (CREDENTIALS_PATH.name + ".tmp")
        tmp_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        tmp_path.replace(CREDENTIALS_PATH)
    except Exception as exc:
        log_debug(f"token refresh: got new tokens but failed to write credentials file: {exc}")
        return False

    log_debug("token refresh: succeeded, credentials.json updated")
    return True


def _request_usage_payload() -> tuple[dict | None, int | None, bool]:
    """Raw GET to the undocumented usage endpoint. Same response backs both the
    five_hour/seven_day rate limits and the real 'spend' (usage credits) data —
    one network call serves both. Returns (payload_or_None, retry_after_seconds,
    auth_expired). retry_after_seconds is only set on a 429, straight from the
    server's own Retry-After header. auth_expired means a 401 — the accessToken
    in .credentials.json is stale; only using Claude Code itself refreshes it,
    we can't fix that from here, just report it clearly instead of retrying blind."""
    creds = _read_json(CREDENTIALS_PATH)
    token = ((creds or {}).get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        log_debug("usage endpoint: no OAuth token found in credentials file")
        return None, None, False

    try:
        response = requests.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=5,
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            retry_after_seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
            log_debug(f"usage endpoint: rate limited (429), Retry-After={retry_after_seconds}s")
            return None, retry_after_seconds, False
        if response.status_code == 401:
            log_debug("usage endpoint: 401 Unauthorized — access token expired, needs Claude Code to refresh it")
            return None, None, True
        response.raise_for_status()
        return response.json(), None, False
    except Exception as exc:
        log_debug(f"usage endpoint: request failed: {exc}")
        return None, None, False


def _parse_rate_limits(payload: dict) -> dict | None:
    five_hour = _find_window(payload, "five_hour")
    seven_day = _find_window(payload, "seven_day")
    if not five_hour and not seven_day:
        log_debug(f"usage endpoint: unrecognized rate-limit shape: {payload!r}"[:500])
        return None

    return {
        "source": "fallback-api",
        "written_at": datetime.now(timezone.utc).isoformat(),
        "five_hour": {
            "used_percentage": _extract_pct(five_hour),
            "resets_at": _extract_resets_at(five_hour),
        },
        "seven_day": {
            "used_percentage": _extract_pct(seven_day),
            "resets_at": _extract_resets_at(seven_day),
        },
    }


def _parse_credits(payload: dict) -> dict:
    spend = payload.get("spend") or {}
    used = spend.get("used") or {}
    amount_minor = used.get("amount_minor")
    exponent = used.get("exponent", 2)
    used_dollars = (amount_minor / (10 ** exponent)) if isinstance(amount_minor, (int, float)) else 0.0

    cap = spend.get("cap")
    cap_dollars = None
    if isinstance(cap, dict):
        cap_minor = cap.get("amount_minor")
        cap_exponent = cap.get("exponent", 2)
        if isinstance(cap_minor, (int, float)):
            cap_dollars = cap_minor / (10 ** cap_exponent)

    return {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(spend.get("enabled")),
        "used_dollars": used_dollars,
        "percent": spend.get("percent"),
        "cap_dollars": cap_dollars,
    }


def maybe_refresh_from_network() -> bool:
    """Called once per tick. Only actually hits the network when something is
    stale AND at least NETWORK_POLL_INTERVAL_SECONDS have passed since the last
    attempt — success or failure, always the same fixed cadence, never more.
    If the server hands back a Retry-After on a 429, that takes priority over
    our own default — no point waiting less than it explicitly asked for.
    Returns whether the access token looks expired (401), so the UI can say so
    instead of silently sitting on frozen numbers."""
    global _next_attempt_allowed_at

    rate_limits_cache = _read_json(CACHE_PATH)
    credits_cache = _read_json(CREDITS_CACHE_PATH)

    rate_limits_stale = not rate_limits_cache or _is_stale(rate_limits_cache)
    credits_stale = not credits_cache or _is_stale(
        {"written_at": (credits_cache or {}).get("written_at"), "source": "fallback-api"}
    )
    if not rate_limits_stale and not credits_stale:
        return False
    if time.time() < _next_attempt_allowed_at:
        return False

    payload, retry_after_seconds, auth_expired = _request_usage_payload()

    if auth_expired and _refresh_oauth_token():
        payload, retry_after_seconds, auth_expired = _request_usage_payload()

    wait_seconds = max(NETWORK_POLL_INTERVAL_SECONDS, retry_after_seconds or 0)
    _next_attempt_allowed_at = time.time() + wait_seconds
    if not payload:
        return auth_expired

    if rate_limits_stale:
        parsed = _parse_rate_limits(payload)
        if parsed:
            _write_json(CACHE_PATH, parsed)

    if credits_stale:
        _write_json(CREDITS_CACHE_PATH, _parse_credits(payload))

    return False


def get_rate_limits() -> dict:
    cache = _read_json(CACHE_PATH)
    return cache or {
        "five_hour": {"used_percentage": None, "resets_at": None},
        "seven_day": {"used_percentage": None, "resets_at": None},
    }


def get_credits() -> dict:
    cache = _read_json(CREDITS_CACHE_PATH)
    return cache or {"enabled": False, "used_dollars": 0.0, "percent": None, "cap_dollars": None}


# ---------------------------------------------------------------------------
# Monthly USD estimate: incremental scan of local session logs
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    return _read_json(STATE_PATH) or {"month": "", "files": {}}


def _save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def _cost_for_entry(entry: dict, month_start: datetime) -> float:
    message = entry.get("message") or {}
    usage = message.get("usage")
    if not usage:
        return 0.0

    timestamp = entry.get("timestamp")
    try:
        entry_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone()
    except Exception:
        return 0.0
    if entry_time < month_start:
        return 0.0

    return pricing.estimate_cost_usd(
        message.get("model", ""),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
    )


def _scan_file(path: Path, state: dict, month_start: datetime) -> None:
    key = str(path)
    file_state = state["files"].setdefault(key, {"offset": 0, "cost": 0.0})

    try:
        size = path.stat().st_size
        if size < file_state["offset"]:
            file_state["offset"] = 0
            file_state["cost"] = 0.0  # file was rotated/truncated since last scan

        with open(path, "rb") as f:
            f.seek(file_state["offset"])
            chunk = f.read()
    except Exception:
        return

    if not chunk:
        return

    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return  # no complete line appended yet, wait for the next tick

    file_state["offset"] += last_newline + 1
    for line in chunk[:last_newline].split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        file_state["cost"] += _cost_for_entry(entry, month_start)


def compute_monthly_cost_usd() -> float:
    now = datetime.now().astimezone()
    month_key = now.strftime("%Y-%m")
    state = _load_state()
    if state.get("month") != month_key:
        state = {"month": month_key, "files": {}}

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if PROJECTS_DIR.exists():
        for path in PROJECTS_DIR.rglob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                continue
            if mtime < month_start:
                continue  # nothing written to this file could be in the current month
            _scan_file(path, state, month_start)

    _save_state(state)
    return sum(f["cost"] for f in state["files"].values())


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_money(value: float) -> str:
    formatted = f"{value:,.2f}"
    return formatted.replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_countdown(resets_at_iso: str | None) -> str:
    if not resets_at_iso:
        return "—"
    try:
        resets_at = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    except Exception:
        return "—"
    delta = resets_at - datetime.now(timezone.utc)
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return t("countdown_now")
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


def lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def color_for_pct(pct: float | None) -> tuple:
    if pct is None:
        return GRAY
    pct = max(0.0, min(100.0, pct))
    if pct <= 50:
        return lerp_color(GREEN, YELLOW, pct / 50)
    return lerp_color(YELLOW, RED, (pct - 50) / 50)


# ---------------------------------------------------------------------------
# Tray icon (compact, no text — legibility lives in the detail panel)
# ---------------------------------------------------------------------------

def draw_icon(five_hour_pct: float | None, seven_day_pct: float | None) -> Image.Image:
    size = 80  # canvas grown to fit bars 30% wider/taller than the original 64px version
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    bar_width = 29  # 22 * 1.3
    gap = 6
    top, bottom = 6, size - 6  # bar height 68px, i.e. 52 * 1.3
    left_x0 = (size - (bar_width * 2 + gap)) // 2
    right_x0 = left_x0 + bar_width + gap

    for x0, pct in ((left_x0, five_hour_pct), (right_x0, seven_day_pct)):
        x1 = x0 + bar_width
        draw.rectangle([x0, top, x1, bottom], fill=BG_DARK, outline=CLAUDE_ORANGE, width=3)
        if pct:
            fill_height = int((bottom - top - 4) * (max(0.0, min(100.0, pct)) / 100))
            if fill_height > 0:
                draw.rectangle(
                    [x0 + 3, bottom - 2 - fill_height, x1 - 3, bottom - 2],
                    fill=color_for_pct(pct),
                )

    return image


def build_tooltip(data: dict) -> str:
    """Order requested: 5h, S (semanal), CEC$ (estimado), CR$ (real)."""
    five_hour = data["five_hour"]
    seven_day = data["seven_day"]
    credits = data["credits"]
    five_pct = five_hour.get("used_percentage")
    seven_pct = seven_day.get("used_percentage")
    five_text = f"{five_pct:.0f}%" if five_pct is not None else "—"
    seven_text = f"{seven_pct:.0f}%" if seven_pct is not None else "—"
    cr_text = format_money(credits["used_dollars"]) if credits.get("enabled") else f"0,00 ({t('tooltip_disabled')})"
    tooltip = (
        f"{t('five_hour_label')}: {five_text} (reset {format_countdown(five_hour.get('resets_at'))}) | "
        f"{t('weekly_label')}: {seven_text} (reset {format_countdown(seven_day.get('resets_at'))}) | "
        f"CEC$: {format_money(data['monthly_cost_usd'])} | "
        f"CR$: {cr_text}"
    )
    if data.get("auth_expired"):
        tooltip = t("tooltip_auth_expired") + " | " + tooltip
    return tooltip[:127]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    try:
        Notification(app_id="Claude Usage Widget", title=title, msg=message, duration="short").show()
    except Exception as exc:
        log_debug(f"notify failed: {exc}")


def check_thresholds(rate_limits: dict) -> None:
    labels = {"five_hour": t("notify_five_hour_label"), "seven_day": t("notify_weekly_label")}
    for key, label in labels.items():
        window = rate_limits.get(key) or {}
        pct = window.get("used_percentage")
        resets_at = window.get("resets_at")
        if pct is None:
            continue

        tracker = notified[key]
        if tracker["resets_at"] != resets_at:
            tracker["resets_at"] = resets_at
            tracker["levels"] = set()

        for threshold in NOTIFY_THRESHOLDS:
            if pct >= threshold and threshold not in tracker["levels"]:
                tracker["levels"].add(threshold)
                notify(f"Claude Code — {label}", t("notify_message", pct=pct))


# ---------------------------------------------------------------------------
# Background refresh loop
# ---------------------------------------------------------------------------

def refresh_loop(icon: Icon, stop_event: threading.Event) -> None:
    tick = 0
    while not stop_event.is_set():
        try:
            auth_expired = maybe_refresh_from_network()
            if auth_expired:
                with state_lock:
                    latest["auth_expired"] = True

            rate_limits = get_rate_limits()
            with state_lock:
                latest["five_hour"] = rate_limits.get("five_hour", latest["five_hour"])
                latest["seven_day"] = rate_limits.get("seven_day", latest["seven_day"])
                latest["degraded"] = not rate_limits.get("source") or _is_stale(rate_limits)
                if not auth_expired and not latest["degraded"]:
                    latest["auth_expired"] = False  # cleared the moment a fresh fetch actually succeeds

            credits = get_credits()
            with state_lock:
                latest["credits"] = credits

            if tick % COST_RESCAN_EVERY_N_TICKS == 0:
                monthly_cost = compute_monthly_cost_usd()
                with state_lock:
                    latest["monthly_cost_usd"] = monthly_cost

            with state_lock:
                snapshot = dict(latest)

            icon.icon = draw_icon(
                snapshot["five_hour"].get("used_percentage"),
                snapshot["seven_day"].get("used_percentage"),
            )
            icon.title = build_tooltip(snapshot)
            check_thresholds({"five_hour": snapshot["five_hour"], "seven_day": snapshot["seven_day"]})
            ui_queue.put("refresh_panel")
        except Exception as exc:
            log_debug(f"refresh_loop error: {exc}")

        tick += 1
        stop_event.wait(FAST_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Detail panel (Tkinter, main thread only)
# ---------------------------------------------------------------------------

class DetailPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None

    def toggle(self) -> None:
        if self.window and self.window.winfo_exists():
            self.close()
        else:
            self.open()

    def open(self) -> None:
        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=BG_DARK_HEX)

        width, height = 220, 375  # taller to fit bars 30% wider/taller + the CR$ line
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        self.window.geometry(f"{width}x{height}+{screen_w - width - 20}+{screen_h - height - 80}")

        self.canvas = tk.Canvas(self.window, width=width, height=height, bg=BG_DARK_HEX, highlightthickness=0)
        self.canvas.pack()
        self.window.bind("<FocusOut>", lambda _e: self.close())
        self.window.bind("<Button-1>", lambda _e: self.close())
        self.window.focus_force()
        self.render()

    def close(self) -> None:
        if self.window:
            self.window.destroy()
            self.window = None
            self.canvas = None

    def render(self) -> None:
        if not self.canvas or not self.window or not self.window.winfo_exists():
            return
        with state_lock:
            snapshot = dict(latest)
        self.canvas.delete("all")
        self._draw_bar(snapshot["five_hour"], x0=40, label=t("five_hour_label"))
        self._draw_bar(snapshot["seven_day"], x0=130, label=t("weekly_label"))
        credits = snapshot["credits"]
        cr_value = format_money(credits["used_dollars"]) if credits.get("enabled") else "0,00"

        self.canvas.create_text(
            110, 291,
            text=f"CEC$: {format_money(snapshot['monthly_cost_usd'])}",
            fill="white", font=("Segoe UI", 10),
        )
        self.canvas.create_text(
            110, 305,
            text=t("panel_cec_caption"),
            fill="#999999", font=("Segoe UI", 7),
        )
        self.canvas.create_text(
            110, 326,
            text=f"CR$: {cr_value}",
            fill="white", font=("Segoe UI", 10),
        )
        self.canvas.create_text(
            110, 340,
            text=t("panel_cr_caption_enabled") if credits.get("enabled") else t("panel_cr_caption_disabled"),
            fill="#999999", font=("Segoe UI", 7),
        )
        if snapshot.get("auth_expired"):
            self.canvas.create_text(110, 359, text=t("panel_auth_expired"), fill="#E5A500", font=("Segoe UI", 7, "bold"))
        elif snapshot.get("degraded"):
            self.canvas.create_text(110, 359, text=t("panel_degraded"), fill="#999999", font=("Segoe UI", 7))

    def _draw_bar(self, window: dict, x0: int, label: str) -> None:
        pct = window.get("used_percentage")
        bar_top, bar_bottom, bar_width = 20, 241, 52
        x1 = x0 + bar_width
        self.canvas.create_rectangle(x0, bar_top, x1, bar_bottom, outline=CLAUDE_ORANGE_HEX, width=3, fill=BG_DARK_HEX)
        if pct:
            fill_h = int((bar_bottom - bar_top - 4) * (max(0.0, min(100.0, pct)) / 100))
            color = "#%02x%02x%02x" % color_for_pct(pct)
            if fill_h > 0:
                self.canvas.create_rectangle(x0 + 3, bar_bottom - 2 - fill_h, x1 - 3, bar_bottom - 2, fill=color, outline="")
        pct_text = f"{pct:.0f}%" if pct is not None else "—"
        self.canvas.create_text((x0 + x1) / 2, bar_top - 10, text=pct_text, fill="white", font=("Segoe UI", 10, "bold"))
        self.canvas.create_text((x0 + x1) / 2, bar_bottom + 15, text=label, fill="white", font=("Segoe UI", 11, "bold"))
        self.canvas.create_text(
            (x0 + x1) / 2, bar_bottom + 32,
            text=f"reset {format_countdown(window.get('resets_at'))}",
            fill="#AAAAAA", font=("Segoe UI", 7),
        )


# ---------------------------------------------------------------------------
# Wiring: pystray menu + Tk main loop
# ---------------------------------------------------------------------------

def main() -> None:
    acquire_single_instance_lock()

    root = tk.Tk()
    root.withdraw()
    panel = DetailPanel(root)

    stop_event = threading.Event()

    def on_open_details() -> None:
        ui_queue.put("open_panel")

    def on_quit() -> None:
        stop_event.set()
        icon.stop()
        ui_queue.put("quit")

    menu = Menu(
        MenuItem(t("menu_details"), on_open_details, default=True),
        MenuItem(t("menu_quit"), on_quit),
    )
    icon = Icon("claude-usage-widget", draw_icon(None, None), t("icon_initial_title"), menu)

    worker = threading.Thread(target=refresh_loop, args=(icon, stop_event), daemon=True)
    worker.start()
    threading.Thread(target=icon.run, daemon=True).start()

    def poll_queue() -> None:
        try:
            while True:
                command = ui_queue.get_nowait()
                if command == "open_panel":
                    panel.toggle()
                elif command == "refresh_panel":
                    panel.render()
                elif command == "quit":
                    release_single_instance_lock()
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(200, poll_queue)

    root.after(200, poll_queue)
    root.mainloop()


if __name__ == "__main__":
    main()
