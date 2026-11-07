# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Draws the Unix app logo: an isometric, extruded letter U on a violet tile, with a small spark.

    python scripts/make_unix_logo.py

Writes branding/unix_logo.png (1024 px), branding/unix_logo_256.png and branding/unix.ico. Original artwork,
drawn from geometry here: nothing is taken from another logo.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "branding")
SS = 4                      # supersampling
N = 1024 * SS

C, S = math.cos(math.radians(30)), math.sin(math.radians(30))

TOP, FRONT, SIDE = (255, 255, 255), (232, 224, 255), (171, 148, 255)
BG_TOP, BG_BOTTOM = (124, 58, 237), (59, 19, 140)


def tile_mask(size, radius):
    """A rounded square with continuous (squircle-like) corners."""
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def gradient(size):
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = min(1.0, max(0.0, (0.75 * y + 0.25 * x) / size))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
    return g


def project(p, scale, ox, oy):
    x, y, z = p
    return ox + (x - y) * C * scale, oy + ((x + y) * S - z) * scale


def box(x0, x1, z0, z1, d):
    """The three faces of a box seen from +x, +y, +z, far ones first: top, front (+y), side (+x)."""
    top = [(x0, 0, z1), (x1, 0, z1), (x1, d, z1), (x0, d, z1)]
    front = [(x0, d, z0), (x1, d, z0), (x1, d, z1), (x0, d, z1)]
    side = [(x1, 0, z0), (x1, d, z0), (x1, d, z1), (x1, 0, z1)]
    return [(top, TOP), (front, FRONT), (side, SIDE)]


def letter_u():
    W, H, a, d = 1.32, 1.18, 0.33, 0.30      # width, height, bar thickness, depth
    faces = []
    for x0, x1, z0, z1 in ((0, a, 0, H), (a, W - a, 0, a), (W - a, W, 0, H)):
        faces += box(x0, x1, z0, z1, d)
    return faces


def draw():
    base = gradient(N // 8).resize((N, N), Image.BICUBIC)
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    img.paste(base, (0, 0), tile_mask(N, int(N * 0.225)))

    faces = letter_u()
    pts = [p for f, _ in faces for p in f]
    xs = [(x - y) * C for x, y, z in pts]
    ys = [(x + y) * S - z for x, y, z in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    scale = N * 0.56 / span
    ox = N / 2 - (min(xs) + max(xs)) / 2 * scale
    oy = N / 2 - (min(ys) + max(ys)) / 2 * scale + N * 0.01

    # soft shadow under the letter
    shadow = Image.new("L", (N, N), 0)
    sd = ImageDraw.Draw(shadow)
    for f, _ in faces:
        sd.polygon([project(p, scale, ox, oy + N * 0.03) for p in f], fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(N * 0.03))
    dark = Image.new("RGBA", (N, N), (20, 6, 50, 255))
    img = Image.composite(dark, img, Image.composite(shadow, Image.new("L", (N, N), 0), img.split()[3]))
    img.putalpha(tile_mask(N, int(N * 0.225)))

    d = ImageDraw.Draw(img)
    for f, colour in faces:
        d.polygon([project(p, scale, ox, oy) for p in f], fill=colour)

    # a four-point spark, top right: the AI in the app
    cx, cy, r = N * 0.765, N * 0.235, N * 0.075
    spark = []
    for i in range(8):
        ang = math.pi / 2 * (i // 2) + (math.pi / 4 if i % 2 else 0) - math.pi / 2
        rr = r if i % 2 == 0 else r * 0.22
        spark.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(spark, fill=(255, 255, 255))
    small = N * 0.028
    d.ellipse([cx + r * 0.95 - small, cy + r * 0.95 - small, cx + r * 0.95 + small, cy + r * 0.95 + small],
              fill=(232, 224, 255))

    # a faint light edge along the top of the tile
    rim = Image.new("L", (N, N), 0)
    ImageDraw.Draw(rim).rounded_rectangle([0, 0, N - 1, N - 1], radius=int(N * 0.225), outline=70, width=int(N * 0.006))
    rim = Image.composite(rim, Image.new("L", (N, N), 0),
                          Image.linear_gradient("L").rotate(180).resize((N, N)))
    img = Image.composite(Image.new("RGBA", (N, N), (255, 255, 255, 255)), img, rim)
    img.putalpha(tile_mask(N, int(N * 0.225)))
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    logo = draw()
    logo.save(os.path.join(OUT, "unix_logo.png"))
    logo.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "unix_logo_256.png"))
    logo.save(os.path.join(OUT, "unix.ico"), sizes=[(s, s) for s in (256, 128, 64, 48, 32, 24, 16)])
    print("wrote", os.path.join(OUT, "unix_logo.png"), "and unix.ico")


if __name__ == "__main__":
    main()
