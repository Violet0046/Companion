import math
import struct
import zlib
from pathlib import Path

OUT = Path("src/companion_ever/web")


def write_png(path, w, h, pixels):
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xffffffff
        return (struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def lerp(a, b, t):
    out = []
    for i in range(3):
        out.append(int(round(a[i] + (b[i] - a[i]) * t)))
    return tuple(out)


def heart_inside(px, py, cx, cy, scale):
    x = (px - cx) / scale
    y = -(py - cy) / scale
    v = (x * x + y * y - 1.0) ** 3 - x * x * (y ** 3)
    return v <= 0.0


def make_icon(size):
    ss = 3
    big = size * ss
    c1 = (240, 168, 146)
    c2 = (205, 100, 78)
    cx = big / 2.0
    cy = big / 2.0
    heart_scale = big * 0.19
    radius = big * 0.22

    acc = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]
    for by in range(big):
        for bx in range(big):
            t = min(1.0, (bx / big + by / big) / 2.0)
            r, g, b = lerp(c1, c2, t)
            dx = max(radius - bx, 0, bx - (big - 1 - radius))
            dy = max(radius - by, 0, by - (big - 1 - radius))
            if dx * dx + dy * dy > radius * radius:
                continue
            if heart_inside(bx, by, cx, cy, heart_scale):
                r, g, b = 255, 255, 255
            cell = acc[by // ss][bx // ss]
            cell[0] += r
            cell[1] += g
            cell[2] += b
            cell[3] += 1

    n = ss * ss
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            r, g, b, a = acc[y][x]
            if a == 0:
                row.append((0, 0, 0))
            else:
                row.append((r // a, g // a, b // a))
        rows.append(row)
    return rows


for sz in (192, 512):
    pixels = make_icon(sz)
    path = OUT / ("icon-%d.png" % sz)
    write_png(path, sz, sz, pixels)
    print("  %s  %s bytes" % (path, format(path.stat().st_size, ",")))
