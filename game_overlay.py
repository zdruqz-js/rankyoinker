"""Spiel-Overlay: kleine Fenster direkt ueber VALORANT (ab 2.5.5-dev2).

Der Dienst (vry_log_server.py, _ovl_start) startet es als eigenen Prozess, solange
VALORANT laeuft (und fuer die Vorschau in den Einstellungen):
    pythonw.exe game_overlay.py <cfg.json>
Eigener Prozess, damit ein Fehler hier nie den Dienst mitreisst. Nur Windows-Bordmittel
ueber ctypes (user32/gdi32/gdiplus) - das eingebettete Python hat kein tkinter.

Drei Fenster, alle oben (topmost), keins nimmt VALORANT den Fokus:
- Agentenauswahl: Raenge, Peak und Partys (oben rechts bzw. links)
- Spielertabelle: alle Spieler mit Rang, solange die Taste gehalten wird (Standard Tab,
  wie das Scoreboard im Spiel)
- Karte unten rechts: Ergebnis nach dem Match (Note, K/D/A, RR) und die Instalock-Frage
Die beiden Panels lassen Klicks durch (WS_EX_TRANSPARENT), nur die Karte ist klickbar.
Zu sehen nur, solange VALORANT vorne ist; im exklusiven Vollbild gar nicht.

Daten holt es alle 0,4-1,2 s beim Dienst (GET /api/ovl/state, Token aus der cfg),
Klicks gehen an POST /api/ovl/action. Gezeichnet wird mit GDI+ in eine 32-Bit-Bitmap
mit Alphakanal (UpdateLayeredWindow): weiche Schatten, runde Ecken, halbdurchsichtig.

Aufbau: oben die reinen Layout-Funktionen (layout_*). Sie liefern eine Liste von
Zeichenbefehlen (Ops), sind plattformunabhaengig und unter Linux testbar. Darunter
das Zeichnen mit GDI+ und die Fenster.
Testmodi fuer die CI (Windows-Runner): --render-test <state.json> <ordner> zeichnet
alle Panels als PNG, --smoke <state.json> <ordner> zeigt die echten Fenster und
macht ein Bildschirmfoto.
"""
import ctypes
import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.request
from ctypes import wintypes

# ============================ Aussehen ============================
# Wie rankyoinker.de: fast schwarze Flaechen mit goldenem und rotem Schimmer, rotes "Y"-Logo,
# goldene Mono-Ueberschriften mit leuchtendem Punkt und weitem Zeichenabstand, Pillen-Chips.

SH = 18            # Rand fuer den weichen Schatten rund um jedes Panel (logische Pixel)
PANEL_W = 424      # Agentenauswahl + Spielertabelle
CARD_W = 400       # Ergebnis / Instalock-Frage
PAD = 14
ROW_H = 42
ROW_GAP = 3
RANK_W = 150
FORM_W = 50        # "Letzte 5": Punktreihe + Bilanz
RADIUS = 18
GRADE_TILE = 56    # Agent + Notenkachel in der Ergebnis-Karte
GRADE_LIFT = 0.05  # Grossbuchstaben in GDI+ optisch mittig (Anteil der Schriftgroesse)
TRACK = 1.7        # Zeichenabstand der Mono-Ueberschriften (Website: .18em)

COL = {
    "bg": "#09090b", "tile": "#18181c", "white": "#ffffff", "black": "#000000",
    "text": "#f5f5f2", "text2": "#bcbcb5", "text3": "#8b8b84",     # heller als die Website - im Spiel lesbarer (2.5.6 noch etwas heller)
    "accent": "#d8b87c", "accent2": "#f3dfb0", "accent3": "#a9864c", "accent_ink": "#1b1408",
    "brand": "#ff4655", "brand_hi": "#ff6b78", "brand_lo": "#d42b3d", "teal": "#0ac8b9",
    "ally": "#5aa9ff", "enemy": "#ff4d5e", "win": "#3ddc84", "loss": "#ff6b7a", "draw": "#9a9a95",
}
BG_ALPHA = 248
TIER_COLORS = ((2, "#7a8590"), (5, "#9aa3ad"), (8, "#c99060"), (11, "#d3dbe3"), (14, "#f0cb52"),
               (17, "#46d0bd"), (20, "#c895f5"), (23, "#43e08c"), (26, "#ff5d7a"), (99, "#ffe58a"))
PARTY_COLORS = ("#2dd4bf", "#c084fc", "#f4b850", "#f472b6", "#a3e635", "#fb923c", "#38bdf8")
GRADE_COLORS = {"S": ("#ffe7a8", "#d8a84c"), "A": ("#3ddc84", "#3ddc84"), "B": ("#6fb6ff", "#6fb6ff"),
                "C": ("#a7a59d", "#a7a59d"), "D": ("#ff6b7a", "#ff6b7a")}
# Raenge bleiben Englisch (wie im Overlay, RANK_NAMES in index.html)
RANK_NAMES = ["Unranked", "Unranked", "Unranked", "Iron 1", "Iron 2", "Iron 3", "Bronze 1", "Bronze 2",
              "Bronze 3", "Silver 1", "Silver 2", "Silver 3", "Gold 1", "Gold 2", "Gold 3", "Platinum 1",
              "Platinum 2", "Platinum 3", "Diamond 1", "Diamond 2", "Diamond 3", "Ascendant 1", "Ascendant 2",
              "Ascendant 3", "Immortal 1", "Immortal 2", "Immortal 3", "Radiant"]
_RANK_SHORT = {"Platinum": "Plat", "Diamond": "Dia", "Ascendant": "Asc", "Immortal": "Imm"}

# Schriftrollen: (Schnitt, Groesse in logischen Pixeln) - Schnitte wie auf der Website:
# Inter fuer Text, Inter Tight fuer Namen/Raenge/Zahlen, Geist Mono fuer kleine Ueberschriften
FONTS = {
    "eyebrow": ("mono", 9.5), "label": ("mono", 8.5), "title": ("display", 16), "name": ("display", 13),
    "tag": ("regular", 11), "sub": ("regular", 11), "rank": ("display", 12.5), "chip": ("semibold", 10.5),
    "big": ("black", 28), "banner": ("black", 23), "grade": ("black", 34), "value": ("display", 15),
    "delta": ("semibold", 9.5), "body": ("regular", 12), "small": ("regular", 10.5), "btn": ("display", 13),
    "rr": ("black", 19), "mark": ("black", 13), "brand": ("display", 13.5), "vs": ("black", 11),
    # Bilanz unter "Letzte 5" (3W · 2L): bis 2.5.5 Geist Mono 8,5 px - bei 100 % kaum lesbar
    "form": ("semibold", 9.5),
    # Urteil unter dem VS-Balken ("Deutlicher Vorteil ..."): halbfett wie .tverdict im Overlay (bis 2.5.6-dev1 "small")
    "verdict": ("semibold", 11.5),
}
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
# Laufweite wie auf der Website (letter-spacing in em, negativ = enger). GDI+ kennt weder Laufweite
# noch das Kerning der Schriften (das steht nur in GPOS) - der Text wirkte darum lockerer und unruhiger
# als im Browser. Seit 2.5.6-dev1 setzt Gdi die Zeichen mit den mitgelieferten Schriften selbst
# (Breiten + Kerning aus fonts/metrics.json, erzeugt mit setup/make_overlay_metrics.py).
LETTER_SPACING = {"banner": -0.035, "big": -0.03, "grade": -0.02, "rr": -0.025, "mark": -0.02, "title": -0.025,
                  "name": -0.02, "rank": -0.015, "value": -0.02, "btn": -0.01, "brand": -0.03, "chip": -0.005}
METRICS_FILE = os.path.join(FONT_DIR, "metrics.json")
# Logo (seit 2.5.5-dev5, erzeugt mit setup/make_logo_assets.py)
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo-mark.png")

# Tasten fuer die Spielertabelle (virtuelle Tastencodes)
KEYS = {"tab": 0x09, "capslock": 0x14, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "x1": 0x05, "x2": 0x06}
SIZES = {"s": 0.9, "m": 1.0, "l": 1.15}

RESULT_SHOW = 45.0     # so lange bleibt die Ergebnis-Karte sichtbar (Sekunden, nur sichtbare Zeit)
# Die Tab-Tabelle wird unsichtbar vorgezeichnet, sobald sich ihre Daten aendern (hoechstens alle
# BOARD_PREPAINT Sekunden) - Tab blendet dann nur noch ein. Bis 2.5.6 wurde genau beim Druecken
# gezeichnet (beim ersten Mal pro Match inklusive Bilder laden), das kostete im Spiel kurz Bilder/s.
BOARD_PREPAINT = 2.0

FALLBACK = {
    "ovl_eyebrow_pregame": "AGENT SELECT", "ovl_eyebrow_board": "LOBBY", "ovl_eyebrow_result": "MATCH RESULT",
    "ovl_team_ally": "YOUR TEAM", "ovl_team_enemy": "ENEMIES", "ovl_team_all": "PLAYERS",
    "ovl_open_report": "Open analysis", "ovl_rank_up": "Ranked up!", "ovl_rank_down": "Ranked down",
    "ovl_il_in": "{name} in {n} s", "ovl_il_locked": "{name} locked", "ovl_hidden": "Hidden",
    "ovl_avg": "Avg {rank}", "ovl_preview": "PREVIEW", "ovl_grade": "Grade", "ovl_today": "Today {rr} · {w}–{l}",
    "report_victory": "Victory", "report_defeat": "Defeat", "report_draw": "Draw", "report_place": "Place {n} of {of}",
    "report_mvp": "Match MVP", "report_team_mvp": "Team MVP", "pregame_you": "You", "pregame_hovering": "Hovering",
    "instalock_ask_title": "Keep going with {name}?",
    "instalock_ask_body": "Should Instalock lock {name} in the next match too? No answer pauses it.",
    "instalock_ask_skip": "Don't ask for the next {n} matches", "instalock_ask_yes": "Yes", "instalock_ask_no": "No",
    "peak_label": "Peak", "verdict_even": "Even", "verdict_slight_edge": "Slight edge",
    "verdict_clear_edge": "Clear edge", "verdict_strong_edge": "Strong edge",
    "for_your_team": "for your team", "for_the_enemies": "for the enemies",
}


def tr(strings, key, **kw):
    s = (strings or {}).get(key) or FALLBACK.get(key) or key
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def upper(s, lang):
    """Grossbuchstaben - im Tuerkischen wird aus i ein I mit Punkt."""
    if lang == "tr":
        s = s.replace("i", "İ")
    return s.upper()


def rank_name(tier):
    try:
        t = int(tier or 0)
    except (TypeError, ValueError):
        t = 0
    return RANK_NAMES[t] if 0 <= t < len(RANK_NAMES) else "Unranked"


def rank_short(tier):
    name = rank_name(tier)
    word, _, num = name.partition(" ")
    return (_RANK_SHORT.get(word, word) + (" " + num if num else "")).strip()


def tier_color(tier):
    try:
        t = int(tier or 0)
    except (TypeError, ValueError):
        t = 0
    for limit, color in TIER_COLORS:
        if t <= limit:
            return color
    return TIER_COLORS[-1][1]


def party_color(n):
    return PARTY_COLORS[(int(n) - 1) % len(PARTY_COLORS)] if n and int(n) > 0 else None


def avg_tier(players):
    tiers = [int(p.get("tier") or 0) for p in players if int(p.get("tier") or 0) > 2]
    return int(round(sum(tiers) / len(tiers))) if tiers else 0


def verdict(ally, enemy, strings):
    """Teamvergleich wie teamCompareHtml() im Overlay: Abstand in Rangstufen -> Text, Lage des
    Balkens (8..92 %) und Richtung (up/down/even)."""
    diff = ((ally.get("tier") or 0) * 100 + (ally.get("rr") or 0)
            - (enemy.get("tier") or 0) * 100 - (enemy.get("rr") or 0)) / 100.0
    a = abs(diff)
    if a < 0.25:
        return tr(strings, "verdict_even"), 50.0, "even"
    word = "verdict_slight_edge" if a < 1 else "verdict_clear_edge" if a < 2 else "verdict_strong_edge"
    text = tr(strings, word) + " " + tr(strings, "for_your_team" if diff > 0 else "for_the_enemies")
    return text, max(8.0, min(92.0, 50 + diff * 14)), ("up" if diff > 0 else "down")


def split_riot_id(name):
    s = str(name or "")
    i = s.rfind("#")
    if i > 0:
        return s[:i], s[i:]
    return s, ""


def fit_join(m, items, font, width, sep="  ·  "):
    """So viele Angaben wie ganz hineinpassen (keine halben Werte mit "..."), in dieser Reihenfolge."""
    out = []
    for it in items:
        if not it:
            continue
        if m(sep.join(out + [it]), font) <= width:
            out.append(it)
        elif not out:
            out.append(it)          # die erste darf notfalls gekuerzt werden
            break
        else:
            break
    return sep.join(out)


def tracked_width(m, s, font, track=TRACK):
    return sum(m(ch, font) for ch in s) + track * max(0, len(s) - 1)


# ============================ Zeichenbefehle ============================

class Ops:
    """Zeichenliste eines Panels in logischen Pixeln (96 DPI), inklusive Schattenrand."""

    def __init__(self, w=0, h=0):
        self.w, self.h = w, h
        self.items = []
        self.hits = []          # (name, x, y, w, h) - klickbare Flaechen

    def add(self, *item):
        self.items.append(item)

    def hit(self, name, x, y, w, h):
        self.hits.append((name, x, y, w, h))

    def hit_test(self, lx, ly):
        for name, x, y, w, h in self.hits:
            if x <= lx < x + w and y <= ly < y + h:
                return name
        return None


def _frame_light(ops, x, y, w, h):
    """Lichtverlauf + goldener und roter Schimmer (wie .glow/.card auf der Website)."""
    ops.add("vgrad", x, y, w, h, RADIUS, COL["white"], 11, COL["white"], 3)
    ops.add("clip", x, y, w, h, RADIUS)
    ops.add("glow", x + w * 0.18, y - 120, w * 0.95, 230, COL["accent"], 44)
    ops.add("glow", x - w * 0.42, y - 150, w * 0.95, 300, COL["brand"], 34)
    ops.add("unclip")


def _frame(ops, x, y, w, h):
    """Panel-Grund wie die Karten der Website: Schatten, fast schwarz, Schimmer, feiner Rand, Lichtkante."""
    ops.add("shadow", x, y, w, h, RADIUS, SH - 2, 130)
    ops.add("fill", x, y, w, h, RADIUS, COL["bg"], BG_ALPHA)
    _frame_light(ops, x, y, w, h)
    ops.add("stroke", x, y, w, h, RADIUS, COL["white"], 30, 1.0)
    ops.add("line", x + RADIUS, y + 0.6, x + w - RADIUS, y + 0.6, COL["white"], 40, 1.0)


def _brand(ops, x, y):
    """Logo wie oben links auf der Website (rotes "R" mit Kunai) + Schriftzug."""
    ops.add("glow", x - 9, y - 6, 40, 36, COL["brand"], 80)
    ops.add("fill", x, y, 22, 22, 6, "#0b0b0d", 255)
    ops.add("image", x, y, 22, 22, LOGO, 6, 255, False)
    ops.add("stroke", x, y, 22, 22, 6, COL["white"], 34, 1.0)
    ops.add("text", x + 30, y, 130, 22, "RankYoinker", "brand", COL["text"], 255, "l", False)


def _eyebrow(ops, m, x, y, text, color=None, dot=True, align="l", alpha=255):
    """Goldene Mono-Ueberschrift mit leuchtendem Punkt (.eyebrow der Website). Gibt die Breite zurueck."""
    color = color or COL["accent"]
    tw = tracked_width(m, text, "eyebrow")
    total = tw + (12 if dot else 0)
    if align == "r":
        x -= total
    if dot:
        ops.add("glow", x - 5, y + 2, 16, 16, color, 120)
        ops.add("fill", x, y + 7, 6, 6, 3, color, 255)
        x += 12
    ops.add("ttext", x, y, 20, text, "eyebrow", color, alpha, TRACK)
    return total


def _pill(ops, m, x_right, y, text, dot_color=None):
    """Chip wie "v2.5.4 | Fuer VALORANT ..." oben auf der Website, rechtsbuendig."""
    tw = tracked_width(m, text, "eyebrow", 1.2)
    w = tw + 22 + (12 if dot_color else 0)
    x = x_right - w
    ops.add("fill", x, y, w, 22, 11, COL["white"], 12)
    ops.add("stroke", x, y, w, 22, 11, COL["white"], 28, 1.0)
    tx = x + 11
    if dot_color:
        ops.add("glow", tx - 5, y + 3, 16, 16, dot_color, 130)
        ops.add("fill", tx, y + 8, 6, 6, 3, dot_color, 255)
        tx += 12
    ops.add("ttext", tx, y + 1, 20, text, "eyebrow", COL["text2"], 255, 1.2)
    return w


def _chip(ops, m, x, y, text, color, align_right=False, fill_alpha=34, h=20):
    w = m(text, "chip") + 16
    if align_right:
        x -= w
    ops.add("fill", x, y, w, h, h / 2.0, color, fill_alpha)
    ops.add("stroke", x, y, w, h, h / 2.0, color, 90, 1.0)
    ops.add("text", x, y, w, h, text, "chip", color, 255, "c", False)
    return w


def _header(ops, m, ctx, x, y, w, label, dot_color=None):
    """Logo + Chip rechts; ohne label kein Chip (ausser in der Vorschau)."""
    _brand(ops, x, y)
    if ctx.get("preview"):
        pv = tr(ctx["strings"], "ovl_preview")
        label = pv + " · " + label if label else pv
    if label:
        _pill(ops, m, x + w, y, label, dot_color)
    return y + 22 + 12


def _banner(ops, m, ctx, x, y, w, h, img, title, sub, right=None):
    """Kartenbild als Kopf (wie der Match-Kopf im Overlay): grosser Kartenname, darunter Mono."""
    lang = ctx.get("lang")
    ops.add("clip", x, y, w, h, 12)
    ops.add("fill", x, y, w, h, 0, COL["tile"], 255)
    if img:
        ops.add("image", x, y, w, h, img, 0, 255, True)
    ops.add("hgrad", x, y, w, h, 0, COL["bg"], 236, COL["bg"], 60)
    ops.add("vgrad", x, y + h * 0.45, w, h * 0.55, 0, COL["bg"], 0, COL["bg"], 150)
    ops.add("unclip")
    ops.add("stroke", x, y, w, h, 12, COL["white"], 26, 1.0)
    rw = 0
    if right:
        rw = _chip(ops, m, x + w - 12, y + (h - 22) / 2.0, right, COL["accent"], align_right=True, h=22) + 10
    ops.add("text", x + 14, y + 8, w - 28 - rw, 30, upper(title or "—", lang), "banner", COL["text"], 255, "l", False)
    if sub:
        ops.add("ttext", x + 15, y + h - 23, 18, upper(sub, lang), "eyebrow", COL["text2"], 255, TRACK)


def _compare(ops, m, ctx, x, y, w, cmp):
    """Teamvergleich wie im Overlay: Durchschnittsrang beider Teams, VS-Balken, Urteil."""
    strings = ctx["strings"]
    a, e = (cmp or {}).get("ally"), (cmp or {}).get("enemy")
    if not a or not e:
        return y
    h = 66
    ops.add("fill", x, y, w, h, 12, COL["white"], 8)
    ops.add("stroke", x, y, w, h, 12, COL["white"], 20, 1.0)
    side_w = (w - 120) / 2.0
    for side, data, label, col in (("l", a, tr(strings, "ovl_team_ally"), COL["ally"]),
                                   ("r", e, tr(strings, "ovl_team_enemy"), COL["enemy"])):
        tier = int(data.get("tier") or 0)
        if side == "l":
            ix, tx, al = x + 10, x + 44, "l"
        else:
            ix, tx, al = x + w - 10 - 28, x + w - 44 - (side_w - 44), "r"
        if data.get("img"):
            ops.add("image", ix, y + 10, 28, 28, data["img"], 0, 255, False)
        tw = side_w - 44
        if al == "l":
            ops.add("ttext", tx, y + 7, 16, label, "label", col, 255, 1.2)
        else:
            lw = tracked_width(m, label, "label", 1.2)
            ops.add("ttext", tx + tw - lw, y + 7, 16, label, "label", col, 255, 1.2)
        ops.add("text", tx, y + 22, tw, 20, rank_name(tier), "rank", tier_color(tier), 255, al, False)
    text, pct, kind = verdict(a, e, strings)
    mx, mw = x + side_w + 6, 108
    ops.add("text", mx, y + 6, mw, 14, "VS", "vs", COL["text3"], 255, "c", False)
    ops.add("fill", mx + 4, y + 26, mw - 8, 5, 2.5, COL["enemy"], 150)
    ops.add("fill", mx + 4, y + 26, (mw - 8) * pct / 100.0, 5, 2.5, COL["ally"], 230)
    ops.add("fill", mx + 4 + (mw - 8) / 2.0 - 0.5, y + 24, 1, 9, 0, COL["white"], 140)
    vcol = {"up": COL["win"], "down": COL["loss"]}.get(kind, COL["text2"])
    ops.add("line", x + 14, y + 48, x + w - 14, y + 48, COL["white"], 16, 1.0)
    ops.add("text", x + 10, y + 49, w - 20, 16, text, "verdict", vcol, 255, "c", False)
    return y + h + 10


def _name_tag(ops, m, x, y, w, h, name, tag, col_name, extra_w=0):
    tw = m(tag, "tag") if tag else 0
    full = m(name, "name") + 1
    nw = min(full, max(24, w - tw - 4 - extra_w))
    if nw < full:
        # selbst kuerzen, damit der Tag direkt hinter dem "…" steht (sonst blieb je nach Buchstabe eine Luecke)
        s = name
        while len(s) > 1 and m(s.rstrip() + "\u2026", "name") + 1 > nw:
            s = s[:-1]
        name = s.rstrip() + "\u2026"
        nw = min(nw, m(name, "name") + 1)
    ops.add("text", x, y, nw + 2, h, name, "name", col_name, 255, "l", False)
    end = x + nw
    if tag and nw + 4 + tw + extra_w <= w + 1:
        ops.add("text", x + nw + 4, y + 1, tw + 2, h, tag, "tag", COL["text3"], 255, "l", False)
        end = x + nw + 4 + tw
    return end


def form_label(form):
    """Bilanz wie im Overlay unter "Letzte 5": "3W · 2L" (Unentschieden nur, wenn es eins gab)."""
    form = form or []
    parts = ["%dW" % form.count("w"), "%dL" % form.count("l")]
    if form.count("d"):
        parts.append("%dD" % form.count("d"))
    return " · ".join(parts)


def _form(ops, m, x, y, w, form):
    """"Letzte 5" wie in der App: leuchtende Punkte (gruen Sieg, rot Niederlage, grau Unentschieden),
    darunter die Bilanz."""
    form = (form or [])[:5]
    d, gap = 7.0, 2.4
    total = len(form) * d + (len(form) - 1) * gap
    cx = x + (w - total) / 2.0
    for i, r in enumerate(form):
        col = {"w": COL["win"], "l": COL["loss"]}.get(r, COL["draw"])
        px = cx + i * (d + gap)
        ops.add("glow", px - 4, y - 4, d + 8, d + 8, col, 70)
        ops.add("fill", px, y, d, d, d / 2.0, col, 240)
        ops.add("fill", px + 1.6, y + 1.2, 2.2, 2.2, 1.1, COL["white"], 90)      # Lichtpunkt
    label = form_label(form)
    ops.add("text", x, y + 10, w, 14, label, "form", COL["text3"], 255, "c", False)


def _player_row(ops, m, ctx, x, y, w, p, pregame):
    strings = ctx["strings"]
    me = bool(p.get("me"))
    if me:
        ops.add("hgrad", x, y, w, ROW_H, 11, COL["accent"], 46, COL["accent"], 14)
        ops.add("stroke", x, y, w, ROW_H, 11, COL["accent"], 80, 1.0)
    else:
        ops.add("fill", x, y, w, ROW_H, 11, COL["white"], 8)
    pc = party_color(p.get("party"))
    if pc:
        ops.add("fill", x + 4, y + 9, 3, ROW_H - 18, 1.5, pc, 255)
    # Agent
    tx, ty, ts = x + 12, y + 5, 32
    ops.add("fill", tx, ty, ts, ts, 9, COL["tile"], 255)
    hovering = pregame and p.get("hover") and not p.get("locked")
    if p.get("agentImg"):
        ops.add("image", tx, ty, ts, ts, p["agentImg"], 9, 110 if hovering else 255, False)
    else:
        ops.add("text", tx, ty, ts, ts, "?", "name", COL["text3"], 255, "c", False)
    if pregame and p.get("locked"):
        ops.add("stroke", tx, ty, ts, ts, 9, COL["accent"], 150, 1.2)
    # Name (+ Begegnungen)
    nx = tx + ts + 10
    form = p.get("form") or []
    nw = x + w - RANK_W - (FORM_W if form else 0) - nx - 6
    name, tag = split_riot_id(p.get("name"))
    enc = int(p.get("enc") or 0)
    enc_txt = "%d×" % enc if enc >= 2 else ""
    enc_w = (m(enc_txt, "chip") + 12) if enc_txt else 0
    if p.get("hidden") or not name:
        end = _name_tag(ops, m, nx, y + 4, nw, 19, p.get("agent") or tr(strings, "ovl_hidden"), "", COL["text2"], enc_w)
    else:
        end = _name_tag(ops, m, nx, y + 4, nw, 19, name, tag, COL["text"], enc_w)
    if enc_txt and end + 6 + enc_w <= nx + nw + 1:
        ops.add("fill", end + 6, y + 7, enc_w, 15, 7.5, COL["accent"], 30)
        ops.add("text", end + 6, y + 7, enc_w, 15, enc_txt, "delta", COL["accent"], 255, "c", False)
    sub = []
    if me:
        sub.append(tr(strings, "pregame_you"))
    if hovering:
        sub.append(tr(strings, "pregame_hovering"))
    if isinstance(p.get("wr"), (int, float)):
        sub.append("WR %d %%" % round(p["wr"]))
    if not pregame:
        if isinstance(p.get("kd"), (int, float)):
            sub.append("K/D %.2f" % p["kd"])
        if isinstance(p.get("hs"), (int, float)):
            sub.append("HS %d %%" % round(p["hs"]))
    if p.get("level"):
        sub.append("Lvl %s" % p["level"])
    ops.add("text", nx, y + 22, nw, 16, fit_join(m, sub, "sub", nw), "sub",
            COL["accent"] if me else COL["text3"], 255, "l", False)
    # Letzte 5
    if form:
        _form(ops, m, x + w - RANK_W - FORM_W - 4, y + 10, FORM_W, form)
    # Rang
    rx = x + w - RANK_W
    tier = int(p.get("tier") or 0)
    ranked = tier > 2
    if p.get("rankImg") and ranked:
        ops.add("glow", rx - 7, y + 1, 40, 40, tier_color(tier), 60)
        ops.add("image", rx, y + 8, 26, 26, p["rankImg"], 0, 255, False)
    else:
        ops.add("fill", rx + 5, y + 13, 16, 16, 8, COL["white"], 18)
    ops.add("text", rx + 32, y + 4, RANK_W - 36, 19, rank_name(tier) if ranked else "Unranked", "rank",
            tier_color(tier) if ranked else COL["text3"], 255, "l", False)
    low = []
    if ranked and p.get("rr") is not None:
        low.append("%s RR" % p["rr"])
    peak = int(p.get("peak") or 0)
    if peak > 2 and (peak > tier or not ranked):
        low.append("%s %s" % (tr(strings, "peak_label"), rank_short(peak)))
    ops.add("text", rx + 32, y + 22, RANK_W - 36, 16, fit_join(m, low, "sub", RANK_W - 36), "sub",
            COL["text3"], 255, "l", False)


def _section(ops, m, ctx, x, y, w, label, color, players, pregame, show_avg=True):
    lw = _eyebrow(ops, m, x + 2, y, label, color)
    at = avg_tier(players) if show_avg else 0
    right = tr(ctx["strings"], "ovl_avg", rank=rank_name(at)) if at else ""
    rw = m(right, "small") + 10 if right else 0
    if right:
        ops.add("text", x, y, w, 20, right, "small", COL["text3"], 255, "r", False)
    if x + w - rw - (x + lw + 14) > 20:       # Linie bis zum Durchschnitt (wie die Website-Ueberschriften)
        ops.add("hgrad", x + lw + 14, y + 10, w - rw - lw - 14, 1, 0, color, 70, color, 0)
    y += 24
    for p in players:
        _player_row(ops, m, ctx, x, y, w, p, pregame)
        y += ROW_H + ROW_GAP
    return y


def _panel(m, ctx, data, pregame):
    strings, lang = ctx["strings"], ctx.get("lang")
    ops, inner = Ops(), Ops()
    x0, y0, w = SH, SH, PANEL_W
    x, y = x0 + PAD, y0 + PAD
    iw = w - 2 * PAD
    if pregame:
        y = _header(inner, m, ctx, x, y, iw, tr(strings, "ovl_eyebrow_pregame"), COL["accent"])
    else:
        y = _header(inner, m, ctx, x, y, iw, None)
    sub = [s for s in (data.get("mode"), data.get("sideText")) if s]
    il = (data.get("il") or {}).get("text") if pregame else None
    _banner(inner, m, ctx, x, y, iw, 60, data.get("mapImg"), data.get("map") or "", " · ".join(sub), il)
    y += 60 + 10
    if not data.get("ffa"):
        y = _compare(inner, m, ctx, x, y, iw, data.get("cmp"))
    groups = []
    if data.get("ffa"):
        groups.append((tr(strings, "ovl_team_all"), COL["text2"], data.get("allies") or []))
    else:
        if data.get("allies"):
            groups.append((tr(strings, "ovl_team_ally"), COL["ally"], data["allies"]))
        if data.get("enemies"):
            groups.append((tr(strings, "ovl_team_enemy"), COL["enemy"], data["enemies"]))
    for i, (label, color, players) in enumerate(groups):
        if i:
            y += 8
        y = _section(inner, m, ctx, x, y, iw, label, color, players[:14], pregame, show_avg=not data.get("cmp"))
    h = (y - y0) + PAD - ROW_GAP
    _frame(ops, x0, y0, w, h)
    ops.items.extend(inner.items)
    ops.w, ops.h = w + 2 * SH, h + 2 * SH
    return ops


def layout_pregame(m, ctx, data):
    return _panel(m, ctx, data, True)


def layout_board(m, ctx, data):
    return _panel(m, ctx, data, False)


def _close_btn(ops, x, y, hover):
    ops.add("fill", x, y, 26, 26, 13, COL["black"] if not hover else COL["white"], 120 if not hover else 30)
    ops.add("stroke", x, y, 26, 26, 13, COL["white"], 30, 1.0)
    c = COL["text"] if hover else COL["text2"]
    ops.add("line", x + 9, y + 9, x + 17, y + 17, c, 255, 1.6)
    ops.add("line", x + 17, y + 9, x + 9, y + 17, c, 255, 1.6)
    ops.hit("close", x, y, 26, 26)


def _button(ops, name, x, y, w, h, text, primary, hover):
    if primary:
        ops.add("glow", x + w * 0.1, y + h * 0.3, w * 0.8, h * 1.1, COL["accent"], 70 if hover else 45)
        ops.add("dgrad", x, y, w, h, 11, COL["accent2"] if hover else "#ecd5a2", 255, COL["accent3"], 255)
        ops.add("line", x + 10, y + 0.8, x + w - 10, y + 0.8, COL["white"], 110, 1.0)
        ops.add("text", x, y, w, h, text, "btn", COL["accent_ink"], 255, "c", False)
    else:
        ops.add("fill", x, y, w, h, 11, COL["white"], 22 if hover else 12)
        ops.add("stroke", x, y, w, h, 11, COL["white"], 46 if hover else 28, 1.0)
        ops.add("text", x, y, w, h, text, "btn", COL["text"], 255, "c", False)
    ops.hit(name, x, y, w, h)


def _stat_tiles(ops, m, x, y, w, stats):
    gap = 8
    n = len(stats)
    sw = (w - gap * (n - 1)) / float(n)
    for i, st in enumerate(stats):
        sx = x + i * (sw + gap)
        ops.add("vgrad", sx, y, sw, 50, 11, COL["white"], 16, COL["white"], 6)
        ops.add("stroke", sx, y, sw, 50, 11, COL["white"], 18, 1.0)
        ops.add("text", sx, y + 7, sw, 20, str(st.get("value") or "—"), "value", COL["text"], 255, "c", False)
        label = st.get("label") or ""
        d = st.get("delta")
        lw = tracked_width(m, label, "label", 1.0)
        dw = (m(d, "delta") + 5) if d else 0
        lx = sx + (sw - lw - dw) / 2.0
        ops.add("ttext", lx, y + 30, 14, label, "label", COL["text3"], 255, 1.0)
        if d:
            ops.add("text", lx + lw + 5, y + 29, dw + 2, 15, d, "delta",
                    COL["win"] if st.get("up") else COL["loss"], 255, "l", False)


def layout_card(m, ctx, res, ask, ui):
    """Karte unten rechts: Ergebnis (res) und/oder Instalock-Frage (ask).
    ui: {"hover": Name, "skip": Haken gesetzt?, "frac": Restzeit 0..1 oder None}"""
    strings, lang = ctx["strings"], ctx.get("lang")
    hv = (ui or {}).get("hover")
    ops, inner = Ops(), Ops()
    x0, y0, w = SH, SH, CARD_W
    P = 16
    y = y0
    light_at = None
    if res:
        bh = 134
        if res.get("mapImg"):
            inner.add("clip", x0, y0, w, bh, RADIUS)
            inner.add("fill", x0, y0, w, bh, 0, COL["bg"], 255)
            inner.add("image", x0, y0, w, bh, res["mapImg"], 0, 255, True)
            inner.add("hgrad", x0, y0, w, bh, 0, COL["bg"], 240, COL["bg"], 90)
            inner.add("vgrad", x0, y0 + 30, w, bh - 30, 0, COL["bg"], 0, COL["bg"], 255)
            inner.add("unclip")
            # gleiche Lichtebenen wie im Panel darunter -> keine Kante (eingefuegt, wenn die Hoehe feststeht)
            light_at = len(inner.items)
        _brand(inner, x0 + P, y0 + 14)
        _close_btn(inner, x0 + w - 12 - 26, y0 + 12, hv == "close")
        kind = res.get("result") or "draw"
        tcol = {"win": COL["win"], "loss": COL["loss"]}.get(kind, COL["draw"])
        eyebrow = tr(strings, "ovl_eyebrow_result")
        if res.get("mode"):
            eyebrow += " · " + upper(str(res["mode"]), lang)
        if ctx.get("preview"):
            eyebrow = tr(strings, "ovl_preview") + " · " + eyebrow
        _eyebrow(inner, m, x0 + P, y0 + 46, eyebrow, COL["accent"])
        title = upper(tr(strings, {"win": "report_victory", "loss": "report_defeat"}.get(kind, "report_draw")), lang)
        score = res.get("score") or {}
        ty = y0 + 66
        if score.get("place"):
            inner.add("text", x0 + P, ty, w - 2 * P, 36, title, "big", tcol, 255, "l", False)
        else:
            sc = "%s : %s" % (score.get("ally", "?"), score.get("enemy", "?"))
            sw = m(sc, "big") + 4
            inner.add("glow", x0 + P - 20, ty - 4, m(title, "big") + 40, 44, tcol, 40)
            inner.add("text", x0 + P, ty, w - 2 * P - sw - 8, 36, title, "big", tcol, 255, "l", False)
            inner.add("text", x0 + w - P - sw, ty, sw, 36, sc, "big", COL["text"], 255, "r", False)
        info = [res.get("map") or ""]
        if score.get("place"):
            info.append(tr(strings, "report_place", n=score["place"], of=score.get("of") or "?"))
        elif res.get("place"):
            info.append(tr(strings, "report_place", n=res["place"], of=res.get("of") or "?"))
        inner.add("text", x0 + P, y0 + 104, w - 2 * P, 18, "  ·  ".join(i for i in info if i), "body",
                  COL["text2"], 255, "l", False)
        y = y0 + bh + 6
        # Agent, Note, Leistung
        T = GRADE_TILE
        inner.add("fill", x0 + P, y, T, T, 14, COL["tile"], 255)
        if res.get("agentImg"):
            inner.add("image", x0 + P, y, T, T, res["agentImg"], 14, 255, False)
        inner.add("stroke", x0 + P, y, T, T, 14, COL["white"], 24, 1.0)
        g = res.get("grade") or "-"
        c1, c2 = GRADE_COLORS.get(g, ("#3a3a40", "#3a3a40"))
        gx = x0 + P + T + 10
        inner.add("glow", gx - 12, y - 8, T + 24, T + 16, c2, 75)
        inner.add("vgrad", gx, y, T, T, 14, c1, 255, c2, 255)
        inner.add("line", gx + 12, y + 0.8, gx + T - 12, y + 0.8, COL["white"], 120, 1.0)
        # GDI+ mittelt die ganze Zeilenhoehe (mit Unterlaenge) - Grossbuchstaben saessen sonst
        # sichtbar zu tief (im Windows-Test 2,3 px); GRADE_LIFT gleicht das aus
        inner.add("text", gx, y - FONTS["grade"][1] * GRADE_LIFT, T, T, g, "grade", "#0b0b0c", 255, "c", False)
        tx = gx + T + 12
        tw = x0 + w - P - tx
        head = tr(strings, "ovl_grade") + " " + g
        hw = min(m(head, "title") + 2, tw)
        inner.add("text", tx, y + 6, hw, 22, head, "title", COL["text"], 255, "l", False)
        if res.get("mvp"):
            label = tr(strings, "report_team_mvp" if res["mvp"] == "team" else "report_mvp")
            if hw + 8 + m(label, "chip") + 16 <= tw:
                _chip(inner, m, tx + hw + 8, y + 7, label, COL["accent"])
        inner.add("text", tx, y + 31, tw, 18, res.get("perf") or "", "body", COL["text2"], 255, "l", False)
        y += T + 12
        # Highlights
        hls = res.get("highlights") or []
        if hls:
            hx = x0 + P
            for txt in hls[:4]:
                cw = m(txt, "chip") + 16
                if hx + cw > x0 + w - P:
                    break
                hx += _chip(inner, m, hx, y, txt, COL["accent"], fill_alpha=26) + 6
            y += 30
        # Werte (mit Abstand zum eigenen Schnitt)
        stats = res.get("stats") or []
        if stats:
            _stat_tiles(inner, m, x0 + P, y, w - 2 * P, stats)
            y += 58
        if res.get("line"):
            inner.add("text", x0 + P, y - 2, w - 2 * P, 16, res["line"], "small", COL["text3"], 255, "c", False)
            y += 20
        # RR
        rr = res.get("rr")
        if rr:
            inner.add("vgrad", x0 + P, y, w - 2 * P, 56, 12, COL["white"], 14, COL["white"], 5)
            inner.add("stroke", x0 + P, y, w - 2 * P, 56, 12, COL["white"], 20, 1.0)
            tier = int(rr.get("tier") or 0)
            if rr.get("img"):
                inner.add("image", x0 + P + 10, y + 11, 34, 34, rr["img"], 0, 255, False)
            lx = x0 + P + 54
            lw = w - 2 * P - 54 - 112
            inner.add("text", lx, y + 6, lw, 18, rank_name(tier), "rank", tier_color(tier), 255, "l", False)
            if isinstance(rr.get("rr"), int) and tier > 2:
                frac = max(0.0, min(1.0, rr["rr"] / 100.0))
                inner.add("fill", lx, y + 27, lw, 4, 2, COL["white"], 22)
                inner.add("fill", lx, y + 27, max(4.0, lw * frac), 4, 2, tier_color(tier), 255)
                inner.add("text", lx, y + 34, lw, 16, "%d / 100 RR" % rr["rr"], "small", COL["text3"], 255, "l", False)
            before = int(rr.get("before") or 0)
            note, ncol = "", COL["text3"]
            if before and tier > before:
                note, ncol = tr(strings, "ovl_rank_up"), COL["accent"]
            elif before and tier < before:
                note, ncol = tr(strings, "ovl_rank_down"), COL["loss"]
            earned = rr.get("earned")
            if earned is not None:
                txt = ("+%d RR" % earned) if earned > 0 else ("−%d RR" % abs(earned)) if earned < 0 else "±0 RR"
                col = COL["win"] if earned > 0 else COL["loss"] if earned < 0 else COL["text2"]
                inner.add("text", x0 + w - P - 112, y + 6, 100, 26, txt, "rr", col, 255, "r", False)
            today = rr.get("today")
            if not note and today and today.get("n"):
                t_rr = today.get("rr") or 0
                rr_txt = ("+%d RR" % t_rr) if t_rr > 0 else ("−%d RR" % abs(t_rr)) if t_rr < 0 else "±0 RR"
                note = tr(strings, "ovl_today", rr=rr_txt, w=today.get("w", 0), l=today.get("l", 0))
            if note:
                inner.add("text", x0 + w - P - 160, y + 33, 148, 16, note, "small", ncol, 255, "r", False)
            y += 64
        _button(inner, "open", x0 + P, y, w - 2 * P, 36, tr(strings, "ovl_open_report") + "  \u2192", False, hv == "open")
        y += 36 + P
    if ask:
        if res:
            inner.add("hgrad", x0 + P, y, (w - 2 * P) / 2.0, 1, 0, COL["white"], 0, COL["white"], 30)
            inner.add("hgrad", x0 + w / 2.0, y, (w - 2 * P) / 2.0, 1, 0, COL["white"], 30, COL["white"], 0)
            y += 14
        else:
            y += P
            _brand(inner, x0 + P, y)
            _close_btn(inner, x0 + w - 12 - 26, y0 + 12, hv == "close")
            y += 34
        _eyebrow(inner, m, x0 + P, y, "INSTALOCK", COL["accent"])
        y += 22
        inner.add("fill", x0 + P, y, 46, 46, 12, COL["tile"], 255)
        if ask.get("img"):
            inner.add("image", x0 + P, y, 46, 46, ask["img"], 12, 255, False)
        inner.add("stroke", x0 + P, y, 46, 46, 12, COL["accent"], 90, 1.0)
        tx = x0 + P + 58
        tw = x0 + w - P - tx
        name = ask.get("name") or "Agent"
        inner.add("text", tx, y - 1, tw, 22, tr(strings, "instalock_ask_title", name=name), "title", COL["text"], 255, "l", False)
        inner.add("text", tx, y + 22, tw, 32, tr(strings, "instalock_ask_body", name=name), "body", COL["text2"], 255, "l", True)
        y += 60
        on = bool((ui or {}).get("skip"))
        inner.hit("skip", x0 + P - 4, y, w - 2 * P + 8, 26)
        bx, by = x0 + P, y + 5
        if on:
            inner.add("dgrad", bx, by, 16, 16, 4.5, COL["accent2"], 255, COL["accent3"], 255)
            inner.add("line", bx + 4, by + 8.5, bx + 7, by + 11.5, COL["accent_ink"], 255, 2.0)
            inner.add("line", bx + 7, by + 11.5, bx + 12.5, by + 5, COL["accent_ink"], 255, 2.0)
        else:
            inner.add("stroke", bx, by, 16, 16, 4.5, COL["white"], 120 if hv == "skip" else 70, 1.3)
        inner.add("text", x0 + P + 26, y, w - 2 * P - 26, 26,
                  tr(strings, "instalock_ask_skip", n=ask.get("skipN") or 3), "body",
                  COL["text"] if hv == "skip" else COL["text2"], 255, "l", False)
        y += 34
        bw = (w - 2 * P - 10) / 2.0
        _button(inner, "no", x0 + P, y, bw, 38, tr(strings, "instalock_ask_no"), False, hv == "no")
        _button(inner, "yes", x0 + P + bw + 10, y, bw, 38, tr(strings, "instalock_ask_yes"), True, hv == "yes")
        y += 38 + P
    h = y - y0
    frac = (ui or {}).get("frac")
    if light_at is not None:
        light = Ops()
        light.add("clip", x0, y0, w, 134, 0)
        _frame_light(light, x0, y0, w, h)
        light.add("unclip")
        inner.items[light_at:light_at] = light.items
    _frame(ops, x0, y0, w, h)
    ops.items.extend(inner.items)
    if frac is not None and res and not ask:
        ops.add("fill", x0 + RADIUS, y0 + h - 3, (w - 2 * RADIUS) * max(0.0, min(1.0, frac)), 2, 1, COL["accent"], 190)
    ops.hits = inner.hits
    ops.w, ops.h = w + 2 * SH, h + 2 * SH
    return ops


def card_signature(res, ask, ui):
    return json.dumps([res, ask, (ui or {}).get("hover"), bool((ui or {}).get("skip")),
                       round((ui or {}).get("frac") or 0, 2) if (ui or {}).get("frac") is not None else None],
                      sort_keys=True, default=str)


# ============================ Windows: GDI+ ============================

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint), ("pt", _POINT)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", ctypes.c_void_p),
                ("dwHoverTime", wintypes.DWORD)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _RECTF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float), ("Width", ctypes.c_float), ("Height", ctypes.c_float)]


class _POINTF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]


class _COLORMATRIX(ctypes.Structure):
    _fields_ = [("m", (ctypes.c_float * 5) * 5)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


PARGB = 0x000E200B         # PixelFormat32bppPARGB
RGB32 = 0x00022009         # PixelFormat32bppRGB
_PNG_CLSID = (0x557CF406, 0x1A04, 0x11D3, (0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))

# Schriftfamilien je Schnitt: (Name, Stil, aus fonts/?) - zuerst die mitgelieferten Schriften
# der Website (fonts/, erzeugt mit setup/make_overlay_fonts.py), sonst Windows-Schriften.
# Stil 1 = fett, falls es den Schnitt nicht gibt.
_FAMILIES = {
    # seit 2.5.6 kraeftiger als die Website (siehe setup/make_overlay_fonts.py): "regular" = Inter 560
    "regular": (("RY Inter Medium", 0, True), ("Segoe UI Semibold", 0, False), ("Segoe UI", 0, False)),
    "semibold": (("RY Inter SemiBold", 0, True), ("Segoe UI Semibold", 0, False), ("Segoe UI", 1, False)),
    "display": (("RY Inter Tight Display", 0, True), ("Segoe UI", 1, False)),
    "black": (("RY Inter Tight Black", 0, True), ("Segoe UI Black", 0, False), ("Segoe UI", 1, False)),
    "mono": (("RY Geist Mono SemiBold", 0, True), ("Cascadia Mono", 0, False), ("Consolas", 0, False)),
}
_SCRIPT_FAMILIES = {"ko": ("Malgun Gothic", "Gulim"), "ja": ("Yu Gothic UI", "Meiryo UI", "MS UI Gothic"),
                    "zh": ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun"), "th": ("Leelawadee UI", "Tahoma")}


def script_of(text, lang=None):
    """Schrift fuer Namen/Texte in anderen Schriftsystemen - GDI+ sucht sich keine
    Ersatzschrift, ohne das stuenden dort Kaestchen."""
    for ch in text or "":
        o = ord(ch)
        if o < 0x0E00:
            continue
        if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            return "ko"
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
            return "ja"
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            return "ja" if lang == "ja" else ("ko" if lang == "ko" else "zh")
        if 0x0E00 <= o <= 0x0E7F:
            return "th"
    return None


def _argb(color, alpha=255):
    v = int(str(color).lstrip("#"), 16)
    return ctypes.c_uint32((max(0, min(255, int(alpha))) << 24) | v)


def _prototypes(user32, gdi32, kernel32, gdip):
    """Explizite argtypes/restype: Handles sind auf x64 64-bit."""
    vp, ui, i, f = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_float
    sz, ssz, dw = ctypes.c_size_t, ctypes.c_ssize_t, wintypes.DWORD
    P = ctypes.POINTER
    kernel32.GetModuleHandleW.restype, kernel32.GetModuleHandleW.argtypes = vp, [ctypes.c_wchar_p]
    kernel32.OpenProcess.restype, kernel32.OpenProcess.argtypes = vp, [dw, wintypes.BOOL, dw]
    kernel32.CloseHandle.argtypes = [vp]
    kernel32.WaitForSingleObject.restype, kernel32.WaitForSingleObject.argtypes = dw, [vp, dw]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [vp, dw, ctypes.c_wchar_p, P(dw)]
    user32.RegisterClassW.restype, user32.RegisterClassW.argtypes = wintypes.ATOM, [vp]
    user32.CreateWindowExW.restype = vp
    user32.CreateWindowExW.argtypes = [dw, ctypes.c_wchar_p, ctypes.c_wchar_p, dw, i, i, i, i, vp, vp, vp, vp]
    user32.DefWindowProcW.restype, user32.DefWindowProcW.argtypes = ssz, [vp, ui, sz, ssz]
    user32.DestroyWindow.argtypes = [vp]
    user32.ShowWindow.argtypes = [vp, i]
    user32.GetMessageW.restype, user32.GetMessageW.argtypes = wintypes.BOOL, [P(_MSG), vp, ui, ui]
    user32.TranslateMessage.argtypes = [P(_MSG)]
    user32.DispatchMessageW.restype, user32.DispatchMessageW.argtypes = ssz, [P(_MSG)]
    user32.PostMessageW.restype, user32.PostMessageW.argtypes = wintypes.BOOL, [vp, ui, sz, ssz]
    user32.PostQuitMessage.argtypes = [i]
    user32.SetTimer.restype, user32.SetTimer.argtypes = sz, [vp, sz, ui, vp]
    user32.SetWindowPos.restype, user32.SetWindowPos.argtypes = wintypes.BOOL, [vp, vp, i, i, i, i, ui]
    user32.GetForegroundWindow.restype = vp
    user32.GetWindowThreadProcessId.restype, user32.GetWindowThreadProcessId.argtypes = dw, [vp, P(dw)]
    user32.MonitorFromWindow.restype, user32.MonitorFromWindow.argtypes = vp, [vp, dw]
    user32.GetMonitorInfoW.argtypes = [vp, P(_MONITORINFO)]
    user32.LoadCursorW.restype, user32.LoadCursorW.argtypes = vp, [vp, vp]
    user32.SetCursor.restype, user32.SetCursor.argtypes = vp, [vp]
    user32.TrackMouseEvent.argtypes = [P(_TRACKMOUSEEVENT)]
    user32.GetAsyncKeyState.restype, user32.GetAsyncKeyState.argtypes = ctypes.c_short, [i]
    user32.GetDC.restype, user32.GetDC.argtypes = vp, [vp]
    user32.ReleaseDC.argtypes = [vp, vp]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.UpdateLayeredWindow.argtypes = [vp, vp, P(_POINT), P(_SIZE), vp, P(_POINT), dw, P(_BLENDFUNCTION), dw]
    user32.IsIconic.argtypes = [vp]
    user32.GetSystemMetrics.argtypes = [i]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = vp, [vp]
    gdi32.CreateDIBSection.restype = vp
    gdi32.CreateDIBSection.argtypes = [vp, P(_BITMAPINFO), ui, P(vp), vp, dw]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = vp, [vp, vp]
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.BitBlt.argtypes = [vp, i, i, i, i, vp, i, i, dw]
    for name, args in (
            ("GdiplusStartup", [P(sz), P(_GdiplusStartupInput), vp]),
            ("GdipCreateBitmapFromScan0", [i, i, i, i, vp, P(vp)]),
            ("GdipGetImageGraphicsContext", [vp, P(vp)]),
            ("GdipDeleteGraphics", [vp]),
            ("GdipDisposeImage", [vp]),
            ("GdipGraphicsClear", [vp, ctypes.c_uint32]),
            ("GdipSetSmoothingMode", [vp, i]),
            ("GdipSetPixelOffsetMode", [vp, i]),
            ("GdipSetInterpolationMode", [vp, i]),
            ("GdipSetTextRenderingHint", [vp, i]),
            ("GdipSetTextContrast", [vp, ui]),
            ("GdipSetCompositingQuality", [vp, i]),
            ("GdipScaleWorldTransform", [vp, f, f, i]),
            ("GdipTranslateWorldTransform", [vp, f, f, i]),
            ("GdipResetWorldTransform", [vp]),
            ("GdipSaveGraphics", [vp, P(ui)]),
            ("GdipRestoreGraphics", [vp, ui]),
            ("GdipCreateSolidFill", [ctypes.c_uint32, P(vp)]),
            ("GdipCreateLineBrush", [P(_POINTF), P(_POINTF), ctypes.c_uint32, ctypes.c_uint32, i, P(vp)]),
            ("GdipDeleteBrush", [vp]),
            ("GdipCreatePen1", [ctypes.c_uint32, f, i, P(vp)]),
            ("GdipSetPenStartCap", [vp, i]),
            ("GdipSetPenEndCap", [vp, i]),
            ("GdipDeletePen", [vp]),
            ("GdipCreatePath", [i, P(vp)]),
            ("GdipAddPathArc", [vp, f, f, f, f, f, f]),
            ("GdipAddPathRectangle", [vp, f, f, f, f]),
            ("GdipAddPathEllipse", [vp, f, f, f, f]),
            ("GdipCreatePathGradientFromPath", [vp, P(vp)]),
            ("GdipSetPathGradientCenterColor", [vp, ctypes.c_uint32]),
            ("GdipSetPathGradientSurroundColorsWithCount", [vp, P(ctypes.c_uint32), P(i)]),
            ("GdipClosePathFigure", [vp]),
            ("GdipDeletePath", [vp]),
            ("GdipFillPath", [vp, vp, vp]),
            ("GdipDrawPath", [vp, vp, vp]),
            ("GdipDrawLine", [vp, vp, f, f, f, f]),
            ("GdipSetClipPath", [vp, vp, i]),
            ("GdipLoadImageFromFile", [ctypes.c_wchar_p, P(vp)]),
            ("GdipGetImageWidth", [vp, P(ui)]),
            ("GdipGetImageHeight", [vp, P(ui)]),
            ("GdipDrawImageRectI", [vp, vp, i, i, i, i]),
            ("GdipDrawImageRectRect", [vp, vp, f, f, f, f, f, f, f, f, i, vp, vp, vp]),
            ("GdipCreateImageAttributes", [P(vp)]),
            ("GdipSetImageAttributesColorMatrix", [vp, i, wintypes.BOOL, P(_COLORMATRIX), vp, i]),
            ("GdipDisposeImageAttributes", [vp]),
            ("GdipCreateFontFamilyFromName", [ctypes.c_wchar_p, vp, P(vp)]),
            ("GdipNewPrivateFontCollection", [P(vp)]),
            ("GdipPrivateAddFontFile", [vp, ctypes.c_wchar_p]),
            ("GdipDeleteFontFamily", [vp]),
            ("GdipCreateFont", [vp, f, i, i, P(vp)]),
            ("GdipDeleteFont", [vp]),
            ("GdipStringFormatGetGenericTypographic", [P(vp)]),
            ("GdipCloneStringFormat", [vp, P(vp)]),
            ("GdipSetStringFormatFlags", [vp, i]),
            ("GdipSetStringFormatTrimming", [vp, i]),
            ("GdipSetStringFormatAlign", [vp, i]),
            ("GdipSetStringFormatLineAlign", [vp, i]),
            ("GdipDeleteStringFormat", [vp]),
            ("GdipDrawString", [vp, ctypes.c_wchar_p, i, vp, P(_RECTF), vp, vp]),
            ("GdipMeasureString", [vp, ctypes.c_wchar_p, i, vp, P(_RECTF), vp, P(_RECTF), vp, vp]),
            ("GdipDrawDriverString", [vp, P(ctypes.c_uint16), i, vp, vp, P(_POINTF), i, vp]),
            ("GdipGetCellAscent", [vp, i, P(ctypes.c_uint16)]),
            ("GdipGetEmHeight", [vp, i, P(ctypes.c_uint16)]),
            ("GdipSaveImageToFile", [vp, ctypes.c_wchar_p, P(_GUID), vp]),
    ):
        fn = getattr(gdip, name)
        fn.restype = ctypes.c_int
        fn.argtypes = args


def _dpi_aware(user32):
    try:
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _monitor_dpi(hmon):
    try:
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        x, y = ctypes.c_uint(96), ctypes.c_uint(96)
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return x.value
    except Exception:
        pass
    return 96


class Gdi:
    """GDI+ mit Schrift-, Format- und Bild-Speicher. Nur aus dem UI-Thread benutzen."""

    def __init__(self, lang=None):
        self.user32, self.gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        self.kernel32, self.gdip = ctypes.windll.kernel32, ctypes.windll.gdiplus
        _prototypes(self.user32, self.gdi32, self.kernel32, self.gdip)
        self.lang = lang
        tok = ctypes.c_size_t()
        gsi = _GdiplusStartupInput(1, None, False, False)
        if self.gdip.GdiplusStartup(ctypes.byref(tok), ctypes.byref(gsi), None) != 0:
            raise OSError("GdiplusStartup fehlgeschlagen")
        self._families, self._fonts, self._fmts, self._images = {}, {}, {}, {}
        self._scaled = {}               # Bilder schon in Zielgroesse (Pfad, Pixel, Ausschnitt) -> Bitmap
        self._shadows = {}              # weicher Schatten je Panelgroesse + Massstab -> (Bitmap, x, y, w, h)
        self._scale = 1.0               # Massstab des laufenden render()
        self.prof = None                # Test: {Art: Sekunden} je Zeichenbefehl
        self._mcache = {}
        # Zeichensatz selbst setzen (siehe LETTER_SPACING): Schrift-Handle -> (Familie, Groesse, Stil, Familien-Handle)
        self._finfo, self._kcache, self._runs, self._vm = {}, {}, {}, {}
        self.kerning, self.spacing, self.native_text = True, True, False    # Testschalter (text_test)
        self._metrics = {}
        try:
            with open(METRICS_FILE, "r", encoding="utf-8") as fh:
                self._metrics = json.load(fh)
            for fm in self._metrics.values():
                for st in fm.get("kern", ()):
                    st["_cov"] = set(map(str, st["cov"]))
        except (OSError, ValueError):
            self._metrics = {}
        # Schriften der Website aus fonts/ - nur fuer diesen Prozess, nichts wird installiert
        self._priv = ctypes.c_void_p()
        self.private_fonts = 0
        if self.gdip.GdipNewPrivateFontCollection(ctypes.byref(self._priv)) == 0 and os.path.isdir(FONT_DIR):
            for fn in sorted(os.listdir(FONT_DIR)):
                if fn.lower().endswith(".ttf") and \
                        self.gdip.GdipPrivateAddFontFile(self._priv, os.path.join(FONT_DIR, fn)) == 0:
                    self.private_fonts += 1
        self._mbmp, self._mg = ctypes.c_void_p(), ctypes.c_void_p()
        self.gdip.GdipCreateBitmapFromScan0(8, 8, 0, PARGB, None, ctypes.byref(self._mbmp))
        self.gdip.GdipGetImageGraphicsContext(self._mbmp, ctypes.byref(self._mg))
        self.gdip.GdipSetTextRenderingHint(self._mg, 4)

    # ---- Schriften ----
    def _family(self, name, private=False):
        ck = (name, private)
        if ck not in self._families:
            fam = ctypes.c_void_p()
            coll = self._priv if private and self._priv.value else None
            ok = (not private or coll) and \
                self.gdip.GdipCreateFontFamilyFromName(name, coll, ctypes.byref(fam)) == 0 and fam.value
            self._families[ck] = fam if ok else None
        return self._families[ck]

    def font(self, key, text=""):
        weight, size = FONTS.get(key, ("regular", 12))
        script = script_of(text, self.lang)
        ck = (key, script)
        hit = self._fonts.get(ck)
        if hit:
            return hit
        cands = []
        if script:
            style = 0 if weight == "regular" else 1
            cands += [(n, style, False) for n in _SCRIPT_FAMILIES[script]]
        cands += list(_FAMILIES[weight]) + [("Segoe UI", 0, False), ("Arial", 0, False), ("Tahoma", 0, False)]
        font = None
        for name, style, private in cands:
            fam = self._family(name, private)
            if not fam:
                continue
            f = ctypes.c_void_p()
            if self.gdip.GdipCreateFont(fam, ctypes.c_float(size), style, 2, ctypes.byref(f)) == 0 and f.value:
                font = f
                self._finfo[f.value] = (name if private else None, float(size), style, fam)
                break
        if font is None:
            raise OSError("Keine Schrift gefunden")
        self._fonts[ck] = font
        return font

    def fmt(self, align, wrap, measure=False):
        ck = (align, wrap, measure)
        hit = self._fmts.get(ck)
        if hit:
            return hit
        base, fmt = ctypes.c_void_p(), ctypes.c_void_p()
        self.gdip.GdipStringFormatGetGenericTypographic(ctypes.byref(base))
        self.gdip.GdipCloneStringFormat(base, ctypes.byref(fmt))
        flags = 0x0004 | 0x2000                     # NoFitBlackBox | LineLimit
        if not wrap:
            flags |= 0x1000                         # NoWrap
        if measure:
            flags |= 0x0800                         # MeasureTrailingSpaces
        self.gdip.GdipSetStringFormatFlags(fmt, flags)
        self.gdip.GdipSetStringFormatTrimming(fmt, 0 if measure else 3)     # EllipsisCharacter
        self.gdip.GdipSetStringFormatAlign(fmt, {"l": 0, "c": 1, "r": 2}.get(align, 0))
        self.gdip.GdipSetStringFormatLineAlign(fmt, 0 if wrap else 1)
        self._fmts[ck] = fmt
        return fmt

    def _measure(self, text, font):
        layout, box = _RECTF(0, 0, 4000, 400), _RECTF()
        self.gdip.GdipMeasureString(self._mg, text, -1, font, ctypes.byref(layout),
                                    self.fmt("l", False, True), ctypes.byref(box), None, None)
        return float(box.Width)

    def measure(self, text, key, font=None):
        if not text:
            return 0.0
        ck = (text, key, font.value if font is not None else None)
        hit = self._mcache.get(ck)
        if hit is None:
            if len(self._mcache) > 6000:
                self._mcache.clear()
            run = self._glyph_run(key, text) if font is None else None
            hit = self._mcache[ck] = run[1] if run else self._measure(text, font if font is not None else self.font(key, text))
        return hit

    # ---- Zeichen selbst setzen (Kerning + Laufweite, siehe LETTER_SPACING) ----
    def _kern(self, fam, m, a, b):
        """Kerning des Paars a+b in Schrifteinheiten - Auswertung wie OpenType (erste passende Untertabelle)."""
        ck = (fam, a, b)
        v = self._kcache.get(ck)
        if v is None:
            v, sa, sb = 0, str(ord(a)), str(ord(b))
            for st in m.get("kern", ()):
                if sa not in st["_cov"]:
                    continue
                if st["t"] == 1:
                    row = st["p"].get(sa)
                    if row is not None and sb in row:
                        v = row[sb]
                        break
                    continue
                v = st["m"].get("%d,%d" % (st["c1"].get(sa, 0), st["c2"].get(sb, 0)), 0)
                break
            if len(self._kcache) > 30000:
                self._kcache.clear()
            self._kcache[ck] = v
        return v

    def _glyph_run(self, key, text, track_px=None):
        """(x je Zeichen ab 0, Breite) mit Kerning und Laufweite - nur fuer die mitgelieferten Schriften
        und nur, wenn jedes Zeichen darin steht. Sonst None (dann GDI+ wie bisher)."""
        if not text or not self._metrics or self.native_text:
            return None
        font = self.font(key, text)
        info = self._finfo.get(font.value)
        m = self._metrics.get(info[0]) if info and info[0] else None
        if not m:
            return None
        ck = (font.value, text, track_px)
        hit = self._runs.get(ck)
        if hit is not None:
            return hit or None
        adv, size = m["adv"], info[1]
        k = size / float(m["upm"])
        if track_px is None:
            track_px = LETTER_SPACING.get(key, 0.0) * size if self.spacing else 0.0
        xs, x, prev = [], 0.0, None
        for ch in text:
            a = adv.get(str(ord(ch)))
            if a is None:
                xs = None
                break
            if prev is not None:
                x += track_px + (self._kern(info[0], m, prev, ch) * k if self.kerning else 0.0)
            xs.append(x)
            x += a * k
            prev = ch
        out = (xs, x) if xs is not None else False
        if len(self._runs) > 4000:
            self._runs.clear()
        self._runs[ck] = out
        return out or None

    def _fit_run(self, key, text, w):
        """Wie _glyph_run, passt aber in die Breite w (sonst mit "…" gekuerzt wie bisher GDI+)."""
        run = self._glyph_run(key, text)
        if run is None:
            return None
        if run[1] <= w + 0.5:
            return text, run[0], run[1]
        s = text
        while s:
            s = s[:-1].rstrip()
            r = self._glyph_run(key, s + "\u2026")
            if r is None:
                return None
            if r[1] <= w + 0.5:
                return s + "\u2026", r[0], r[1]
        return None

    def _baseline(self, font):
        """(Oberlaenge, Zeilenhoehe) in Pixeln, so wie GDI+ eine einzeilige Zeile setzt."""
        hit = self._vm.get(font.value)
        if hit is None:
            _, size, style, fam = self._finfo[font.value]
            em, asc = ctypes.c_uint16(), ctypes.c_uint16()
            self.gdip.GdipGetEmHeight(fam, style, ctypes.byref(em))
            self.gdip.GdipGetCellAscent(fam, style, ctypes.byref(asc))
            layout, box = _RECTF(0, 0, 4000, 400), _RECTF()
            self.gdip.GdipMeasureString(self._mg, "Hg", -1, font, ctypes.byref(layout),
                                        self.fmt("l", False, True), ctypes.byref(box), None, None)
            hit = self._vm[font.value] = (asc.value * size / float(em.value or 1), float(box.Height))
        return hit

    def _draw_run(self, g, key, text, xs, x0, y, h, brush):
        """Zeichen an festen Positionen (DriverString): Grundlinie wie GDI+ bei mittiger Zeile."""
        font = self.font(key, text)
        asc, lh = self._baseline(font)
        base = y + (h - lh) / 2.0 + asc
        n = len(text)
        buf = (ctypes.c_uint16 * n)(*[ord(c) for c in text])
        pts = (_POINTF * n)(*[_POINTF(x0 + px, base) for px in xs])
        return self.gdip.GdipDrawDriverString(g, buf, n, font, brush, pts, 1, None) == 0    # 1 = CmapLookup

    # ---- Bilder ----
    def image(self, path):
        """Bild einmal laden und als eigene Bitmap behalten - die Datei bleibt so nicht
        gesperrt (GDI+ haelt geladene Dateien sonst offen; der Bildspeicher raeumt auf)."""
        if not path:
            return None
        if path in self._images:
            return self._images[path]
        out = None
        img = ctypes.c_void_p()
        if os.path.isfile(path) and self.gdip.GdipLoadImageFromFile(path, ctypes.byref(img)) == 0 and img.value:
            w, h = ctypes.c_uint(), ctypes.c_uint()
            self.gdip.GdipGetImageWidth(img, ctypes.byref(w))
            self.gdip.GdipGetImageHeight(img, ctypes.byref(h))
            if w.value and h.value:
                k = min(1.0, 640.0 / max(w.value, h.value))
                cw, ch = max(1, int(w.value * k)), max(1, int(h.value * k))
                bmp, g = ctypes.c_void_p(), ctypes.c_void_p()
                if self.gdip.GdipCreateBitmapFromScan0(cw, ch, 0, PARGB, None, ctypes.byref(bmp)) == 0:
                    self.gdip.GdipGetImageGraphicsContext(bmp, ctypes.byref(g))
                    self.gdip.GdipSetInterpolationMode(g, 7)
                    self.gdip.GdipSetPixelOffsetMode(g, 4)
                    self.gdip.GdipDrawImageRectI(g, img, 0, 0, cw, ch)
                    self.gdip.GdipDeleteGraphics(g)
                    out = (bmp, cw, ch)
            self.gdip.GdipDisposeImage(img)
        if len(self._images) > 80:
            gone = set()
            for p, v in list(self._images.items())[:40]:
                if v:
                    self.gdip.GdipDisposeImage(v[0])
                del self._images[p]
                gone.add(p)
            for k in [k for k in self._scaled if k[0] in gone]:
                self.gdip.GdipDisposeImage(self._scaled.pop(k)[0])
        if out or os.path.isfile(path):
            self._images[path] = out            # fehlt die Datei noch (Download laeuft), spaeter erneut
        return out

    def _shadow_rings(self, g, x, y, w, h, r, spread, alpha):
        n = max(1, int(spread))
        dy = spread * 0.3
        for i in range(n, 0, -1):
            a = alpha * ((n - i + 1) / float(n)) ** 2 / n * 2.4
            path = self._rrect(x - i, y - i + dy, w + 2 * i, h + 2 * i, r + i)
            self._fill_path(g, path, _argb(COL["black"], a))
            self.gdip.GdipDeletePath(path)

    def _shadow(self, g, x, y, w, h, r, spread, alpha):
        """Weicher Schatten (viele grosse Flaechen, war der teuerste Teil jedes Zeichnens): einmal je
        Groesse in eine eigene Bitmap, danach nur noch 1:1 aufs Pixelraster kopieren."""
        gd, sc = self.gdip, self._scale
        n = max(1, int(spread))
        dy = spread * 0.3
        x0, y0 = int(math.floor((x - n) * sc)) - 1, int(math.floor((y - n + dy) * sc)) - 1
        x1, y1 = int(math.ceil((x + w + n) * sc)) + 1, int(math.ceil((y + h + n + dy) * sc)) + 1
        key = (round(x, 2), round(y, 2), round(w, 2), round(h, 2), r, spread, alpha, round(sc, 4))
        hit = self._shadows.get(key)
        if hit is None:
            pw, ph = x1 - x0, y1 - y0
            bmp, g2 = ctypes.c_void_p(), ctypes.c_void_p()
            if gd.GdipCreateBitmapFromScan0(pw, ph, 0, PARGB, None, ctypes.byref(bmp)) != 0:
                return self._shadow_rings(g, x, y, w, h, r, spread, alpha)
            gd.GdipGetImageGraphicsContext(bmp, ctypes.byref(g2))
            gd.GdipGraphicsClear(g2, ctypes.c_uint32(0))
            gd.GdipSetSmoothingMode(g2, 4)
            gd.GdipSetPixelOffsetMode(g2, 4)
            gd.GdipSetCompositingQuality(g2, 1)
            gd.GdipScaleWorldTransform(g2, ctypes.c_float(sc), ctypes.c_float(sc), 0)
            # erst verschieben, dann skalieren: Pixel (0,0) der Bitmap = Pixel (x0,y0) des Panels
            gd.GdipTranslateWorldTransform(g2, ctypes.c_float(-x0 / sc), ctypes.c_float(-y0 / sc), 0)
            self._shadow_rings(g2, x, y, w, h, r, spread, alpha)
            gd.GdipDeleteGraphics(g2)
            if len(self._shadows) > 12:
                for k in list(self._shadows)[:6]:
                    gd.GdipDisposeImage(self._shadows.pop(k)[0])
            hit = self._shadows[key] = (bmp, x0, y0, pw, ph)
        bmp, x0, y0, pw, ph = hit
        state = ctypes.c_uint()
        gd.GdipSaveGraphics(g, ctypes.byref(state))
        gd.GdipResetWorldTransform(g)
        gd.GdipSetInterpolationMode(g, 5)               # NearestNeighbor: genau 1:1
        gd.GdipSetPixelOffsetMode(g, 4)
        gd.GdipDrawImageRectI(g, bmp, x0, y0, pw, ph)
        gd.GdipRestoreGraphics(g, state)

    def _fit_image(self, path, img, w, h, sx, sy, sw, sh):
        """Ausschnitt einmal hochwertig auf die Pixelgroesse am Bildschirm bringen und merken. Vorher wurde
        jedes Bild bei jedem Zeichnen neu von bis zu 640 px verkleinert (HighQualityBicubic) - das war ein
        grosser Teil der Zeichenzeit."""
        pw, ph = int(round(w * self._scale)), int(round(h * self._scale))
        if pw < 1 or ph < 1 or (pw >= sw and ph >= sh):
            return None                     # nicht kleiner als die Quelle -> direkt zeichnen
        key = (path, pw, ph, round(sx, 1), round(sy, 1), round(sw, 1), round(sh, 1))
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        bmp, g = ctypes.c_void_p(), ctypes.c_void_p()
        if self.gdip.GdipCreateBitmapFromScan0(pw, ph, 0, PARGB, None, ctypes.byref(bmp)) != 0:
            return None
        self.gdip.GdipGetImageGraphicsContext(bmp, ctypes.byref(g))
        self.gdip.GdipSetInterpolationMode(g, 7)              # HighQualityBicubic
        self.gdip.GdipSetPixelOffsetMode(g, 4)
        self.gdip.GdipDrawImageRectRect(g, img[0], 0.0, 0.0, float(pw), float(ph), sx, sy, sw, sh, 2,
                                        None, None, None)
        self.gdip.GdipDeleteGraphics(g)
        if len(self._scaled) > 160:
            for k in list(self._scaled)[:80]:
                self.gdip.GdipDisposeImage(self._scaled.pop(k)[0])
        hit = self._scaled[key] = (bmp, pw, ph)
        return hit

    # ---- Formen ----
    def _rrect(self, x, y, w, h, r):
        path = ctypes.c_void_p()
        self.gdip.GdipCreatePath(0, ctypes.byref(path))
        r = max(0.0, min(float(r), w / 2.0, h / 2.0))
        if r <= 0.01:
            self.gdip.GdipAddPathRectangle(path, x, y, w, h)
            return path
        d = r * 2
        self.gdip.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
        self.gdip.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
        self.gdip.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
        self.gdip.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
        self.gdip.GdipClosePathFigure(path)
        return path

    def _fill_path(self, g, path, argb):
        brush = ctypes.c_void_p()
        self.gdip.GdipCreateSolidFill(argb, ctypes.byref(brush))
        self.gdip.GdipFillPath(g, brush, path)
        self.gdip.GdipDeleteBrush(brush)

    def set_text_mode(self, native=False, kerning=True, spacing=True):
        """Nur fuer text_test: alter GDI+-Satz bzw. eigener Satz ohne Kerning/Laufweite."""
        self.native_text, self.kerning, self.spacing = native, kerning, spacing
        self._runs.clear()
        self._mcache.clear()

    def _op(self, g, it, clips):
        gd = self.gdip
        kind = it[0]
        if kind == "textmode":
            self.set_text_mode(*it[1:])
            return
        if kind == "texthint":          # nur text_test: Kantenglaettung (3 = mit Raster, 4 = ohne) + Kontrast
            self.gdip.GdipSetTextRenderingHint(g, it[1])
            self.gdip.GdipSetTextContrast(g, it[2])
            return
        if kind == "fill":
            _, x, y, w, h, r, color, alpha = it
            if w <= 0 or h <= 0:
                return
            path = self._rrect(x, y, w, h, r)
            self._fill_path(g, path, _argb(color, alpha))
            gd.GdipDeletePath(path)
        elif kind in ("vgrad", "hgrad", "dgrad"):
            _, x, y, w, h, r, c1, a1, c2, a2 = it
            if w <= 0 or h <= 0:
                return
            p1 = _POINTF(x, y)
            p2 = {"vgrad": _POINTF(x, y + h), "hgrad": _POINTF(x + w, y)}.get(kind) or _POINTF(x + w, y + h)
            brush = ctypes.c_void_p()
            gd.GdipCreateLineBrush(ctypes.byref(p1), ctypes.byref(p2), _argb(c1, a1), _argb(c2, a2), 3,
                                   ctypes.byref(brush))
            path = self._rrect(x, y, w, h, r)
            gd.GdipFillPath(g, brush, path)
            gd.GdipDeletePath(path)
            gd.GdipDeleteBrush(brush)
        elif kind == "glow":
            # weicher Schimmer: Ellipse, innen Farbe, nach aussen durchsichtig (wie radial-gradient)
            _, x, y, w, h, color, alpha = it
            path, brush = ctypes.c_void_p(), ctypes.c_void_p()
            gd.GdipCreatePath(0, ctypes.byref(path))
            gd.GdipAddPathEllipse(path, x, y, w, h)
            if gd.GdipCreatePathGradientFromPath(path, ctypes.byref(brush)) == 0 and brush.value:
                gd.GdipSetPathGradientCenterColor(brush, _argb(color, alpha))
                edge = (ctypes.c_uint32 * 1)(_argb(color, 0).value)
                cnt = ctypes.c_int(1)
                gd.GdipSetPathGradientSurroundColorsWithCount(brush, edge, ctypes.byref(cnt))
                gd.GdipFillPath(g, brush, path)
                gd.GdipDeleteBrush(brush)
            gd.GdipDeletePath(path)
        elif kind == "ttext":
            # Mono-Ueberschrift mit Zeichenabstand (GDI+ kennt kein letter-spacing): Zeichen einzeln
            _, x, y, h, s, key, color, alpha, track = it
            if not s:
                return
            font = self.font(key, s)
            brush = ctypes.c_void_p()
            gd.GdipCreateSolidFill(_argb(color, alpha), ctypes.byref(brush))
            run = self._glyph_run(key, s, float(track))
            if run and self._draw_run(g, key, s, run[0], float(x), y, h, brush):
                gd.GdipDeleteBrush(brush)
                return
            fmt = self.fmt("l", False, True)
            cx = float(x)
            for ch in s:
                adv = self.measure(ch, key, font)
                if not ch.isspace():
                    rect = _RECTF(cx, y, adv + 6, h)
                    gd.GdipDrawString(g, ch, -1, font, ctypes.byref(rect), fmt, brush)
                cx += adv + track
            gd.GdipDeleteBrush(brush)
        elif kind == "stroke":
            _, x, y, w, h, r, color, alpha, width = it
            pen = ctypes.c_void_p()
            gd.GdipCreatePen1(_argb(color, alpha), ctypes.c_float(width), 2, ctypes.byref(pen))
            o = width / 2.0
            path = self._rrect(x + o, y + o, w - width, h - width, max(0.0, r - o))
            gd.GdipDrawPath(g, pen, path)
            gd.GdipDeletePath(path)
            gd.GdipDeletePen(pen)
        elif kind == "shadow":
            self._shadow(g, *it[1:])
        elif kind == "line":
            _, x1, y1, x2, y2, color, alpha, width = it
            pen = ctypes.c_void_p()
            gd.GdipCreatePen1(_argb(color, alpha), ctypes.c_float(width), 2, ctypes.byref(pen))
            gd.GdipSetPenStartCap(pen, 2)
            gd.GdipSetPenEndCap(pen, 2)
            gd.GdipDrawLine(g, pen, x1, y1, x2, y2)
            gd.GdipDeletePen(pen)
        elif kind == "text":
            _, x, y, w, h, s, key, color, alpha, align, wrap = it
            if not s or w <= 1:
                return
            brush = ctypes.c_void_p()
            gd.GdipCreateSolidFill(_argb(color, alpha), ctypes.byref(brush))
            fit = None if wrap else self._fit_run(key, s, w)
            drawn = False
            if fit:
                s2, xs, tw = fit
                x0 = x if align == "l" else (x + (w - tw) / 2.0 if align == "c" else x + w - tw)
                drawn = self._draw_run(g, key, s2, xs, x0, y, h, brush)
            if not drawn:
                rect = _RECTF(x, y, w, h)
                gd.GdipDrawString(g, s, -1, self.font(key, s), ctypes.byref(rect), self.fmt(align, wrap), brush)
            gd.GdipDeleteBrush(brush)
        elif kind == "image":
            _, x, y, w, h, path_, r, alpha, cover = it
            img = self.image(path_)
            if not img:
                return
            bmp, iw, ih = img
            sx, sy, sw, sh = 0.0, 0.0, float(iw), float(ih)
            if cover:
                k = max(w / float(iw), h / float(ih))
                sw, sh = w / k, h / k
                sx, sy = (iw - sw) / 2.0, (ih - sh) / 2.0
            small = self._fit_image(path_, img, w, h, sx, sy, sw, sh)
            if small:
                bmp, (sx, sy, sw, sh) = small[0], (0.0, 0.0, float(small[1]), float(small[2]))
            state = ctypes.c_uint()
            gd.GdipSaveGraphics(g, ctypes.byref(state))
            if r > 0:
                clip = self._rrect(x, y, w, h, r)
                gd.GdipSetClipPath(g, clip, 1)              # CombineModeIntersect
                gd.GdipDeletePath(clip)
            attrs = ctypes.c_void_p()
            if alpha < 255:
                cm = _COLORMATRIX()
                for k in range(5):
                    cm.m[k][k] = 1.0
                cm.m[3][3] = alpha / 255.0
                gd.GdipCreateImageAttributes(ctypes.byref(attrs))
                gd.GdipSetImageAttributesColorMatrix(attrs, 1, True, ctypes.byref(cm), None, 0)
            gd.GdipDrawImageRectRect(g, bmp, x, y, w, h, sx, sy, sw, sh, 2, attrs if attrs.value else None, None, None)
            if attrs.value:
                gd.GdipDisposeImageAttributes(attrs)
            gd.GdipRestoreGraphics(g, state)
        elif kind == "clip":
            _, x, y, w, h, r = it
            state = ctypes.c_uint()
            gd.GdipSaveGraphics(g, ctypes.byref(state))
            clips.append(state.value)
            clip = self._rrect(x, y, w, h, r)
            gd.GdipSetClipPath(g, clip, 1)
            gd.GdipDeletePath(clip)
        elif kind == "unclip":
            if clips:
                gd.GdipRestoreGraphics(g, clips.pop())

    def render(self, ops, scale, save_png=None):
        """Zeichnet in eine DIB (32 Bit, vormultipliziertes Alpha) fuer UpdateLayeredWindow."""
        gd = self.gdip
        w, h = max(1, int(math.ceil(ops.w * scale))), max(1, int(math.ceil(ops.h * scale)))
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h          # von oben nach unten
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        screen = self.user32.GetDC(None)
        mem = self.gdi32.CreateCompatibleDC(screen)
        self.user32.ReleaseDC(None, screen)
        bits = ctypes.c_void_p()
        hbmp = self.gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbmp or not bits.value:
            self.gdi32.DeleteDC(mem)
            raise OSError("CreateDIBSection fehlgeschlagen")
        old = self.gdi32.SelectObject(mem, hbmp)
        gbmp, g = ctypes.c_void_p(), ctypes.c_void_p()
        gd.GdipCreateBitmapFromScan0(w, h, w * 4, PARGB, bits, ctypes.byref(gbmp))
        gd.GdipGetImageGraphicsContext(gbmp, ctypes.byref(g))
        gd.GdipGraphicsClear(g, ctypes.c_uint32(0))
        gd.GdipSetSmoothingMode(g, 4)               # AntiAlias
        gd.GdipSetPixelOffsetMode(g, 4)             # Half
        gd.GdipSetInterpolationMode(g, 7)           # HighQualityBicubic
        # NICHT HighQuality: das mischt mit Gammakorrektur - ein 3-%-Weissschleier auf dunklem
        # Grund wurde damit fast 20 % hell (im ersten Windows-Test gesehen)
        gd.GdipSetCompositingQuality(g, 1)          # HighSpeed = normale Mischung
        gd.GdipSetTextRenderingHint(g, 4)           # AntiAlias (ClearType passt nicht auf Alpha)
        # Kontrast 0 statt 4: helle Schrift auf Dunkel wird voller, wie im Browser (im Windows-Test
        # verglichen, 2.5.6-dev1) - mit 4 wirkte kleiner grauer Text duenn und blass
        gd.GdipSetTextContrast(g, 0)
        gd.GdipScaleWorldTransform(g, ctypes.c_float(scale), ctypes.c_float(scale), 0)
        self._scale = scale
        clips = []
        prof = self.prof
        for it in ops.items:
            if prof is None:
                self._op(g, it, clips)
            else:
                t0 = time.perf_counter()
                self._op(g, it, clips)
                prof[it[0]] = prof.get(it[0], 0.0) + time.perf_counter() - t0
        gd.GdipDeleteGraphics(g)
        if save_png:
            clsid = _GUID(_PNG_CLSID[0], _PNG_CLSID[1], _PNG_CLSID[2], (ctypes.c_ubyte * 8)(*_PNG_CLSID[3]))
            gd.GdipSaveImageToFile(gbmp, save_png, ctypes.byref(clsid), None)
        gd.GdipDisposeImage(gbmp)
        return {"mem": mem, "hbmp": hbmp, "old": old, "w": w, "h": h}

    def free(self, frame):
        if frame:
            self.gdi32.SelectObject(frame["mem"], frame["old"])
            self.gdi32.DeleteObject(frame["hbmp"])
            self.gdi32.DeleteDC(frame["mem"])

    def screenshot(self, path):
        """Ganzer Hauptbildschirm als PNG (nur fuer den CI-Test, mit CAPTUREBLT -> inkl. Overlays)."""
        u, g32, gd = self.user32, self.gdi32, self.gdip
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        screen = u.GetDC(None)
        mem = g32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        hbmp = g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = g32.SelectObject(mem, hbmp)
        g32.BitBlt(mem, 0, 0, w, h, screen, 0, 0, 0x00CC0020 | 0x40000000)     # SRCCOPY | CAPTUREBLT
        u.ReleaseDC(None, screen)
        gbmp = ctypes.c_void_p()
        gd.GdipCreateBitmapFromScan0(w, h, w * 4, RGB32, bits, ctypes.byref(gbmp))
        clsid = _GUID(_PNG_CLSID[0], _PNG_CLSID[1], _PNG_CLSID[2], (ctypes.c_ubyte * 8)(*_PNG_CLSID[3]))
        gd.GdipSaveImageToFile(gbmp, path, ctypes.byref(clsid), None)
        gd.GdipDisposeImage(gbmp)
        g32.SelectObject(mem, old)
        g32.DeleteObject(hbmp)
        g32.DeleteDC(mem)


# ============================ Windows: Fenster ============================

WM_APP_STATE = 0x8001
WM_APP_QUIT = 0x8002
_EX_BASE = 0x00000008 | 0x00000080 | 0x00080000 | 0x08000000      # TOPMOST|TOOLWINDOW|LAYERED|NOACTIVATE
_EX_CLICKTHROUGH = 0x00000020                                     # TRANSPARENT
_HWND_TOPMOST = ctypes.c_void_p(-1)
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
VALORANT_EXE = "valorant-win64-shipping.exe"


class OverlayWindow:
    def __init__(self, app, name, clickthrough):
        self.app, self.name = app, name
        u = app.gdi.user32
        ex = _EX_BASE | (_EX_CLICKTHROUGH if clickthrough else 0)
        self.hwnd = u.CreateWindowExW(ex, app.class_name, "RankYoinker", 0x80000000, 0, 0, 1, 1,
                                      None, None, app.hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW fehlgeschlagen (%s)" % name)
        self.frame, self.ops, self.sig = None, None, None
        self.pos, self.size, self.visible = (0, 0), (0, 0), False
        self.top_at, self.scale = 0.0, 1.0

    def update(self, ops, scale, x, y):
        frame = self.app.gdi.render(ops, scale)
        blend = _BLENDFUNCTION(0, 0, 255, 1)            # AC_SRC_OVER, AC_SRC_ALPHA
        pt, size, src = _POINT(int(x), int(y)), _SIZE(frame["w"], frame["h"]), _POINT(0, 0)
        screen = self.app.gdi.user32.GetDC(None)
        ok = self.app.gdi.user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt), ctypes.byref(size),
                                                     frame["mem"], ctypes.byref(src), 0, ctypes.byref(blend), 2)
        self.app.gdi.user32.ReleaseDC(None, screen)
        self.app.gdi.free(self.frame)
        self.frame, self.ops = frame, ops
        self.pos, self.size = (int(x), int(y)), (frame["w"], frame["h"])
        if not ok:
            raise OSError("UpdateLayeredWindow fehlgeschlagen (%s)" % self.name)

    def move(self, x, y):
        if (int(x), int(y)) != self.pos:
            self.app.gdi.user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0,
                                             _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
            self.pos = (int(x), int(y))

    def show(self):
        now = time.time()
        if not self.visible:
            self.app.gdi.user32.ShowWindow(self.hwnd, 8)    # SW_SHOWNA
            self.visible = True
        elif now - self.top_at < 2.0:
            return
        # Spiele holen sich gern wieder nach vorn - alle 2 s wieder ganz nach oben
        self.app.gdi.user32.SetWindowPos(self.hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                         _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
        self.top_at = now

    def hide(self):
        if self.visible:
            self.app.gdi.user32.ShowWindow(self.hwnd, 0)    # SW_HIDE
            self.visible = False


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gdi = Gdi()
        u, k = self.gdi.user32, self.gdi.kernel32
        self.hinst = k.GetModuleHandleW(None)
        self.class_name = "RankYoinkerGameOverlay"
        self._proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_ssize_t)(self._wndproc)
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = self.hinst
        wc.hCursor = u.LoadCursorW(None, ctypes.c_void_p(32512))      # IDC_ARROW
        wc.lpszClassName = self.class_name
        u.RegisterClassW(ctypes.byref(wc))
        self.hand = u.LoadCursorW(None, ctypes.c_void_p(32649))      # IDC_HAND
        self.wins = {"pregame": OverlayWindow(self, "pregame", True),
                     "board": OverlayWindow(self, "board", True),
                     "card": OverlayWindow(self, "card", False)}
        self.main = self.wins["card"].hwnd
        self.state, self._raw, self._lock = {}, None, threading.Lock()
        self.sigs = {}
        self.ui = {"hover": None, "pressed": None, "skip": False, "tracking": False, "inside": False}
        self._mon = {"at": 0.0, "key": None, "val": None}
        self.shown = {}                 # Ergebnis-ID -> sichtbare Sekunden
        self.dismissed = set()          # ("result"|"ask", id) - hier schon weggeklickt
        self.fg = {"hwnd": None, "valorant": False}
        self.forced = False             # Test: Fenster ohne VALORANT zeigen
        self.force_board = True         # Test: bei forced die Tabelle auch ohne Taste zeigen
        self.key_sim = None             # Test: Tabellen-Taste (None = echte Tastatur)
        self._board_pre = 0.0           # letztes unsichtbares Vorzeichnen der Tabelle
        self.errors = 0
        self.stop = False
        self.last_tick = time.time()
        self.parent = None
        if cfg.get("parentPid"):
            self.parent = k.OpenProcess(0x00100000, False, int(cfg["parentPid"]))     # SYNCHRONIZE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # nie ueber einen Proxy

    # ---- Daten vom Dienst ----
    def poll_loop(self):
        url = "http://127.0.0.1:%d/api/ovl/state" % int(self.cfg.get("port") or 1101)
        fails = 0
        while not self.stop:
            busy = True
            try:
                req = urllib.request.Request(url, headers={"X-RY-Ovl": self.cfg.get("token") or ""})
                with self.opener.open(req, timeout=4) as r:
                    raw = r.read()
                fails = 0
                if raw != self._raw:
                    data = json.loads(raw.decode("utf-8"))
                    self.set_state(data if isinstance(data, dict) else {}, raw)
                st = self.state
                busy = bool(st.get("preview") or st.get("pregame") or st.get("board")
                            or st.get("result") or st.get("ask"))
            except Exception:
                fails += 1
                if fails == 3:
                    self.set_state({}, None)
            if self.parent and self.gdi.kernel32.WaitForSingleObject(self.parent, 0) == 0:
                self.gdi.user32.PostMessageW(self.main, WM_APP_QUIT, 0, 0)       # Dienst beendet
                return
            time.sleep(0.4 if busy else 1.2)

    def set_state(self, data, raw=None):
        sigs = {k: json.dumps([data.get(k), data.get("lang"), data.get("preview")], sort_keys=True, default=str)
                for k in ("pregame", "board")}
        with self._lock:
            self._raw, self.state, self.sigs = raw, data, sigs
        self.gdi.user32.PostMessageW(self.main, WM_APP_STATE, 0, 0)

    def action(self, body):
        def run():
            try:
                url = "http://127.0.0.1:%d/api/ovl/action" % int(self.cfg.get("port") or 1101)
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                             headers={"X-RY-Ovl": self.cfg.get("token") or "",
                                                      "Content-Type": "application/json"})
                with self.opener.open(req, timeout=6) as r:
                    r.read()
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    # ---- Vordergrund / Bildschirm ----
    def _exe_of(self, hwnd):
        u, k = self.gdi.user32, self.gdi.kernel32
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = k.OpenProcess(0x1000, False, pid.value)                # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            n = wintypes.DWORD(520)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
        return ""

    def valorant_front(self):
        hwnd = self.gdi.user32.GetForegroundWindow()
        if hwnd != self.fg["hwnd"]:
            self.fg = {"hwnd": hwnd, "valorant": bool(hwnd) and self._exe_of(hwnd) == VALORANT_EXE}
        return self.fg["valorant"] and not self.gdi.user32.IsIconic(hwnd)

    def monitor(self):
        u = self.gdi.user32
        hwnd = self.fg["hwnd"] if self.fg["valorant"] else u.GetForegroundWindow()
        size = (self.state.get("settings") or {}).get("size")
        now = time.time()
        if self._mon["key"] == (hwnd, size) and now - self._mon["at"] < 1.0:
            return self._mon["val"]
        hmon = u.MonitorFromWindow(hwnd, 2 if hwnd else 1)         # NEAREST / PRIMARY
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        u.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r, wk = mi.rcMonitor, mi.rcWork
        mon_h = max(1, r.bottom - r.top)
        # wie das Spiel selbst: auf 1440p/4K ohne Windows-Skalierung mitwachsen
        base = max(_monitor_dpi(hmon) / 96.0, min(2.0, mon_h / 1080.0))
        val = {"mon": (r.left, r.top, r.right, r.bottom), "work": (wk.left, wk.top, wk.right, wk.bottom),
               "scale": round(base * SIZES.get(size, 1.0), 3)}
        self._mon = {"at": now, "key": (hwnd, size), "val": val}
        return val

    # ---- Zeichnen ----
    def ctx(self):
        st = self.state
        self.gdi.lang = st.get("lang")
        return {"strings": st.get("strings") or {}, "lang": st.get("lang"), "preview": bool(st.get("preview"))}

    def _place(self, name, mon, w, h):
        left, top, right, bottom = mon["mon"]
        wl, wt, wr, wb = mon["work"]
        sc = mon["scale"]
        side = (self.state.get("settings") or {}).get("side") or "right"
        margin = int(round(6 * sc))
        if name == "card":
            return wr - w - margin, wb - h - margin
        x = (left + margin) if side == "left" else (right - w - margin)
        if name == "pregame":
            return x, top + int((bottom - top) * 0.11)
        return x, top + max(0, ((bottom - top) - h) // 2)

    def paint(self, name, ops_fn, sig, mon):
        win = self.wins[name]
        full = (sig, mon["scale"], mon["work"])
        if win.sig != full:
            ops = ops_fn()
            sc = mon["scale"]
            avail = (mon["work"][3] - mon["work"][1]) - 2 * int(round(6 * sc))
            if avail > 100 and ops.h * sc > avail:
                sc = avail / float(ops.h)          # zu hoch fuer den Bildschirm -> etwas kleiner
            w, h = int(math.ceil(ops.w * sc)), int(math.ceil(ops.h * sc))
            x, y = self._place(name, mon, w, h)
            win.update(ops, sc, x, y)
            win.scale = sc
            win.sig = full
        else:
            x, y = self._place(name, mon, win.size[0], win.size[1])
            win.move(x, y)

    def _card_parts(self):
        st = self.state
        res, ask = st.get("result"), st.get("ask")
        if res and (("result", res.get("id")) in self.dismissed or self.shown.get(res.get("id"), 0) >= RESULT_SHOW):
            res = None
        if ask and ("ask", ask.get("id")) in self.dismissed:
            ask = None
        return res, ask

    def refresh(self, dt=0.0):
        with self._lock:
            st, sigs = self.state, self.sigs
        settings = st.get("settings") or {}
        preview = st.get("preview")
        front = self.forced or bool(preview) or self.valorant_front()
        mon = self.monitor() if front else None
        m = self.gdi.measure
        ctx = self.ctx() if front else None
        want = {"pregame": False, "board": False, "card": False}
        if front:
            pre = st.get("pregame")
            if pre:
                want["pregame"] = True
                self.paint("pregame", lambda: layout_pregame(m, ctx, pre), sigs.get("pregame"), mon)
            board = st.get("board")
            if board:
                if self.key_sim is not None:
                    down = self.key_sim
                else:
                    vk = KEYS.get(settings.get("key") or "tab", 0x09)
                    down = bool(self.gdi.user32.GetAsyncKeyState(vk) & 0x8000)
                    if vk == 0x09 and self.gdi.user32.GetAsyncKeyState(0x12) & 0x8000:
                        down = False                                       # Alt+Tab
                if down or preview or (self.forced and self.force_board):
                    want["board"] = True
                    self.paint("board", lambda: layout_board(m, ctx, board), sigs.get("board"), mon)
                elif time.time() - self._board_pre >= BOARD_PREPAINT:
                    # unsichtbar vorzeichnen (nur wenn sich etwas geaendert hat, sonst nur Position pruefen)
                    self._board_pre = time.time()
                    self.paint("board", lambda: layout_board(m, ctx, board), sigs.get("board"), mon)
            res, ask = self._card_parts()
            if res or ask:
                want["card"] = True
                frac = None
                if res:
                    hovered = self.ui["inside"] and self.wins["card"].visible
                    if not hovered and not ask:
                        self.shown[res["id"]] = self.shown.get(res["id"], 0) + dt
                    frac = 1.0 - self.shown.get(res["id"], 0) / RESULT_SHOW
                    if frac <= 0:
                        self.action({"action": "dismiss", "kind": "result", "id": res["id"]})
                        want["card"] = bool(ask)
                        res = None
                if want["card"]:
                    ui = dict(self.ui, frac=frac)
                    self.paint("card", lambda: layout_card(m, ctx, res, ask, ui), card_signature(res, ask, ui), mon)
        for name, win in self.wins.items():
            if want[name]:
                win.show()
            else:
                win.hide()
        if not want["card"]:
            self.ui.update(hover=None, pressed=None, inside=False)

    # ---- Maus auf der Karte ----
    def _card_hit(self, lparam):
        win = self.wins["card"]
        if not win.ops:
            return None
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        return win.ops.hit_test(x / win.scale, y / win.scale)

    def click(self, name):
        res, ask = self._card_parts()
        if name == "skip" and ask:
            self.ui["skip"] = not self.ui["skip"]
        elif name in ("yes", "no") and ask:
            answer = "no" if name == "no" else ("yes3" if self.ui["skip"] else "yes")
            self.dismissed.add(("ask", ask.get("id")))
            self.action({"action": "il", "answer": answer, "id": ask.get("id")})
        elif name == "open" and res:
            self.dismissed.add(("result", res.get("id")))
            self.action({"action": "open", "id": res.get("id")})
        elif name == "close":
            if res:
                self.dismissed.add(("result", res.get("id")))
                self.action({"action": "dismiss", "kind": "result", "id": res.get("id")})
            if ask:
                self.dismissed.add(("ask", ask.get("id")))
                self.action({"action": "dismiss", "kind": "ask", "id": ask.get("id")})
        self.refresh()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u = self.gdi.user32
        try:
            if msg == 0x0021:                       # WM_MOUSEACTIVATE
                return 3                            # MA_NOACTIVATE - VALORANT behaelt den Fokus
            if msg == WM_APP_STATE:
                self.refresh()
                return 0
            if msg == WM_APP_QUIT:
                u.PostQuitMessage(0)
                return 0
            if msg == 0x0113:                       # WM_TIMER
                now = time.time()
                dt = min(1.0, max(0.0, now - self.last_tick))
                self.last_tick = now
                self.refresh(dt)
                return 0
            if hwnd == self.main:
                if msg == 0x0200:                   # WM_MOUSEMOVE
                    h = self._card_hit(lparam)
                    self.ui["inside"] = True
                    if not self.ui["tracking"]:
                        tme = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), 0x2, hwnd, 0)    # TME_LEAVE
                        u.TrackMouseEvent(ctypes.byref(tme))
                        self.ui["tracking"] = True
                    if h != self.ui["hover"]:
                        self.ui["hover"] = h
                        self.refresh()
                    return 0
                if msg == 0x02A3:                   # WM_MOUSELEAVE
                    self.ui["tracking"] = False
                    self.ui["inside"] = False
                    if self.ui["hover"]:
                        self.ui["hover"] = None
                        self.refresh()
                    return 0
                if msg == 0x0020 and self.ui["hover"]:  # WM_SETCURSOR
                    u.SetCursor(self.hand)
                    return 1
                if msg == 0x0201:                   # WM_LBUTTONDOWN
                    self.ui["pressed"] = self._card_hit(lparam)
                    return 0
                if msg == 0x0202:                   # WM_LBUTTONUP
                    h = self._card_hit(lparam)
                    if h and h == self.ui["pressed"]:
                        self.click(h)
                    self.ui["pressed"] = None
                    return 0
            if msg == 0x0010:                       # WM_CLOSE
                u.PostQuitMessage(0)
                return 0
        except Exception:
            self.errors += 1
            _log_error(self.cfg)
            if self.errors > 40:
                u.PostQuitMessage(1)
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.gdi.user32.SetTimer(self.main, 1, 33, None)
        msg = _MSG()
        while self.gdi.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.gdi.user32.TranslateMessage(ctypes.byref(msg))
            self.gdi.user32.DispatchMessageW(ctypes.byref(msg))
        self.stop = True
        return 1 if self.errors > 40 else 0


def _log_error(cfg):
    try:
        path = (cfg or {}).get("errFile")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc()[-2000:] + "\n")
    except Exception:
        pass


# ============================ Tests fuer die CI ============================

def _load_test_state(path):
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
    base = os.path.dirname(os.path.abspath(path))

    def fix(o):
        if isinstance(o, dict):
            return {k: (os.path.join(base, v) if isinstance(v, str) and k.endswith(("Img", "img")) and v else fix(v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [fix(v) for v in o]
        return o
    return fix(st)


def render_test(state_path, out_dir):
    """Alle Panels als PNG (echtes GDI+): Deutsch in zwei Groessen mit Hover-Zustaenden, Deathmatch,
    und jedes Panel in jeder weiteren Sprache aus "extraLangs" (mit dort uebersetzten Daten)."""
    os.makedirs(out_dir, exist_ok=True)
    st = _load_test_state(state_path)
    gdi = Gdi(st.get("lang"))
    ctx = {"strings": st.get("strings") or {}, "lang": st.get("lang"), "preview": False}
    m = gdi.measure
    board = st["board"]
    ffa = dict(board, ffa=True, cmp=None, mode="Deathmatch", allies=board["allies"] + board["enemies"], enemies=[])
    jobs = [("pregame", lambda: layout_pregame(m, ctx, st["pregame"])),
            ("board", lambda: layout_board(m, ctx, board)),
            ("board-ffa", lambda: layout_board(m, ctx, ffa)),
            ("card", lambda: layout_card(m, ctx, st["result"], st["ask"], {"hover": "yes", "skip": True})),
            ("card-result", lambda: layout_card(m, ctx, st["result"], None, {"hover": "open", "frac": 0.6})),
            ("card-ask", lambda: layout_card(m, ctx, None, st["ask"], {"hover": None, "skip": False}))]
    for name, fn in jobs:
        for scale in (1.0, 1.5):
            ops = fn()
            gdi.free(gdi.render(ops, scale, os.path.join(out_dir, "%s@%sx.png" % (name, scale))))
    text_test(gdi, m, ctx, board, out_dir)
    for lst in st.get("extraLangs") or []:
        lang = lst["lang"]
        ctx2 = {"strings": lst["strings"], "lang": lang, "preview": False}
        gdi.lang = lang
        for name, fn in (("pregame", lambda: layout_pregame(m, ctx2, lst.get("pregame") or st["pregame"])),
                         ("board", lambda: layout_board(m, ctx2, lst.get("board") or board)),
                         ("card", lambda: layout_card(m, ctx2, lst.get("result") or st["result"],
                                                      lst.get("ask") or st["ask"], {"hover": None, "skip": False}))):
            gdi.free(gdi.render(fn(), 1.0, os.path.join(out_dir, "%s-%s.png" % (name, lang))))
            if lang == "en":                    # fuer englische Schaubilder (README, Website) auch scharf
                gdi.free(gdi.render(fn(), 1.5, os.path.join(out_dir, "%s-%s@1.5x.png" % (name, lang))))
    return 0


def text_test(gdi, m, ctx, board, out_dir):
    """Schriftsatz pruefen (2.5.6-dev1): text-align = alter GDI+-Satz (rot) unter dem eigenen Satz ohne
    Kerning/Laufweite (cyan) - deckungsgleich heisst: Grundlinie und Breiten stimmen. text-look = alt
    neben neu (Kerning + Laufweite). Dazu die Tab-Tabelle noch einmal im alten Satz (board-old)."""
    samples = [("banner", "ASCENT", "l"), ("banner", "Lotus · Haven", "l"), ("big", "VICTORY 13:9", "l"),
               ("title", "Keep going with Jett?", "l"), ("name", "zdruqz", "l"), ("name", "Kaiserschmarrn", "l"),
               ("name", "AVATAR Tyrone", "l"), ("rank", "Platinum 2", "l"), ("rank", "Ascendant 1", "l"),
               ("sub", "WR 54 % · K/D 1.12", "l"), ("sub", "47 RR · Peak Dia 2", "l"),
               ("verdict", "Deutlicher Vorteil für die Gegner", "c"), ("value", "21 / 14 / 6", "l"),
               ("rr", "+18 RR", "r"), ("btn", "Open analysis", "c"), ("chip", "Team MVP", "c"),
               ("tag", "#EUW", "l"), ("grade", "A", "c"), ("eyebrow", "DEIN TEAM", "l"), ("label", "3W · 2L", "l")]
    rh, bw = 40, 470

    def row(ops, x, y, key, text, align, color, alpha):
        if key in ("eyebrow", "label"):
            ops.add("ttext", x, y, rh, text, key, color, alpha, TRACK if key == "eyebrow" else 1.2)
        else:
            ops.add("text", x, y, bw, rh, text, key, color, alpha, align, False)

    ops = Ops(bw + 40, rh * len(samples) + 40)
    ops.add("fill", 0, 0, ops.w, ops.h, 0, COL["bg"], 255)
    for i, (key, text, align) in enumerate(samples):
        y = 20 + i * rh
        ops.add("textmode", True, True, True)
        row(ops, 20, y, key, text, align, "#ff3b3b", 255)
        ops.add("textmode", False, False, False)
        row(ops, 20, y, key, text, align, "#22e6ff", 170)
    ops.add("textmode", False, True, True)
    for scale in (1.0, 1.5):
        gdi.free(gdi.render(ops, scale, os.path.join(out_dir, "text-align@%sx.png" % scale)))
    ops = Ops(2 * bw + 60, rh * len(samples) + 70)
    ops.add("fill", 0, 0, ops.w, ops.h, 0, COL["bg"], 255)
    ops.add("ttext", 20, 12, 20, "GDI+ (BIS 2.5.5)", "eyebrow", COL["text3"], 255, TRACK)
    ops.add("ttext", 40 + bw, 12, 20, "NEU: KERNING + LAUFWEITE", "eyebrow", COL["accent"], 255, TRACK)
    for i, (key, text, align) in enumerate(samples):
        y = 46 + i * rh
        ops.add("textmode", True, True, True)
        row(ops, 20, y, key, text, align, COL["text"], 255)
        ops.add("textmode", False, True, True)
        row(ops, 40 + bw, y, key, text, align, COL["text"], 255)
    for scale in (1.0, 1.5):
        gdi.free(gdi.render(ops, scale, os.path.join(out_dir, "text-look@%sx.png" % scale)))
    # Kleine helle Schrift auf Dunkel: GDI+-Kontrast 0-12 (Standard 4) und Kantenglaettung mit/ohne Raster
    lines = [("name", "zdruqz  Kaiserschmarrn"), ("rank", "Platinum 2  Ascendant 1"), ("sub", "WR 54 % · K/D 1.12"),
             ("sub", "47 RR · Peak Dia 2"), ("form", "3W · 2L   4W · 1L"), ("label", "DEIN TEAM  ACS  ADR")]
    variants = [(4, 0), (4, 2), (4, 4), (4, 8), (4, 12), (3, 4)]
    cw, lh = 230, 22
    ops = Ops(cw * len(variants) + 20, 40 + lh * len(lines) + 20)
    ops.add("fill", 0, 0, ops.w, ops.h, 0, "#141417", 255)
    for ci, (hint, contrast) in enumerate(variants):
        cx = 10 + ci * cw
        ops.add("texthint", 4, 4)
        ops.add("ttext", cx, 10, 20, ("HINT %d · KONTRAST %d" % (hint, contrast)), "eyebrow", COL["accent"], 255, 1.0)
        ops.add("texthint", hint, contrast)
        for li, (key, text) in enumerate(lines):
            col = COL["text"] if key in ("name",) else (COL["teal"] if key == "rank" else COL["text2"])
            ops.add("text", cx, 40 + li * lh, cw - 10, lh, text, key, col, 255, "l", False)
    ops.add("texthint", 4, 0)
    gdi.free(gdi.render(ops, 1.0, os.path.join(out_dir, "text-contrast@1.0x.png")))
    gdi.set_text_mode(native=True)
    gdi.free(gdi.render(layout_board(m, ctx, board), 1.5, os.path.join(out_dir, "board-old@1.5x.png")))
    gdi.free(gdi.render(layout_board(m, ctx, board), 1.0, os.path.join(out_dir, "board-old@1.0x.png")))
    gdi.set_text_mode()


def smoke_test(state_path, out_dir):
    """Echte Fenster auf dem Desktop des Runners: anlegen, Daten per HTTP wie vom Dienst,
    Timer, Mausklicks ueber die Fensterprozedur, Bildschirmfoto."""
    import http.server
    os.makedirs(out_dir, exist_ok=True)
    st = _load_test_state(state_path)
    body = json.dumps(st).encode("utf-8")
    token, actions = "test-token", []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/ovl/state" and self.headers.get("X-RY-Ovl") == token:
                return self._ok(body)
            self.send_response(403)
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if self.path == "/api/ovl/action" and self.headers.get("X-RY-Ovl") == token:
                actions.append(json.loads(self.rfile.read(n) or b"{}"))
                return self._ok(b'{"ok":true}')
            self.send_response(403)
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = App({"errFile": os.path.join(out_dir, "errors.txt"), "port": srv.server_address[1], "token": token})
    app.forced = True
    u = app.gdi.user32
    msg = _MSG()

    def pump(sec):
        deadline = time.time() + sec
        while time.time() < deadline:
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)

    threading.Thread(target=app.poll_loop, daemon=True).start()
    u.SetTimer(app.main, 1, 33, None)
    t0 = time.time()
    while not app.state and time.time() - t0 < 8:
        pump(0.1)
    report = {"httpState": bool(app.state)}
    pump(1.5)
    shown = {n: w.visible for n, w in app.wins.items()}
    app.gdi.screenshot(os.path.join(out_dir, "desktop.png"))
    win = app.wins["card"]
    hits = {h[0]: h for h in (win.ops.hits if win.ops else [])}
    sc = win.scale

    def lp(name):
        _, x, y, w, h = hits[name]
        px, py = int((x + w / 2.0) * sc), int((y + h / 2.0) * sc)
        return (py << 16) | (px & 0xFFFF)

    for name in ("skip", "yes"):
        if name in hits:
            app._wndproc(app.main, 0x0200, 0, lp(name))         # Maus drueber
            hover = app.ui["hover"]
            app._wndproc(app.main, 0x0201, 0, lp(name))
            app._wndproc(app.main, 0x0202, 0, lp(name))
            report["hover_" + name] = hover
    pump(1.0)
    # Tab-Tabelle: unsichtbar vorgezeichnet, Tab blendet nur ein (2.5.7) - Zeiten messen
    bwin = app.wins["board"]
    app.force_board, app.key_sim = False, False
    pump(0.3)
    hidden_ok = not bwin.visible
    b2 = json.loads(body.decode("utf-8"))
    b2["board"]["allies"][0]["rank"] = "Diamond 1"           # andere Daten -> neue Signatur
    app.set_state(b2, json.dumps(b2).encode("utf-8"))
    pump(BOARD_PREPAINT + 0.5)
    prepainted = bwin.sig is not None and bwin.sig[0] == app.sigs.get("board") and not bwin.visible
    app.key_sim = True
    t1 = time.perf_counter()
    app.refresh()
    show_ms = (time.perf_counter() - t1) * 1000
    shown_on_key = bwin.visible
    app.key_sim = False
    app.refresh()
    gdi2 = Gdi(st.get("lang"))                               # frisch: Bilder noch nicht geladen
    ctx2 = {"strings": st.get("strings") or {}, "lang": st.get("lang"), "preview": False}
    times = {}
    for label in ("cold", "warm", "warm2"):
        if label == "warm2":
            gdi2.prof = {}
        t1 = time.perf_counter()
        gdi2.free(gdi2.render(layout_board(gdi2.measure, ctx2, st["board"]), max(1.0, bwin.scale)))
        times[label] = round((time.perf_counter() - t1) * 1000, 1)
    report["renderProfileMs"] = {k: round(v * 1000, 2) for k, v in sorted(gdi2.prof.items(), key=lambda kv: -kv[1])}
    gdi2.prof = None
    t1 = time.perf_counter()
    gdi2.free(gdi2.render(layout_board(gdi2.measure, ctx2, st["board"]), 1.5))
    times["warm15first"] = round((time.perf_counter() - t1) * 1000, 1)
    report["renderMs"] = times
    report["boardTab"] = {"hiddenAfterRelease": hidden_ok, "prepainted": prepainted, "shownOnKey": shown_on_key,
                          "hiddenAgain": not bwin.visible, "showMs": round(show_ms, 1),
                          "renderMsCold": times["cold"], "renderMsWarm": times["warm"]}
    report.update({"shown": shown, "sizes": {n: w.size for n, w in app.wins.items()},
                   "pos": {n: w.pos for n, w in app.wins.items()}, "scale": sc, "cardHits": sorted(hits),
                   "skipAfterClick": app.ui["skip"], "actions": actions, "errors": app.errors})
    app.stop = True
    srv.shutdown()
    with open(os.path.join(out_dir, "smoke.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report))
    want_action = {"action": "il", "answer": "yes3", "id": (st.get("ask") or {}).get("id")}
    bt = report["boardTab"]
    ok = (report["httpState"] and all(shown.values()) and app.errors == 0
          and {"yes", "no", "skip", "close", "open"} <= set(hits) and report.get("hover_yes") == "yes"
          and want_action in actions
          and bt["hiddenAfterRelease"] and bt["prepainted"] and bt["shownOnKey"] and bt["hiddenAgain"])
    return 0 if ok else 2


def _below_normal():
    """Overlay-Prozess unter normale Prioritaet: Zeichnen soll VALORANT nie Rechenzeit wegnehmen
    (das Einblenden selbst ist nur noch ShowWindow, siehe BOARD_PREPAINT)."""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)       # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def main(argv):
    if len(argv) >= 4 and argv[1] == "--render-test":
        return render_test(argv[2], argv[3])
    if len(argv) >= 4 and argv[1] == "--smoke":
        ctypes.windll.user32.PeekMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint,
                                                      ctypes.c_uint, ctypes.c_uint]
        _dpi_aware(ctypes.windll.user32)
        return smoke_test(argv[2], argv[3])
    cfg = {}
    try:
        with open(argv[1], encoding="utf-8") as fh:
            cfg = json.load(fh)
        _dpi_aware(ctypes.windll.user32)
        _below_normal()
        return App(cfg).run()
    except Exception:
        _log_error(cfg)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
