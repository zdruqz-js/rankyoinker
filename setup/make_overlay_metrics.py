"""Zeichenbreiten und Kerning der Overlay-Schriften fuer game_overlay.py (seit 2.5.6-dev1).

GDI+ setzt Text ohne Kerning (es liest nur die alte 'kern'-Tabelle, Inter/Geist haben ihr Kerning
nur in GPOS) und ohne Laufweite (letter-spacing). Darum sah die Schrift im Spiel-Overlay lockerer
und unruhiger aus als auf der Website. game_overlay.py setzt die Zeichen deshalb selbst
(GdipDrawDriverString mit eigenen Positionen) und braucht dafuer diese Werte.

    python make_overlay_metrics.py ../fonts            (nach make_overlay_fonts.py laufen lassen)

Schreibt fonts/metrics.json: je Schriftfamilie (Name wie in der Datei, z. B. "RY Inter Medium")
  upm   - Einheiten pro em
  adv   - Zeichenbreite je Unicode-Zeichen (Dezimalzahl als Schluessel)
  kern  - die PairPos-Untertabellen des Merkmals 'kern' in Originalreihenfolge, nur fuer Zeichen
          der Schrift: {"t": 1, "cov": [...], "p": {"a": {"b": wert}}} bzw.
          {"t": 2, "cov": [...], "c1": {zeichen: klasse}, "c2": {zeichen: klasse}, "m": {"k1,k2": wert}}
Auswertung wie OpenType: die erste Untertabelle, deren Coverage das erste Zeichen enthaelt, gilt
(bei Format 1 nur, wenn das Paar darin steht).
"""
import json
import os
import sys

from fontTools.ttLib import TTFont


def _kern_lookups(font):
    gpos = font["GPOS"].table
    idx = set()
    for fr in gpos.FeatureList.FeatureRecord:
        if fr.FeatureTag == "kern":
            idx.update(fr.Feature.LookupListIndex)
    return [gpos.LookupList.Lookup[i] for i in sorted(idx)]


def _subtables(lookup):
    for st in lookup.SubTable:
        if lookup.LookupType == 9:          # Extension
            if st.ExtensionLookupType != 2:
                continue
            st = st.ExtSubTable
        elif lookup.LookupType != 2:
            continue
        yield st


def _xadv(value):
    return int(getattr(value, "XAdvance", 0) or 0) if value is not None else 0


def metrics(path):
    font = TTFont(path)
    name = font["name"].getDebugName(1)
    cmap = font.getBestCmap()
    by_glyph = {}
    for cp, g in cmap.items():
        by_glyph.setdefault(g, []).append(cp)
    hmtx = font["hmtx"]
    out = {"upm": font["head"].unitsPerEm, "adv": {str(cp): hmtx[g][0] for cp, g in cmap.items()}, "kern": []}
    if "GPOS" not in font:
        return name, out
    for lookup in _kern_lookups(font):
        for st in _subtables(lookup):
            cov = [cp for g in st.Coverage.glyphs for cp in by_glyph.get(g, [])]
            if not cov:
                continue
            if st.Format == 1:
                pairs = {}
                for g1, ps in zip(st.Coverage.glyphs, st.PairSet):
                    row = {}
                    for rec in ps.PairValueRecord:
                        v = _xadv(rec.Value1)
                        if v:
                            for cp2 in by_glyph.get(rec.SecondGlyph, []):
                                row[str(cp2)] = v
                    if row:
                        for cp1 in by_glyph.get(g1, []):
                            pairs[str(cp1)] = row
                out["kern"].append({"t": 1, "cov": cov, "p": pairs})
            elif st.Format == 2:
                c1 = {str(cp): k for g, k in st.ClassDef1.classDefs.items() for cp in by_glyph.get(g, [])}
                c2 = {str(cp): k for g, k in st.ClassDef2.classDefs.items() for cp in by_glyph.get(g, [])}
                m = {}
                for i, rec in enumerate(st.Class1Record):
                    for j, r2 in enumerate(rec.Class2Record):
                        v = _xadv(r2.Value1)
                        if v:
                            m["%d,%d" % (i, j)] = v
                out["kern"].append({"t": 2, "cov": cov, "c1": c1, "c2": c2, "m": m})
    return name, out


def main(font_dir):
    data = {}
    for f in sorted(os.listdir(font_dir)):
        if f.lower().endswith(".ttf"):
            name, m = metrics(os.path.join(font_dir, f))
            data[name] = m
            print("%-24s %4d Zeichen, %d Kerning-Tabellen" % (name, len(m["adv"]), len(m["kern"])))
    path = os.path.join(font_dir, "metrics.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)
    print(path, os.path.getsize(path), "Bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fonts"))
