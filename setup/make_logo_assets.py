"""Alle Logo-Dateien aus einer quadratischen Vorlage erzeugen (Logo seit 2.5.5-dev5: weisses "R" mit
roten Akzenten und Kunai auf dunklem Grund, 1024x1024).

Kleine Symbole schneiden enger um das "R" zu - in voller Breite war es bei 16-32 px kaum zu erkennen.
Abgerundet wie ein App-Symbol (22,5 %), nur das Apple-Symbol bleibt eckig (iOS rundet selbst).

    python make_logo_assets.py <vorlage.jpg> <vry-Ordner> [<Website-Ordner>]

App (vry/):  setup/rankyoinker.ico (Installer, vry.exe, Verknuepfungen, Tray), assets/logo-mark.png
             (Spiel-Overlay), assets/logo-mark-48.png + logo-mark-128.webp (fuer index.html eingebettet),
             assets/apple-touch-icon.png (Dienst, _APP_ICONS)
Website:     public/favicon.ico, favicon-16/32.png, apple-touch-icon.png, img/brand/logo-*.webp|png,
             demo-dev-src/pwa/apple-touch-icon.png, rankyoinker-logo.png (Discord)
"""
import os
import sys

from PIL import Image, ImageDraw

CROP = (150, 160, 870, 880)          # 720 px um das R (ab 48 px)
CROP_TIGHT = (175, 190, 845, 860)    # 670 px fuer 16-32 px
RADIUS = 0.225


def rounded(im, frac=RADIUS):
    s = 4
    w, h = im.size
    mask = Image.new("L", (w * s, h * s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=int(w * s * frac), fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask.resize((w, h), Image.LANCZOS))
    return out


def mark(src, size, round_=True):
    crop = CROP_TIGHT if size <= 32 else CROP
    im = src.crop(crop).resize((size, size), Image.LANCZOS)
    return rounded(im) if round_ else im.convert("RGB")


def main(src_path, vry, web=None):
    src = Image.open(src_path).convert("RGB")
    assets = os.path.join(vry, "assets")
    os.makedirs(assets, exist_ok=True)
    sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
    icons = [mark(src, s) for s in sizes]
    icons[-1].save(os.path.join(vry, "setup", "rankyoinker.ico"), format="ICO",
                   sizes=[(s, s) for s in sizes], append_images=icons[:-1])
    mark(src, 128).save(os.path.join(assets, "logo-mark.png"), optimize=True)
    mark(src, 48).save(os.path.join(assets, "logo-mark-48.png"), optimize=True)
    mark(src, 128, round_=False).save(os.path.join(assets, "logo-mark-128.webp"), quality=88, method=6)
    apple = mark(src, 180, round_=False).quantize(colors=256, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
    apple.save(os.path.join(assets, "apple-touch-icon.png"), optimize=True)
    if web:
        pub = os.path.join(web, "public")
        brand = os.path.join(pub, "img", "brand")
        os.makedirs(brand, exist_ok=True)
        fav = [mark(src, s) for s in (16, 32, 48)]
        fav[-1].save(os.path.join(pub, "favicon.ico"), format="ICO", sizes=[(16, 16), (32, 32), (48, 48)],
                     append_images=fav[:-1])
        fav[0].save(os.path.join(pub, "favicon-16.png"), optimize=True)
        fav[1].save(os.path.join(pub, "favicon-32.png"), optimize=True)
        apple.save(os.path.join(pub, "apple-touch-icon.png"), optimize=True)
        apple.save(os.path.join(web, "demo-dev-src", "pwa", "apple-touch-icon.png"), optimize=True)
        for s in (128, 256):
            mark(src, s, round_=False).save(os.path.join(brand, "logo-mark-%d.webp" % s), quality=88, method=6)
        src.save(os.path.join(brand, "logo-full-1024.webp"), quality=86, method=6)
        mark(src, 512).save(os.path.join(brand, "logo-mark-512.png"), optimize=True)
        src.save(os.path.join(web, "rankyoinker-logo.png"), optimize=True)
    for root in filter(None, (assets, web and os.path.join(web, "public"))):
        print(root)


if __name__ == "__main__":
    main(*sys.argv[1:])
