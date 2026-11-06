"""Extract a die-cut card outline from reference artwork with a true alpha channel.

The silhouette of the design IS the die line: this traces the alpha boundary
(Moore neighbourhood), simplifies it with Ramer-Douglas-Peucker and writes an
outline file of [u, v] points in canvas space (0..1, y down -- the same space the
layer PNGs live in). card-config.json then references that file by path
(`"outline": "./outline.json"`), which build_card.py reads; the viewer ignores the
key. An optional overlay preview draws the loop and its vertices over the
reference so the trace can be checked by eye before a build: the red line must hug
the physical edge, with a vertex on every corner and notch.

Only Pillow is required (already a skill dependency: validate_assets.py and
generate_typography.py use it). There is no numpy dependency -- the trace and the
RDP simplification are pure Python, and this script is a standalone tool that the
main pipeline never imports, so it can never block a build.

Usage:
  python3 extract_outline.py <reference.png> <outline.json> [preview.png] [--into <card-config.json>]

`--into` sets card-config.json's "outline" to the outline file's path (relative to
the config), matching the assets.* path style. Hand-authored outlines need no
script: write {"points": [[u, v], ...]} with 8..200 pairs into outline.json.
"""
import argparse
import json
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw

EPS = 0.0015  # RDP tolerance in normalised canvas units
MAX_POINTS = 200  # keep a dense contour from bloating the config/mesh


def alpha_mask(im):
    """Threshold the alpha channel to a row-major list of 0/1 bytes rows."""
    a = im.getchannel("A")
    w, h = a.size
    data = a.tobytes()
    thresh = bytes(1 if b > 128 else 0 for b in data)
    return [thresh[y * w:(y + 1) * w] for y in range(h)], w, h


def trace_boundary(mask, w, h):
    """Moore-neighbour boundary trace; returns an ordered (x, y) pixel loop.

    Starts at the topmost-then-leftmost opaque pixel and walks the 8-neighbourhood
    clockwise, backtracking from the virtual outside pixel above the start.
    """
    start = None
    for y in range(h):
        x = mask[y].find(1)
        if x != -1:
            start = (x, y)
            break
    if start is None:
        return []

    neigh = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

    def opaque(p):
        return 0 <= p[0] < w and 0 <= p[1] < h and mask[p[1]][p[0]]

    loop = [start]
    prev = (start[0], start[1] - 1)  # virtual outside pixel above the start
    cur = start
    for _ in range(8 * (w + h)):
        ib = neigh.index((prev[0] - cur[0], prev[1] - cur[1]))
        nxt = None
        for k in range(1, 9):  # sweep clockwise from just after the backtrack
            d = neigh[(ib + k) % 8]
            p = (cur[0] + d[0], cur[1] + d[1])
            if opaque(p):
                nxt = p
                break
        if nxt is None or nxt == start:
            break
        loop.append(nxt)
        prev, cur = cur, nxt
    return loop


def rdp(points, eps):
    """Iterative Ramer-Douglas-Peucker on an open polyline (pure Python).

    Mirrors the vectorised version exactly: at each step the farthest point from
    the a-b segment is found (first maximum wins, as np.argmax does) and kept if
    it exceeds eps, then the two sub-segments are recursed via an explicit stack.
    """
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        segx = bx - ax
        segy = by - ay
        length = math.hypot(segx, segy)
        maxi = -1
        maxd = -1.0
        for k in range(a + 1, b):
            px, py = points[k]
            dx = px - ax
            dy = py - ay
            if length < 1e-12:
                d = math.hypot(dx, dy)
            else:
                d = abs(segx * dy - segy * dx) / length
            if d > maxd:  # strict > keeps the FIRST maximum, like np.argmax
                maxd = d
                maxi = k
        if maxi >= 0 and maxd > eps:
            keep[maxi] = True
            stack.append((a, maxi))
            stack.append((maxi, b))
    return [points[i] for i in range(n) if keep[i]]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("reference", type=Path)
    p.add_argument("outline", type=Path)
    p.add_argument("preview", type=Path, nargs="?")
    p.add_argument("--into", type=Path, help="set this card-config.json's \"outline\" to the outline path")
    a = p.parse_args(argv)

    im = Image.open(a.reference).convert("RGBA")
    W, H = im.size
    mask, w, h = alpha_mask(im)
    frac = sum(sum(row) for row in mask) / float(W * H)
    if frac < 0.05:
        raise SystemExit("reference alpha is (almost) empty; nothing to trace")
    print(f"reference {W}x{H}, opaque fraction {frac:.1%}")

    loop = trace_boundary(mask, w, h)
    if len(loop) < 8:
        raise SystemExit("boundary trace collapsed; is the alpha a single blob without a silhouette?")
    print(f"traced boundary: {len(loop)} px")

    simp = rdp([(x / W, y / H) for x, y in loop], EPS)
    if simp and simp[0] == simp[-1]:
        simp = simp[:-1]
    if len(simp) < 8:
        raise SystemExit(f"simplified to {len(simp)} vertices; lower EPS or check the alpha")
    if len(simp) > MAX_POINTS:
        raise SystemExit(f"simplified to {len(simp)} vertices (> {MAX_POINTS}); raise EPS to coarsen the contour")
    print(f"simplified: {len(simp)} vertices at eps={EPS}")

    pts = [[round(u, 5), round(v, 5)] for u, v in simp]
    a.outline.write_text(json.dumps({"points": pts}, ensure_ascii=False, indent=1), encoding="utf8")
    print("wrote", a.outline)

    if a.preview:
        prev = im.convert("RGB")
        dr = ImageDraw.Draw(prev)
        px = [(u * W, v * H) for u, v in simp]
        dr.line(px + [px[0]], fill=(255, 0, 0), width=3)
        for x, y in px:
            dr.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 0))
        prev.save(a.preview)
        print("wrote", a.preview)

    if a.into:
        cfg = json.loads(a.into.read_text(encoding="utf-8-sig"))
        rel = os.path.relpath(a.outline, a.into.parent).replace(os.sep, "/")
        if "/" not in rel:
            rel = "./" + rel
        cfg["outline"] = rel
        a.into.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf8")
        print("set", a.into, '"outline" ->', rel)


if __name__ == "__main__":
    main()
