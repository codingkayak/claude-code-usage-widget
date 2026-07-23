"""Minimal i18n: detect the OS UI language once at startup, default to English
for anything not explicitly supported. No live-switching — like most desktop
apps, changing the OS language takes effect on the next restart."""

import locale

try:
    import ctypes
except ImportError:
    ctypes = None

STRINGS = {
    "en": {
        "lock_already_running": "The widget is already running.",
        "menu_details": "Details",
        "menu_quit": "Quit",
        "icon_initial_title": "Claude Code — usage",
        "tooltip_disabled": "disabled",
        "tooltip_auth_expired": "Token expired, open Claude Code to renew",
        "five_hour_label": "5h",
        "weekly_label": "Wk",
        "panel_cec_caption": "Estimated usage cost (API pricing)",
        "panel_cr_caption_enabled": "Real cost (account usage credits)",
        "panel_cr_caption_disabled": "Real cost (usage credits — disabled)",
        "panel_degraded": "(data may be outdated)",
        "panel_auth_expired": "Token expired — open Claude Code to renew",
        "notify_five_hour_label": "5h session",
        "notify_weekly_label": "Weekly limit",
        "notify_message": "{pct:.0f}% of the limit already used",
        "countdown_now": "now",
    },
    "pt": {
        "lock_already_running": "Já existe uma instância do widget rodando.",
        "menu_details": "Detalhes",
        "menu_quit": "Sair",
        "icon_initial_title": "Claude Code — uso",
        "tooltip_disabled": "desativado",
        "tooltip_auth_expired": "Token expirado, abra o Claude Code p/ renovar",
        "five_hour_label": "5h",
        "weekly_label": "S",
        "panel_cec_caption": "Custo estimado de consumo (preço de API)",
        "panel_cr_caption_enabled": "Custo Real (créditos de uso da conta)",
        "panel_cr_caption_disabled": "Custo Real (créditos de uso — desativado)",
        "panel_degraded": "(dados podem estar desatualizados)",
        "panel_auth_expired": "Token expirado — abra o Claude Code p/ renovar",
        "notify_five_hour_label": "Sessão de 5h",
        "notify_weekly_label": "Limite semanal",
        "notify_message": "{pct:.0f}% do limite já consumido",
        "countdown_now": "agora",
    },
}


def detect_language() -> str:
    candidates = []
    try:
        if ctypes is not None and hasattr(ctypes, "windll"):
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            candidates.append(locale.windows_locale.get(lcid))
    except Exception:
        pass
    try:
        candidates.append(locale.getlocale()[0])
    except Exception:
        pass

    for candidate in candidates:
        if not candidate:
            continue
        lang = candidate.split("_")[0].lower()
        if lang in STRINGS:
            return lang
    return "en"


LANG = detect_language()


def t(key: str, **kwargs) -> str:
    text = STRINGS.get(LANG, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text
