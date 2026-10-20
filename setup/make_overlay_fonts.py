"""Schriften fuer das Spiel-Overlay (game_overlay.py) erzeugen - dieselben wie Website und
Overlay-Seite: Inter (Text), Inter Tight (Ueberschriften, Zahlen), Geist Mono (kleine
Ueberschriften in Grossbuchstaben).

GDI+ kann weder WOFF2 noch variable Schriften (es nimmt nur die Grundeinstellung). Darum
werden aus den variablen TTFs von Google Fonts feste Schnitte erzeugt, auf Latein,
Griechisch, Kyrillisch und Satzzeichen beschnitten (Koreanisch/Japanisch/Chinesisch kommen
ohnehin aus den Windows-Schriften, siehe script_of() in game_overlay.py) und eindeutig
benannt ("RY ..."), damit sie nie mit einem installierten Inter verwechselt werden.
Alle drei stehen unter der SIL Open Font License 1.1 ohne reservierte Namen - Lizenztexte
liegen daneben in fonts/.

Benutzung (fontTools noetig):
    curl -L -o inter.ttf      "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/Inter%5Bopsz,wght%5D.ttf"
    curl -L -o intertight.ttf "https://raw.githubusercontent.com/google/fonts/main/ofl/intertight/InterTight%5Bwght%5D.ttf"
    curl -L -o geistmono.ttf  "https://raw.githubusercontent.com/google/fonts/main/ofl/geistmono/GeistMono%5Bwght%5D.ttf"
    python make_overlay_fonts.py <ordner mit den drei Dateien> ../fonts
    python make_overlay_metrics.py ../fonts      (danach: Breiten + Kerning fuer den eigenen Satz)
"""
import os
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

# (Quelle, Achsen, Familienname, Dateiname, Gewicht)
# Seit 2.5.6 je eine Stufe kraeftiger als auf der Website (Text 500 statt 400, Namen/Zahlen 750, Mono 600):
# GDI+ zeichnet in Graustufen ohne die Verstaerkung von DirectWrite/Chrome, gleiche Gewichte wirkten duenn.
FONTS = (
    ("inter.ttf", {"wght": 500, "opsz": 14}, "RY Inter Medium", "RYInter-Medium.ttf", 500),
    ("inter.ttf", {"wght": 600, "opsz": 14}, "RY Inter SemiBold", "RYInter-SemiBold.ttf", 600),
    ("intertight.ttf", {"wght": 750}, "RY Inter Tight Display", "RYInterTight-Display.ttf", 750),
    ("intertight.ttf", {"wght": 900}, "RY Inter Tight Black", "RYInterTight-Black.ttf", 900),
    ("geistmono.ttf", {"wght": 600}, "RY Geist Mono SemiBold", "RYGeistMono-SemiBold.ttf", 600),
)
UNICODES = [*range(0x20, 0x250), *range(0x2B0, 0x370), *range(0x370, 0x400), *range(0x400, 0x530),
            *range(0x1E00, 0x1F00), *range(0x2000, 0x2070), *range(0x2070, 0x20D0), *range(0x2100, 0x2200),
            *range(0x2200, 0x2300), *range(0x25A0, 0x2600), *range(0xFB00, 0xFB07)]


def rename(font, family, weight):
    name = font["name"]
    for nid in (16, 17, 21, 22, 25):
        name.removeNames(nameID=nid)
    ps = family.replace(" ", "")
    for nid, value in ((1, family), (2, "Regular"), (3, "%s-Regular" % ps), (4, family), (6, ps)):
        name.setName(value, nid, 3, 1, 0x409)
        name.setName(value, nid, 1, 0, 0)
    os2 = font["OS/2"]
    os2.usWeightClass = weight
    os2.fsSelection = (os2.fsSelection & ~0b1100001) | 0b1000000     # weder fett noch kursiv, REGULAR
    font["head"].macStyle = 0


def main(src, out):
    os.makedirs(out, exist_ok=True)
    for source, axes, family, filename, weight in FONTS:
        font = TTFont(os.path.join(src, source))
        font = instancer.instantiateVariableFont(font, axes)
        opts = subset.Options()
        opts.hinting = False
        opts.name_IDs = ["*"]
        opts.name_languages = ["*"]
        opts.notdef_outline = True
        sub = subset.Subsetter(opts)
        sub.populate(unicodes=UNICODES)
        sub.subset(font)
        rename(font, family, weight)
        path = os.path.join(out, filename)
        font.save(path)
        print("%-26s %6d Bytes  %s" % (filename, os.path.getsize(path), family))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
