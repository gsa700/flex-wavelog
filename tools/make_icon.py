#!/usr/bin/env python3
"""Generate assets/flex-wavelog.ico.

The icon is drawn in code rather than shipped as an opaque binary so anyone can
see exactly what it is and regenerate it. Motif: an RF sine wave crossing a dark
rounded square, ending in a solid dot - the signal landing in the log.

Requires Pillow (dev-time only; the .ico is committed so users never need this).
"""

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "assets", "flex-wavelog.ico")

# Draw huge, downscale for anti-aliasing.
S = 1024
BG = (28, 38, 50, 255)        # dark slate, close to Wavelog's dark theme
WAVE = (26, 188, 156, 255)    # the teal used across the app's UI
DOT = (235, 235, 235, 255)


def draw_master():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square plate. Radius tuned so the silhouette still reads square
    # at 16 px instead of collapsing into a circle.
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 5, fill=BG)

    # Sine wave, two periods, amplitude growing left to right. Drawn by
    # stamping a disc at each sample point rather than ImageDraw.line - thick
    # polylines get spiky joint artifacts, stamped discs stay smooth.
    x0, x1 = int(S * 0.14), int(S * 0.74)
    mid = S / 2
    r = S // 28    # stroke radius
    for i in range(0, 1201):
        t = i / 1200
        x = x0 + (x1 - x0) * t
        amp = S * (0.10 + 0.16 * t)      # growing envelope
        y = mid - amp * math.sin(t * 2 * math.pi * 2)
        d.ellipse((x - r, y - r, x + r, y + r), fill=WAVE)

    # The dot the wave arrives at.
    dr = S * 0.075
    dx, dy = S * 0.84, mid
    d.ellipse((dx - dr, dy - dr, dx + dr, dy + dr), fill=DOT)

    return img


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    master = draw_master()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    print(f"wrote {os.path.normpath(OUT)} ({os.path.getsize(OUT)} bytes, sizes {sizes})")


if __name__ == "__main__":
    main()
