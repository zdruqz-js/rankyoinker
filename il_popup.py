"""Kleines Fenster ueber VALORANT: "Weiter mit <Agent>?" (Instalock, ab 2.5.5).

Der Dienst (vry_log_server.py, _il_popup_open) startet es als eigenen Prozess:
    pythonw.exe il_popup.py <cfg.json>
Eigener Prozess, damit ein Fehler hier nie den Dienst mitreisst. Nur Windows-Bordmittel
ueber ctypes (user32/gdi32/gdiplus) - das eingebettete Python hat kein tkinter.

- nimmt VALORANT nie den Fokus (WS_EX_NOACTIVATE, WM_MOUSEACTIVATE -> MA_NOACTIVATE)
- liegt oben (topmost, alle 2 s erneuert), unten rechts auf dem Bildschirm des Spiels
- Antwort = Exit-Code (EXIT_*), der Dienst liest ihn ueber Popen.poll()
- schliesst sich selbst, wenn der Dienst weg ist oder nach cfg["timeout"] Sekunden
Im exklusiven Vollbild ist es nicht zu sehen - dann bleibt die Frage im Overlay.
"""
import ctypes
import json
import sys
import time
import traceback
from ctypes import wintypes

EXIT_YES, EXIT_YES_SKIP, EXIT_NO, EXIT_CLOSED, EXIT_ERROR = 10, 13, 20, 30, 1

# Masse in logischen Pixeln (96 DPI), werden mit der Bildschirm-Skalierung multipliziert
W, H, PAD = 400, 204, 18
ICON = 56
TEXT_X = PAD + ICON + 14
CLOSE = (W - 14 - 24, 12, 24, 24)
BODY_H = 54                      # 3 Zeilen - Franzoesisch/Polnisch mit langen Agentennamen
SKIP_ROW = (PAD - 4, 120, W - 2 * PAD + 8, 26)
BTN_Y, BTN_H, BTN_GAP = 152, 34, 10
BTN_W = (W - 2 * PAD - BTN_GAP) // 2
BTN_NO = (PAD, BTN_Y, BTN_W, BTN_H)
BTN_YES = (PAD + BTN_W + BTN_GAP, BTN_Y, BTN_W, BTN_H)
RADIUS = 8

COL = {
    "bg": "#121216", "border": "#d8b87c", "eyebrow": "#d8b87c", "title": "#f2f2f4", "body": "#a3a3ad",
    "tile": "#1e1e25", "box": "#5a5a66", "box_hover": "#8a8a96", "check_bg": "#d8b87c", "check": "#121216",
    "no_bg": "#22222a", "no_bg_hover": "#2c2c36", "no_line": "#34343e", "no_text": "#e6e6ea",
    "yes_bg": "#d8b87c", "yes_bg_hover": "#e6ca94", "yes_text": "#17140e",
    "close": "#7c7c88", "close_hover": "#d0d0d8", "close_bg_hover": "#22222a",
}


def hit_test(x, y, scale):
    """Welcher Bereich liegt unter dem Mauszeiger (physische Pixel)?"""
    lx, ly = x / scale, y / scale
    for name, (rx, ry, rw, rh) in (("close", CLOSE), ("yes", BTN_YES), ("no", BTN_NO), ("skip", SKIP_ROW)):
        if rx <= lx < rx + rw and ry <= ly < ry + rh:
            return name
    return None


def answer_code(name, skip_checked):
    """Klick -> Exit-Code, None = nur Haken umschalten."""
    if name == "yes":
        return EXIT_YES_SKIP if skip_checked else EXIT_YES
    if name == "no":
        return EXIT_NO
    if name == "close":
        return EXIT_CLOSED
    return None


def _argb(hex_color, alpha=255):
    v = int(hex_color.lstrip("#"), 16)
    return ctypes.c_uint32((alpha << 24) | v)


def _colorref(hex_color):
    v = int(hex_color.lstrip("#"), 16)
    r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
    return (b << 16) | (g << 8) | r


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint), ("pt", _POINT)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", ctypes.c_void_p),
                ("dwHoverTime", wintypes.DWORD)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


def _prototypes(user32, gdi32, kernel32, gdip):
    """Explizite argtypes/restype: Handles sind auf x64 64-bit (siehe _tray_setup_prototypes im Dienst)."""
    vp, ui, i, f = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_float
    sz, ssz = ctypes.c_size_t, ctypes.c_ssize_t
    P = ctypes.POINTER
    kernel32.GetModuleHandleW.restype, kernel32.GetModuleHandleW.argtypes = vp, [ctypes.c_wchar_p]
    kernel32.OpenProcess.restype, kernel32.OpenProcess.argtypes = vp, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype, kernel32.WaitForSingleObject.argtypes = wintypes.DWORD, [vp, wintypes.DWORD]
    user32.RegisterClassW.restype, user32.RegisterClassW.argtypes = wintypes.ATOM, [vp]
    user32.CreateWindowExW.restype = vp
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD,
                                       i, i, i, i, vp, vp, vp, vp]
    user32.DefWindowProcW.restype, user32.DefWindowProcW.argtypes = ssz, [vp, ui, sz, ssz]
    user32.DestroyWindow.argtypes = [vp]
    user32.ShowWindow.argtypes = [vp, i]
    user32.UpdateWindow.argtypes = [vp]
    user32.InvalidateRect.argtypes = [vp, vp, wintypes.BOOL]
    user32.BeginPaint.restype, user32.BeginPaint.argtypes = vp, [vp, P(_PAINTSTRUCT)]
    user32.EndPaint.argtypes = [vp, P(_PAINTSTRUCT)]
    user32.GetMessageW.restype, user32.GetMessageW.argtypes = wintypes.BOOL, [P(_MSG), vp, ui, ui]
    user32.TranslateMessage.argtypes = [P(_MSG)]
    user32.DispatchMessageW.restype, user32.DispatchMessageW.argtypes = ssz, [P(_MSG)]
    user32.PostQuitMessage.argtypes = [i]
    user32.SetTimer.restype, user32.SetTimer.argtypes = sz, [vp, sz, ui, vp]
    user32.SetWindowPos.argtypes = [vp, vp, i, i, i, i, ui]
    user32.GetForegroundWindow.restype = vp
    user32.MonitorFromWindow.restype, user32.MonitorFromWindow.argtypes = vp, [vp, wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [vp, P(_MONITORINFO)]
    user32.LoadCursorW.restype, user32.LoadCursorW.argtypes = vp, [vp, vp]
    user32.SetCursor.restype, user32.SetCursor.argtypes = vp, [vp]
    user32.TrackMouseEvent.argtypes = [P(_TRACKMOUSEEVENT)]
    user32.SetWindowRgn.argtypes = [vp, vp, wintypes.BOOL]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = vp, [vp]
    gdi32.CreateCompatibleBitmap.restype, gdi32.CreateCompatibleBitmap.argtypes = vp, [vp, i, i]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = vp, [vp, vp]
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.BitBlt.argtypes = [vp, i, i, i, i, vp, i, i, wintypes.DWORD]
    gdi32.SetBkMode.argtypes = [vp, i]
    gdi32.SetTextColor.restype, gdi32.SetTextColor.argtypes = wintypes.DWORD, [vp, wintypes.DWORD]
    gdi32.SetTextCharacterExtra.argtypes = [vp, i]
    gdi32.CreateFontW.restype = vp
    gdi32.CreateFontW.argtypes = [i, i, i, i, i] + [wintypes.DWORD] * 8 + [ctypes.c_wchar_p]
    gdi32.CreateRoundRectRgn.restype, gdi32.CreateRoundRectRgn.argtypes = vp, [i, i, i, i, i, i]
    user32.DrawTextW.argtypes = [vp, ctypes.c_wchar_p, i, P(wintypes.RECT), ui]
    gdip.GdiplusStartup.argtypes = [P(sz), P(_GdiplusStartupInput), vp]
    gdip.GdipCreateFromHDC.argtypes = [vp, P(vp)]
    gdip.GdipDeleteGraphics.argtypes = [vp]
    gdip.GdipSetSmoothingMode.argtypes = [vp, i]
    gdip.GdipSetPixelOffsetMode.argtypes = [vp, i]
    gdip.GdipSetInterpolationMode.argtypes = [vp, i]
    gdip.GdipCreateSolidFill.argtypes = [ctypes.c_uint32, P(vp)]
    gdip.GdipDeleteBrush.argtypes = [vp]
    gdip.GdipCreatePen1.argtypes = [ctypes.c_uint32, f, i, P(vp)]
    gdip.GdipSetPenStartCap.argtypes = [vp, i]
    gdip.GdipSetPenEndCap.argtypes = [vp, i]
    gdip.GdipDeletePen.argtypes = [vp]
    gdip.GdipCreatePath.argtypes = [i, P(vp)]
    gdip.GdipAddPathArc.argtypes = [vp, f, f, f, f, f, f]
    gdip.GdipClosePathFigure.argtypes = [vp]
    gdip.GdipDeletePath.argtypes = [vp]
    gdip.GdipFillPath.argtypes = [vp, vp, vp]
    gdip.GdipDrawPath.argtypes = [vp, vp, vp]
    gdip.GdipDrawLine.argtypes = [vp, vp, f, f, f, f]
    gdip.GdipSetClipPath.argtypes = [vp, vp, i]
    gdip.GdipResetClip.argtypes = [vp]
    gdip.GdipLoadImageFromFile.argtypes = [ctypes.c_wchar_p, P(vp)]
    gdip.GdipDrawImageRectI.argtypes = [vp, vp, i, i, i, i]
    gdip.GdipDisposeImage.argtypes = [vp]


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


def run(cfg):
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    gdip = ctypes.windll.gdiplus
    _prototypes(user32, gdi32, kernel32, gdip)
    _dpi_aware(user32)

    token = ctypes.c_size_t()
    gsi = _GdiplusStartupInput(1, None, False, False)
    gdip.GdiplusStartup(ctypes.byref(token), ctypes.byref(gsi), None)

    hmon = user32.MonitorFromWindow(user32.GetForegroundWindow(), 1)    # MONITOR_DEFAULTTOPRIMARY
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    scale = _monitor_dpi(hmon) / 96.0
    S = lambda v: int(round(v * scale))
    win_w, win_h = S(W), S(H)
    x = mi.rcWork.right - win_w - S(24)
    y = mi.rcWork.bottom - win_h - S(24)

    face = cfg.get("font") or "Segoe UI"
    mkfont = lambda px, weight: gdi32.CreateFontW(-S(px), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)
    fonts = {"eyebrow": mkfont(11, 700), "title": mkfont(17, 600), "body": mkfont(12.5, 400),
             "skip": mkfont(12.5, 400), "btn": mkfont(13.5, 600)}

    icon = ctypes.c_void_p()
    if cfg.get("icon"):
        if gdip.GdipLoadImageFromFile(cfg["icon"], ctypes.byref(icon)) != 0:
            icon = ctypes.c_void_p()

    parent = None
    if cfg.get("parentPid"):
        parent = kernel32.OpenProcess(0x00100000, False, int(cfg["parentPid"]))     # SYNCHRONIZE
    deadline = time.time() + float(cfg.get("timeout") or 900)

    st = {"hover": None, "pressed": None, "skip": False, "tracking": False, "result": EXIT_CLOSED, "hwnd": None}
    hand = user32.LoadCursorW(None, ctypes.c_void_p(32649))       # IDC_HAND

    def rrect(x0, y0, w, h, r):
        path = ctypes.c_void_p()
        gdip.GdipCreatePath(0, ctypes.byref(path))
        d = float(r * 2)
        gdip.GdipAddPathArc(path, x0, y0, d, d, 180.0, 90.0)
        gdip.GdipAddPathArc(path, x0 + w - d, y0, d, d, 270.0, 90.0)
        gdip.GdipAddPathArc(path, x0 + w - d, y0 + h - d, d, d, 0.0, 90.0)
        gdip.GdipAddPathArc(path, x0, y0 + h - d, d, d, 90.0, 90.0)
        gdip.GdipClosePathFigure(path)
        return path

    def fill(g, path, color, alpha=255):
        brush = ctypes.c_void_p()
        gdip.GdipCreateSolidFill(_argb(color, alpha), ctypes.byref(brush))
        gdip.GdipFillPath(g, brush, path)
        gdip.GdipDeleteBrush(brush)

    def outline(g, path, color, alpha, width):
        pen = ctypes.c_void_p()
        gdip.GdipCreatePen1(_argb(color, alpha), float(width), 2, ctypes.byref(pen))   # UnitPixel
        gdip.GdipDrawPath(g, pen, path)
        gdip.GdipDeletePen(pen)

    def line(g, color, width, x1, y1, x2, y2):
        pen = ctypes.c_void_p()
        gdip.GdipCreatePen1(_argb(color), float(width), 2, ctypes.byref(pen))
        gdip.GdipSetPenStartCap(pen, 2)            # LineCapRound
        gdip.GdipSetPenEndCap(pen, 2)
        gdip.GdipDrawLine(g, pen, float(x1), float(y1), float(x2), float(y2))
        gdip.GdipDeletePen(pen)

    def shapes(g):
        hv = st["hover"]
        bg = rrect(0.0, 0.0, float(win_w), float(win_h), S(RADIUS))
        fill(g, bg, COL["bg"])
        gdip.GdipDeletePath(bg)
        edge = rrect(0.5, 0.5, win_w - 1.0, win_h - 1.0, S(RADIUS))
        outline(g, edge, COL["border"], 110, max(1, S(1)))
        gdip.GdipDeletePath(edge)
        # Agentenbild in einer abgerundeten Kachel
        tile = rrect(float(S(PAD)), float(S(PAD)), float(S(ICON)), float(S(ICON)), S(12))
        fill(g, tile, COL["tile"])
        if icon.value:
            gdip.GdipSetClipPath(g, tile, 0)
            gdip.GdipDrawImageRectI(g, icon, S(PAD), S(PAD), S(ICON), S(ICON))
            gdip.GdipResetClip(g)
        gdip.GdipDeletePath(tile)
        # Schliessen
        cx, cy, cw, ch = CLOSE
        if hv == "close":
            circ = rrect(float(S(cx)), float(S(cy)), float(S(cw)), float(S(ch)), S(cw) / 2.0)
            fill(g, circ, COL["close_bg_hover"])
            gdip.GdipDeletePath(circ)
        c = COL["close_hover"] if hv == "close" else COL["close"]
        m = 8
        line(g, c, max(1.5, 1.6 * scale), S(cx + m), S(cy + m), S(cx + cw - m), S(cy + ch - m))
        line(g, c, max(1.5, 1.6 * scale), S(cx + cw - m), S(cy + m), S(cx + m), S(cy + ch - m))
        # Haken "nicht fragen"
        bx, by = PAD, SKIP_ROW[1] + 5
        box = rrect(float(S(bx)), float(S(by)), float(S(16)), float(S(16)), S(4))
        if st["skip"]:
            fill(g, box, COL["check_bg"])
            line(g, COL["check"], max(1.5, 2 * scale), S(bx + 4), S(by + 8.5), S(bx + 7), S(by + 11.5))
            line(g, COL["check"], max(1.5, 2 * scale), S(bx + 7), S(by + 11.5), S(bx + 12.5), S(by + 5))
        else:
            outline(g, box, COL["box_hover"] if hv == "skip" else COL["box"], 255, max(1, 1.4 * scale))
        gdip.GdipDeletePath(box)
        # Knoepfe
        for name, (rx, ry, rw, rh) in (("no", BTN_NO), ("yes", BTN_YES)):
            p = rrect(float(S(rx)), float(S(ry)), float(S(rw)), float(S(rh)), S(9))
            if name == "yes":
                fill(g, p, COL["yes_bg_hover"] if hv == "yes" else COL["yes_bg"])
            else:
                fill(g, p, COL["no_bg_hover"] if hv == "no" else COL["no_bg"])
                outline(g, p, COL["no_line"], 255, max(1, S(1)))
            gdip.GdipDeletePath(p)

    def text(hdc, key, s, color, rect, flags):
        gdi32.SelectObject(hdc, fonts[key])
        gdi32.SetTextColor(hdc, _colorref(color))
        r = wintypes.RECT(S(rect[0]), S(rect[1]), S(rect[0] + rect[2]), S(rect[1] + rect[3]))
        user32.DrawTextW(hdc, s, -1, ctypes.byref(r), flags | 0x800)          # DT_NOPREFIX

    def paint(hwnd):
        ps = _PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, win_w, win_h)
        old = gdi32.SelectObject(mem, bmp)
        g = ctypes.c_void_p()
        gdip.GdipCreateFromHDC(mem, ctypes.byref(g))
        gdip.GdipSetSmoothingMode(g, 4)            # AntiAlias
        gdip.GdipSetPixelOffsetMode(g, 4)          # Half
        gdip.GdipSetInterpolationMode(g, 7)        # HighQualityBicubic
        shapes(g)
        gdip.GdipDeleteGraphics(g)                 # erst GDI+ fertig, dann GDI-Text darueber
        gdi32.SetBkMode(mem, 1)                    # TRANSPARENT
        tw = CLOSE[0] - 6 - TEXT_X
        gdi32.SetTextCharacterExtra(mem, S(1.4))
        text(mem, "eyebrow", cfg.get("eyebrow") or "INSTALOCK", COL["eyebrow"], (TEXT_X, PAD - 1, tw, 16), 0x20 | 0x8000)
        gdi32.SetTextCharacterExtra(mem, 0)
        text(mem, "title", cfg.get("title") or "", COL["title"], (TEXT_X, 33, tw, 26), 0x20 | 0x8000)
        text(mem, "body", cfg.get("body") or "", COL["body"], (TEXT_X, 60, W - PAD - TEXT_X, BODY_H), 0x10)
        text(mem, "skip", cfg.get("skip") or "", COL["title"] if st["hover"] == "skip" else COL["body"],
             (PAD + 26, SKIP_ROW[1], W - 2 * PAD - 26, SKIP_ROW[3]), 0x20 | 0x4 | 0x8000)
        text(mem, "btn", cfg.get("no") or "Nein", COL["no_text"], BTN_NO, 0x1 | 0x4 | 0x20)
        text(mem, "btn", cfg.get("yes") or "Ja", COL["yes_text"], BTN_YES, 0x1 | 0x4 | 0x20)
        gdi32.BitBlt(hdc, 0, 0, win_w, win_h, mem, 0, 0, 0x00CC0020)       # SRCCOPY
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    def finish(hwnd, code):
        st["result"] = code
        user32.DestroyWindow(hwnd)

    def xy(lparam):
        return ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == 0x000F:                      # WM_PAINT
                paint(hwnd)
                return 0
            if msg == 0x0014:                      # WM_ERASEBKGND
                return 1
            if msg == 0x0021:                      # WM_MOUSEACTIVATE
                return 3                           # MA_NOACTIVATE - VALORANT behaelt den Fokus
            if msg == 0x0200:                      # WM_MOUSEMOVE
                h = hit_test(*xy(lparam), scale)
                if not st["tracking"]:
                    tme = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), 0x2, hwnd, 0)     # TME_LEAVE
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    st["tracking"] = True
                if h != st["hover"]:
                    st["hover"] = h
                    user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x02A3:                      # WM_MOUSELEAVE
                st["tracking"] = False
                if st["hover"]:
                    st["hover"] = None
                    user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x0020:                      # WM_SETCURSOR
                if st["hover"]:
                    user32.SetCursor(hand)
                    return 1
            if msg == 0x0201:                      # WM_LBUTTONDOWN
                st["pressed"] = hit_test(*xy(lparam), scale)
                return 0
            if msg == 0x0202:                      # WM_LBUTTONUP
                h = hit_test(*xy(lparam), scale)
                if h and h == st["pressed"]:
                    code = answer_code(h, st["skip"])
                    if code is None:
                        st["skip"] = not st["skip"]
                        user32.InvalidateRect(hwnd, None, False)
                    else:
                        finish(hwnd, code)
                st["pressed"] = None
                return 0
            if msg == 0x0113:                      # WM_TIMER
                if parent and kernel32.WaitForSingleObject(parent, 0) == 0:
                    finish(hwnd, EXIT_CLOSED)      # Dienst beendet -> nicht stehen bleiben
                elif time.time() > deadline:
                    finish(hwnd, EXIT_CLOSED)
                else:                              # Spiele holen sich gern wieder nach vorn
                    user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                return 0
            if msg == 0x0010:                      # WM_CLOSE
                finish(hwnd, EXIT_CLOSED)
                return 0
            if msg == 0x0002:                      # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            _log_error(cfg)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)                         # Referenz halten, sonst ruft Windows ins Leere
    win11 = sys.getwindowsversion().build >= 22000
    hinst = kernel32.GetModuleHandleW(None)
    wc = _WNDCLASSW()
    wc.style = 0x00020000 if win11 else 0           # CS_DROPSHADOW (Win10: Fensterform per Region)
    wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))     # IDC_ARROW
    wc.lpszClassName = "RankYoinkerInstalockAsk"
    user32.RegisterClassW(ctypes.byref(wc))
    ex = 0x00000008 | 0x00000080 | 0x08000000       # TOPMOST | TOOLWINDOW | NOACTIVATE
    hwnd = user32.CreateWindowExW(ex, wc.lpszClassName, "RankYoinker", 0x80000000,     # WS_POPUP
                                  x, y, win_w, win_h, None, None, hinst, None)
    if not hwnd:
        raise OSError("CreateWindowExW fehlgeschlagen")
    st["hwnd"] = hwnd
    if win11:
        try:
            pref = ctypes.c_int(2)                  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4)
        except Exception:
            pass
    else:
        r = S(RADIUS) * 2
        user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(0, 0, win_w + 1, win_h + 1, r, r), True)
    user32.ShowWindow(hwnd, 4)                      # SW_SHOWNOACTIVATE
    user32.UpdateWindow(hwnd)
    user32.SetTimer(hwnd, 1, 2000, None)

    msg = _MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if icon.value:
        gdip.GdipDisposeImage(icon)
    return st["result"]


def _log_error(cfg):
    try:
        path = (cfg or {}).get("errFile")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc()[-2000:] + "\n")
    except Exception:
        pass


def main(argv):
    cfg = {}
    try:
        with open(argv[1], encoding="utf-8") as fh:
            cfg = json.load(fh)
        return run(cfg)
    except Exception:
        _log_error(cfg)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv))
