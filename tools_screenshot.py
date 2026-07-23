"""Dev utility: renders a mockup of the tray icon and detail panel with sample
data, purely with PIL — no screen capture involved, so there's zero risk of
grabbing unrelated window content. Not part of the running widget itself."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import tray_widget as tw

DOCS_DIR = Path(__file__).parent / "docs"
DOCS_DIR.mkdir(exist_ok=True)

FONT_DIR = Path(r"C:\Windows\Fonts")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except Exception:
        return ImageFont.load_default()


def draw_panel_mockup(five_hour_pct, seven_day_pct, cec_dollars, cr_dollars, cr_enabled) -> Image.Image:
    width, height = 220, 375
    image = Image.new("RGB", (width, height), tw.BG_DARK)
    draw = ImageDraw.Draw(image)

    def centered_text(cx, y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (bbox[2] - bbox[0]) / 2, y), text, font=font, fill=fill)

    def bar(x0, pct, label, resets_text):
        bar_top, bar_bottom, bar_width = 20, 241, 52
        x1 = x0 + bar_width
        draw.rectangle([x0, bar_top, x1, bar_bottom], outline=tw.CLAUDE_ORANGE, width=3, fill=tw.BG_DARK)
        if pct:
            fill_h = int((bar_bottom - bar_top - 4) * (max(0.0, min(100.0, pct)) / 100))
            if fill_h > 0:
                draw.rectangle([x0 + 3, bar_bottom - 2 - fill_h, x1 - 3, bar_bottom - 2], fill=tw.color_for_pct(pct))
        cx = (x0 + x1) / 2
        centered_text(cx, bar_top - 24, f"{pct:.0f}%", _font(14, bold=True), "white")
        centered_text(cx, bar_bottom + 10, label, _font(15, bold=True), "white")
        centered_text(cx, bar_bottom + 30, f"reset {resets_text}", _font(11), "#AAAAAA")

    bar(40, five_hour_pct, t_five, "2h 13min")
    bar(130, seven_day_pct, t_weekly, "2d 22h")

    cr_text = tw.format_money(cr_dollars) if cr_enabled else "0,00"
    centered_text(110, 285, f"CEC$: {tw.format_money(cec_dollars)}", _font(14), "white")
    centered_text(110, 303, cec_caption, _font(11), "#999999")
    centered_text(110, 322, f"CR$: {cr_text}", _font(14), "white")
    centered_text(110, 340, cr_caption, _font(11), "#999999")

    return image


from i18n import STRINGS

en = STRINGS["en"]
t_five = en["five_hour_label"]
t_weekly = en["weekly_label"]
cec_caption = en["panel_cec_caption"]
cr_caption = en["panel_cr_caption_disabled"]

tw.draw_icon(42, 61).save(DOCS_DIR / "tray-icon.png")
draw_panel_mockup(42, 61, 18.42, 0.0, cr_enabled=False).save(DOCS_DIR / "detail-panel.png")

print("Saved:", DOCS_DIR / "tray-icon.png", "and", DOCS_DIR / "detail-panel.png")
