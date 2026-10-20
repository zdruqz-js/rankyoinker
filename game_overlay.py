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

SH = 18            # Rand fuer den weichen Schatten rund um jedes Panel (logische Pixel)
PANEL_W = 368      # Agentenauswahl + Spielertabelle
CARD_W = 384       # Ergebnis / Instalock-Frage
PAD = 14
ROW_H = 42
ROW_GAP = 3
RANK_W = 142
RADIUS = 16

COL = {
    "bg": "#0e0f13", "tile": "#1c1d23", "white": "#ffffff", "black": "#000000",
    "text": "#f5f5f2", "text2": "#a9a9a2", "text3": "#6d6d67",
    "accent": "#d8b87c", "accent2": "#f3dfb0", "accent_ink": "#1b1408",
    "ally": "#5aa9ff", "enemy": "#ff4d5e", "win": "#3ddc84", "loss": "#ff6b7a", "draw": "#9a9a95",
}
BG_ALPHA = 238
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

# Schriftrollen: (Gewicht, Groesse in logischen Pixeln)
FONTS = {
    "eyebrow": ("semibold", 10), "title": ("semibold", 15.5), "name": ("semibold", 13),
    "tag": ("regular", 11), "sub": ("regular", 11), "rank": ("semibold", 12), "chip": ("semibold", 10.5),
    "big": ("black", 26), "grade": ("black", 25), "value": ("semibold", 14.5), "label": ("semibold", 9),
    "body": ("regular", 12), "btn": ("semibold", 13), "rr": ("black", 17),
}

# Tasten fuer die Spielertabelle (virtuelle Tastencodes)
KEYS = {"tab": 0x09, "capslock": 0x14, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "x1": 0x05, "x2": 0x06}
SIZES = {"s": 0.9, "m": 1.0, "l": 1.15}

RESULT_SHOW = 45.0     # so lange bleibt die Ergebnis-Karte sichtbar (Sekunden, nur sichtbare Zeit)

FALLBACK = {
    "ovl_eyebrow_pregame": "AGENT SELECT", "ovl_eyebrow_board": "LOBBY", "ovl_eyebrow_result": "MATCH RESULT",
    "ovl_team_ally": "YOUR TEAM", "ovl_team_enemy": "ENEMIES", "ovl_team_all": "PLAYERS",
    "ovl_open_report": "Open analysis", "ovl_rank_up": "Ranked up!", "ovl_rank_down": "Ranked down",
    "ovl_il_in": "{name} in {n} s", "ovl_il_locked": "{name} locked", "ovl_hidden": "Hidden",
    "ovl_avg": "Avg {rank}", "ovl_preview": "PREVIEW", "ovl_grade": "Grade",
    "report_victory": "Victory", "report_defeat": "Defeat", "report_draw": "Draw", "report_place": "Place {n} of {of}",
    "report_mvp": "Match MVP", "report_team_mvp": "Team MVP", "pregame_you": "You", "pregame_hovering": "Hovering",
    "instalock_ask_title": "Keep going with {name}?",
    "instalock_ask_body": "Should Instalock lock {name} in the next match too? No answer pauses it.",
    "instalock_ask_skip": "Don't ask for the next {n} matches", "instalock_ask_yes": "Yes", "instalock_ask_no": "No",
    "peak_label": "Peak",
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


def split_riot_id(name):
    s = str(name or "")
    i = s.rfind("#")
    if i > 0:
        return s[:i], s[i:]
    return s, ""


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


def _frame(ops, x, y, w, h):
    """Panel-Grund: Schatten, Glasflaeche, Lichtkante oben, feiner Rand."""
    ops.add("shadow", x, y, w, h, RADIUS, SH - 2, 120)
    ops.add("fill", x, y, w, h, RADIUS, COL["bg"], BG_ALPHA)
    ops.add("vgrad", x, y, w, min(70, h), RADIUS, COL["white"], 14, COL["white"], 0)
    ops.add("stroke", x, y, w, h, RADIUS, COL["white"], 26, 1.0)


def _chip(ops, m, x, y, text, color, align_right=False, fill_alpha=34, h=20):
    w = m(text, "chip") + 16
    if align_right:
        x -= w
    ops.add("fill", x, y, w, h, h / 2.0, color, fill_alpha)
    ops.add("stroke", x, y, w, h, h / 2.0, color, 90, 1.0)
    ops.add("text", x, y, w, h, text, "chip", color, 255, "c", False)
    return w


def _name_tag(ops, m, x, y, w, h, name, tag, col_name):
    tw = m(tag, "tag") if tag else 0
    nw = min(m(name, "name") + 1, max(24, w - tw - 4))
    ops.add("text", x, y, nw + 2, h, name, "name", col_name, 255, "l", False)
    if tag and nw + 4 + tw <= w + 1:
        ops.add("text", x + nw + 4, y + 1, tw + 2, h, tag, "tag", COL["text3"], 255, "l", False)


def _player_row(ops, m, ctx, x, y, w, p, pregame):
    strings, lang = ctx["strings"], ctx.get("lang")
    me = bool(p.get("me"))
    if me:
        ops.add("fill", x, y, w, ROW_H, 11, COL["accent"], 26)
        ops.add("stroke", x, y, w, ROW_H, 11, COL["accent"], 70, 1.0)
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
    # Name
    nx = tx + ts + 10
    nw = x + w - RANK_W - nx - 6
    name, tag = split_riot_id(p.get("name"))
    if p.get("hidden") or not name:
        name, tag = (p.get("agent") or tr(strings, "ovl_hidden")), ""
        _name_tag(ops, m, nx, y + 4, nw, 19, name, "", COL["text2"])
    else:
        _name_tag(ops, m, nx, y + 4, nw, 19, name, tag, COL["text"])
    sub = []
    if me:
        sub.append(tr(strings, "pregame_you"))
    if hovering:
        sub.append(tr(strings, "pregame_hovering"))
    if pregame and p.get("level"):
        sub.append("Lvl %s" % p["level"])
    if not pregame:
        if p.get("kd") not in (None, ""):
            try:
                sub.append("K/D %.2f" % float(p["kd"]))
            except (TypeError, ValueError):
                pass
        if p.get("hs") not in (None, ""):
            try:
                sub.append("HS %d%%" % round(float(p["hs"])))
            except (TypeError, ValueError):
                pass
    ops.add("text", nx, y + 22, nw, 16, "  ·  ".join(sub), "sub",
            COL["accent"] if me else COL["text3"], 255, "l", False)
    # Rang
    rx = x + w - RANK_W
    tier = int(p.get("tier") or 0)
    ranked = tier > 2
    if p.get("rankImg") and ranked:
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
    ops.add("text", rx + 32, y + 22, RANK_W - 36, 16, "  ·  ".join(low), "sub", COL["text3"], 255, "l", False)


def _section(ops, m, ctx, x, y, w, label, color, players, pregame):
    ops.add("fill", x + 2, y + 7, 6, 6, 3, color, 255)
    ops.add("text", x + 14, y, w - 14, 20, label, "eyebrow", color, 255, "l", False)
    at = avg_tier(players)
    if at:
        ops.add("text", x, y, w, 20, tr(ctx["strings"], "ovl_avg", rank=rank_name(at)), "sub",
                COL["text3"], 255, "r", False)
    y += 22
    for p in players:
        _player_row(ops, m, ctx, x, y, w, p, pregame)
        y += ROW_H + ROW_GAP
    return y


def _panel(m, ctx, data, pregame):
    strings, lang = ctx["strings"], ctx.get("lang")
    ops = Ops()
    x0, y0, w = SH, SH, PANEL_W
    items = []
    inner = Ops()
    x, y = x0 + PAD, y0 + PAD
    iw = w - 2 * PAD
    eyebrow = tr(strings, "ovl_eyebrow_pregame" if pregame else "ovl_eyebrow_board")
    if data.get("mode"):
        eyebrow += "  ·  " + upper(str(data["mode"]), lang)
    if ctx.get("preview"):
        eyebrow = tr(strings, "ovl_preview") + "  ·  " + eyebrow
    inner.add("text", x, y, iw - 90, 14, eyebrow, "eyebrow", COL["text3"], 255, "l", False)
    inner.add("text", x, y, iw, 14, "RANKYOINKER", "eyebrow", COL["accent"], 210, "r", False)
    title = data.get("map") or ""
    if data.get("sideText"):
        title = (title + "  ·  " if title else "") + data["sideText"]
    il = data.get("il") if pregame else None
    chip_w = 0
    if il and il.get("text"):
        chip_w = _chip(inner, m, x + iw, y + 17, il["text"], COL["accent"], align_right=True) + 8
    inner.add("text", x, y + 15, iw - chip_w, 24, title or " ", "title", COL["text"], 255, "l", False)
    y += 46
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
        y = _section(inner, m, ctx, x, y, iw, label, color, players[:14], pregame)
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
    ops.add("fill", x, y, 26, 26, 13, COL["black"] if not hover else COL["white"], 110 if not hover else 30)
    c = COL["text"] if hover else COL["text2"]
    ops.add("line", x + 9, y + 9, x + 17, y + 17, c, 255, 1.6)
    ops.add("line", x + 17, y + 9, x + 9, y + 17, c, 255, 1.6)
    ops.hit("close", x, y, 26, 26)


def _button(ops, name, x, y, w, h, text, primary, hover):
    if primary:
        ops.add("vgrad", x, y, w, h, 10, COL["accent2"] if hover else "#ead2a0", 255, COL["accent"], 255)
        ops.add("text", x, y, w, h, text, "btn", COL["accent_ink"], 255, "c", False)
    else:
        ops.add("fill", x, y, w, h, 10, COL["white"], 22 if hover else 12)
        ops.add("stroke", x, y, w, h, 10, COL["white"], 40 if hover else 26, 1.0)
        ops.add("text", x, y, w, h, text, "btn", COL["text"], 255, "c", False)
    ops.hit(name, x, y, w, h)


def layout_card(m, ctx, res, ask, ui):
    """Karte unten rechts: Ergebnis (res) und/oder Instalock-Frage (ask).
    ui: {"hover": Name, "skip": Haken gesetzt?, "frac": Restzeit 0..1 oder None}"""
    strings, lang = ctx["strings"], ctx.get("lang")
    hv = (ui or {}).get("hover")
    ops = Ops()
    inner = Ops()
    x0, y0, w = SH, SH, CARD_W
    P = 16
    y = y0
    if res:
        bh = 108
        inner.add("clip", x0, y0, w, 400, RADIUS)
        if res.get("mapImg"):
            inner.add("image", x0, y0, w, bh, res["mapImg"], 0, 255, True)
        inner.add("hgrad", x0, y0, w, bh, 0, COL["bg"], 245, COL["bg"], 110)
        inner.add("vgrad", x0, y0 + 30, w, bh - 30, 0, COL["bg"], 0, COL["bg"], BG_ALPHA)
        inner.add("unclip")
        kind = res.get("result") or "draw"
        tcol = {"win": COL["win"], "loss": COL["loss"]}.get(kind, COL["draw"])
        eyebrow = tr(strings, "ovl_eyebrow_result")
        if res.get("mode"):
            eyebrow += "  ·  " + upper(str(res["mode"]), lang)
        if ctx.get("preview"):
            eyebrow = tr(strings, "ovl_preview") + "  ·  " + eyebrow
        inner.add("text", x0 + P, y0 + 14, w - 2 * P - 34, 14, eyebrow, "eyebrow", COL["text2"], 255, "l", False)
        title = upper(tr(strings, {"win": "report_victory", "loss": "report_defeat"}.get(kind, "report_draw")), lang)
        score = res.get("score") or {}
        if score.get("place"):
            right = tr(strings, "report_place", n=score["place"], of=score.get("of") or "?")
            inner.add("text", x0 + P, y0 + 36, w - 2 * P, 34, title, "big", tcol, 255, "l", False)
            inner.add("text", x0 + P, y0 + 70, w - 2 * P, 18, right, "body", COL["text"], 255, "l", False)
        else:
            sc = "%s : %s" % (score.get("ally", "?"), score.get("enemy", "?"))
            sw = m(sc, "big") + 4
            inner.add("text", x0 + P, y0 + 36, w - 2 * P - sw - 8, 34, title, "big", tcol, 255, "l", False)
            inner.add("text", x0 + w - P - sw, y0 + 36, sw, 34, sc, "big", COL["text"], 255, "r", False)
            inner.add("text", x0 + P, y0 + 72, w - 2 * P, 18, res.get("map") or "", "body", COL["text2"], 255, "l", False)
        _close_btn(inner, x0 + w - 10 - 26, y0 + 8, hv == "close")
        y = y0 + bh + 6
        # Agent, Note, Leistung
        inner.add("fill", x0 + P, y, 48, 48, 12, COL["tile"], 255)
        if res.get("agentImg"):
            inner.add("image", x0 + P, y, 48, 48, res["agentImg"], 12, 255, False)
        g = res.get("grade") or "-"
        c1, c2 = GRADE_COLORS.get(g, ("#3a3a40", "#3a3a40"))
        gx = x0 + P + 58
        inner.add("vgrad", gx, y, 48, 48, 12, c1, 255, c2, 255)
        inner.add("text", gx, y + 1, 48, 48, g, "grade", "#0b0b0c", 255, "c", False)
        tx = gx + 60
        tw = x0 + w - P - tx
        head = tr(strings, "ovl_grade") + " " + g
        hw = min(m(head, "title") + 2, tw)
        inner.add("text", tx, y + 4, hw, 22, head, "title", COL["text"], 255, "l", False)
        if res.get("mvp"):
            label = tr(strings, "report_team_mvp" if res["mvp"] == "team" else "report_mvp")
            if hw + 8 + m(label, "chip") + 16 <= tw:
                _chip(inner, m, tx + hw + 8, y + 5, label, COL["accent"])
        inner.add("text", tx, y + 27, tw, 18, res.get("perf") or "", "body", COL["text2"], 255, "l", False)
        y += 60
        # Werte
        stats = res.get("stats") or []
        if stats:
            gap = 8
            sw = (w - 2 * P - gap * (len(stats) - 1)) / float(len(stats))
            for i, (label, value) in enumerate(stats):
                sx = x0 + P + i * (sw + gap)
                inner.add("fill", sx, y, sw, 46, 10, COL["white"], 9)
                inner.add("text", sx, y + 6, sw, 20, str(value), "value", COL["text"], 255, "c", False)
                inner.add("text", sx, y + 27, sw, 13, label, "label", COL["text3"], 255, "c", False)
            y += 54
        # RR
        rr = res.get("rr")
        if rr:
            inner.add("fill", x0 + P, y, w - 2 * P, 44, 10, COL["white"], 7)
            tier = int(rr.get("tier") or 0)
            if rr.get("img"):
                inner.add("image", x0 + P + 9, y + 7, 30, 30, rr["img"], 0, 255, False)
            line1 = rank_name(tier) + ("  ·  %s RR" % rr["rr"] if rr.get("rr") is not None else "")
            inner.add("text", x0 + P + 48, y + 5, w - 2 * P - 150, 18, line1, "rank", tier_color(tier), 255, "l", False)
            before = int(rr.get("before") or 0)
            if before and tier > before:
                inner.add("text", x0 + P + 48, y + 23, w - 2 * P - 150, 15, tr(strings, "ovl_rank_up"), "sub",
                          COL["accent"], 255, "l", False)
            elif before and tier < before:
                inner.add("text", x0 + P + 48, y + 23, w - 2 * P - 150, 15, tr(strings, "ovl_rank_down"), "sub",
                          COL["loss"], 255, "l", False)
            earned = rr.get("earned")
            if earned is not None:
                txt = ("+%d RR" % earned) if earned > 0 else ("−%d RR" % abs(earned)) if earned < 0 else "±0 RR"
                col = COL["win"] if earned > 0 else COL["loss"] if earned < 0 else COL["text2"]
                inner.add("text", x0 + w - P - 112, y, 100, 44, txt, "rr", col, 255, "r", False)
            y += 52
        _button(inner, "open", x0 + P, y, w - 2 * P, 34, tr(strings, "ovl_open_report"), False, hv == "open")
        y += 34 + P
    if ask:
        if res:
            inner.add("line", x0 + P, y, x0 + w - P, y, COL["white"], 24, 1.0)
            y += 14
        else:
            y += P
            _close_btn(inner, x0 + w - 12 - 26, y0 + 10, hv == "close")
        inner.add("text", x0 + P, y, w - 2 * P - 34, 14, "INSTALOCK", "eyebrow", COL["accent"], 255, "l", False)
        y += 20
        inner.add("fill", x0 + P, y, 46, 46, 12, COL["tile"], 255)
        if ask.get("img"):
            inner.add("image", x0 + P, y, 46, 46, ask["img"], 12, 255, False)
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
            inner.add("fill", bx, by, 16, 16, 4.5, COL["accent"], 255)
            inner.add("line", bx + 4, by + 8.5, bx + 7, by + 11.5, COL["accent_ink"], 255, 2.0)
            inner.add("line", bx + 7, by + 11.5, bx + 12.5, by + 5, COL["accent_ink"], 255, 2.0)
        else:
            inner.add("stroke", bx, by, 16, 16, 4.5, COL["white"], 120 if hv == "skip" else 70, 1.3)
        inner.add("text", x0 + P + 26, y, w - 2 * P - 26, 26,
                  tr(strings, "instalock_ask_skip", n=ask.get("skipN") or 3), "body",
                  COL["text"] if hv == "skip" else COL["text2"], 255, "l", False)
        y += 34
        bw = (w - 2 * P - 10) / 2.0
        _button(inner, "no", x0 + P, y, bw, 36, tr(strings, "instalock_ask_no"), False, hv == "no")
        _button(inner, "yes", x0 + P + bw + 10, y, bw, 36, tr(strings, "instalock_ask_yes"), True, hv == "yes")
        y += 36 + P
    h = y - y0
    frac = (ui or {}).get("frac")
    _frame(ops, x0, y0, w, h)
    ops.items.extend(inner.items)
    if frac is not None and res and not ask:
        ops.add("fill", x0 + RADIUS, y0 + h - 3, (w - 2 * RADIUS) * max(0.0, min(1.0, frac)), 2, 1, COL["accent"], 170)
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

# Schriftfamilien je Gewicht: (Name, Stil) - Stil 1 = fett, falls es den Schnitt nicht gibt
_FAMILIES = {"regular": (("Segoe UI", 0),), "semibold": (("Segoe UI Semibold", 0), ("Segoe UI", 1)),
             "black": (("Segoe UI Black", 0), ("Segoe UI", 1))}
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
            ("GdipSetCompositingQuality", [vp, i]),
            ("GdipScaleWorldTransform", [vp, f, f, i]),
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
        self._mbmp, self._mg = ctypes.c_void_p(), ctypes.c_void_p()
        self.gdip.GdipCreateBitmapFromScan0(8, 8, 0, PARGB, None, ctypes.byref(self._mbmp))
        self.gdip.GdipGetImageGraphicsContext(self._mbmp, ctypes.byref(self._mg))
        self.gdip.GdipSetTextRenderingHint(self._mg, 4)

    # ---- Schriften ----
    def _family(self, name):
        if name not in self._families:
            fam = ctypes.c_void_p()
            ok = self.gdip.GdipCreateFontFamilyFromName(name, None, ctypes.byref(fam)) == 0 and fam.value
            self._families[name] = fam if ok else None
        return self._families[name]

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
            cands += [(n, style) for n in _SCRIPT_FAMILIES[script]]
        cands += list(_FAMILIES[weight]) + [("Segoe UI", 0), ("Arial", 0), ("Tahoma", 0)]
        font = None
        for name, style in cands:
            fam = self._family(name)
            if not fam:
                continue
            f = ctypes.c_void_p()
            if self.gdip.GdipCreateFont(fam, ctypes.c_float(size), style, 2, ctypes.byref(f)) == 0 and f.value:
                font = f
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

    def measure(self, text, key):
        if not text:
            return 0.0
        layout, box = _RECTF(0, 0, 4000, 400), _RECTF()
        self.gdip.GdipMeasureString(self._mg, text, -1, self.font(key, text), ctypes.byref(layout),
                                    self.fmt("l", False, True), ctypes.byref(box), None, None)
        return float(box.Width)

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
            for p, v in list(self._images.items())[:40]:
                if v:
                    self.gdip.GdipDisposeImage(v[0])
                del self._images[p]
        if out or os.path.isfile(path):
            self._images[path] = out            # fehlt die Datei noch (Download laeuft), spaeter erneut
        return out

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

    def _op(self, g, it, clips):
        gd = self.gdip
        kind = it[0]
        if kind == "fill":
            _, x, y, w, h, r, color, alpha = it
            if w <= 0 or h <= 0:
                return
            path = self._rrect(x, y, w, h, r)
            self._fill_path(g, path, _argb(color, alpha))
            gd.GdipDeletePath(path)
        elif kind in ("vgrad", "hgrad"):
            _, x, y, w, h, r, c1, a1, c2, a2 = it
            if w <= 0 or h <= 0:
                return
            p1 = _POINTF(x, y)
            p2 = _POINTF(x, y + h) if kind == "vgrad" else _POINTF(x + w, y)
            brush = ctypes.c_void_p()
            gd.GdipCreateLineBrush(ctypes.byref(p1), ctypes.byref(p2), _argb(c1, a1), _argb(c2, a2), 3,
                                   ctypes.byref(brush))
            path = self._rrect(x, y, w, h, r)
            gd.GdipFillPath(g, brush, path)
            gd.GdipDeletePath(path)
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
            _, x, y, w, h, r, spread, alpha = it
            n = max(1, int(spread))
            dy = spread * 0.3
            for i in range(n, 0, -1):
                a = alpha * ((n - i + 1) / float(n)) ** 2 / n * 2.4
                path = self._rrect(x - i, y - i + dy, w + 2 * i, h + 2 * i, r + i)
                self._fill_path(g, path, _argb(COL["black"], a))
                gd.GdipDeletePath(path)
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
        gd.GdipScaleWorldTransform(g, ctypes.c_float(scale), ctypes.c_float(scale), 0)
        clips = []
        for it in ops.items:
            self._op(g, it, clips)
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
        self.top_at = 0.0

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
        full = (sig, mon["scale"])
        if win.sig != full:
            ops = ops_fn()
            w, h = int(math.ceil(ops.w * mon["scale"])), int(math.ceil(ops.h * mon["scale"]))
            x, y = self._place(name, mon, w, h)
            win.update(ops, mon["scale"], x, y)
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
                vk = KEYS.get(settings.get("key") or "tab", 0x09)
                down = bool(self.gdi.user32.GetAsyncKeyState(vk) & 0x8000)
                if vk == 0x09 and self.gdi.user32.GetAsyncKeyState(0x12) & 0x8000:
                    down = False                                       # Alt+Tab
                if down or preview or self.forced:
                    want["board"] = True
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
        sc = (win.sig or (None, 1.0))[1] or 1.0
        return win.ops.hit_test(x / sc, y / sc)

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
    """Alle Panels als PNG (echtes GDI+), je in zwei Groessen und mit Hover-Zustaenden."""
    os.makedirs(out_dir, exist_ok=True)
    st = _load_test_state(state_path)
    gdi = Gdi(st.get("lang"))
    ctx = {"strings": st.get("strings") or {}, "lang": st.get("lang"), "preview": False}
    m = gdi.measure
    jobs = [("pregame", lambda: layout_pregame(m, ctx, st["pregame"])),
            ("board", lambda: layout_board(m, ctx, st["board"])),
            ("card", lambda: layout_card(m, ctx, st["result"], st["ask"], {"hover": "yes", "skip": True})),
            ("card-result", lambda: layout_card(m, ctx, st["result"], None, {"hover": "open", "frac": 0.6})),
            ("card-ask", lambda: layout_card(m, ctx, None, st["ask"], {"hover": None, "skip": False}))]
    for name, fn in jobs:
        for scale in (1.0, 1.5):
            ops = fn()
            gdi.free(gdi.render(ops, scale, os.path.join(out_dir, "%s@%sx.png" % (name, scale))))
    for lang_st in st.get("extraLangs") or []:
        ctx2 = {"strings": lang_st["strings"], "lang": lang_st["lang"], "preview": False}
        gdi.lang = lang_st["lang"]
        ops = layout_card(m, ctx2, st["result"], st["ask"], {"hover": None, "skip": False})
        gdi.free(gdi.render(ops, 1.0, os.path.join(out_dir, "card-%s.png" % lang_st["lang"])))
    return 0


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
    sc = (win.sig or (None, 1.0))[1] or 1.0

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
    report.update({"shown": shown, "sizes": {n: w.size for n, w in app.wins.items()},
                   "pos": {n: w.pos for n, w in app.wins.items()}, "scale": sc, "cardHits": sorted(hits),
                   "skipAfterClick": app.ui["skip"], "actions": actions, "errors": app.errors})
    app.stop = True
    srv.shutdown()
    with open(os.path.join(out_dir, "smoke.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report))
    want_action = {"action": "il", "answer": "yes3", "id": (st.get("ask") or {}).get("id")}
    ok = (report["httpState"] and all(shown.values()) and app.errors == 0
          and {"yes", "no", "skip", "close", "open"} <= set(hits) and report.get("hover_yes") == "yes"
          and want_action in actions)
    return 0 if ok else 2


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
        return App(cfg).run()
    except Exception:
        _log_error(cfg)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
