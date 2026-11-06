"""Regression tests for extract_outline.py (pure Python -- no Blender, no numpy).

Builds synthetic alpha fixtures with known silhouettes and checks the boundary
trace, the RDP simplification (epsilon invariant, determinism, monotonicity), the
outline-file round-trip and the --into config wiring.

Run: python3 test_outline.py
Exit code 0 = all assertions passed.
"""
import json
import math
import os
import sys
import tempfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_outline as eo  # noqa: E402


def fixture(w=200, h=300, r=24, notch=44):
    """Rounded opaque card body with a triangular notch cut from the bottom-right
    corner, so the silhouette is both curved and concave like a real die-cut."""
    a = Image.new("L", (w, h), 0)
    ImageDraw.Draw(a).rounded_rectangle([10, 10, w - 11, h - 11], radius=r, fill=255)
    x1, y1 = w - 10, h - 10           # exclusive bottom-right of the body
    nx, ny = x1 - notch, y1 - notch   # notch corner
    apx = a.load()
    for y in range(h):
        for x in range(w):
            if x >= nx and y >= ny and (x - nx) + (y - ny) > notch:
                apx[x, y] = 0         # cut the triangle
    im = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    im.putalpha(a)
    return im


def kept_indices(points, simp):
    """Map a simplification back to input indices (simp is an ordered subsequence)."""
    idx = []
    s = 0
    for i, p in enumerate(points):
        if s < len(simp) and (p[0], p[1]) == (simp[s][0], simp[s][1]):
            idx.append(i)
            s += 1
    assert s == len(simp), f"only matched {s}/{len(simp)} simplified points back to the input"
    return idx


def seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    sx, sy = bx - ax, by - ay
    L = math.hypot(sx, sy)
    if L < 1e-12:
        return math.hypot(px - ax, py - ay)
    return abs(sx * (py - ay) - sy * (px - ax)) / L


def main():
    # --- 1) boundary trace: starts topmost-then-leftmost, walks opaque pixels ---
    im = fixture()
    W, H = im.size
    mask, w, h = eo.alpha_mask(im)
    assert (w, h) == (W, H), "mask dims must match the image"
    loop = eo.trace_boundary(mask, w, h)
    assert len(loop) > 8, f"trace collapsed to {len(loop)} points"
    # the trace must start at the topmost opaque row, then its leftmost pixel
    exp_start = next((mask[y].find(1), y) for y in range(h) if mask[y].find(1) != -1)
    assert loop[0] == exp_start, f"trace must start topmost-then-leftmost: got {loop[0]}, expected {exp_start}"
    for x, y in loop:
        assert mask[y][x], f"traced point ({x},{y}) is not opaque"
    # the notch must be visited (a concave corner near the bottom-right)
    assert any(x > W - 60 and y > H - 60 for x, y in loop), "trace never reached the notch corner"

    # --- 2) rdp on a straight line collapses to its endpoints ---
    line = [(i / 10.0, 0.0) for i in range(11)]
    assert eo.rdp(line, eo.EPS) == [line[0], line[-1]], "a straight line must simplify to 2 points"

    # --- 3) rdp keeps a sharp corner ---
    corner = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (1.0, 0.5), (1.0, 1.0)]
    simp = eo.rdp(corner, eo.EPS)
    assert (1.0, 0.0) in simp and (1.0, 1.0) in simp, "the 90-degree corner must survive"

    # --- 4) epsilon invariant on the real fixture: every dropped point is within eps ---
    norm = [(x / W, y / H) for x, y in loop]
    simp = eo.rdp(norm, eo.EPS)
    assert 8 <= len(simp) <= eo.MAX_POINTS, f"simplified to {len(simp)} vertices, outside 8..{eo.MAX_POINTS}"
    ki = kept_indices(norm, simp)
    worst = 0.0
    for s in range(len(ki) - 1):
        a, b = ki[s], ki[s + 1]
        for k in range(a + 1, b):
            worst = max(worst, seg_dist(norm[k], norm[a], norm[b]))
    assert worst <= eo.EPS + 1e-12, f"dropped a point {worst:.6f} away (> eps {eo.EPS})"

    # --- 5) determinism: same input -> identical output, twice ---
    assert eo.rdp(norm, eo.EPS) == simp, "rdp is not deterministic"
    assert eo.trace_boundary(mask, w, h) == loop, "trace is not deterministic"

    # --- 6) monotonicity: a coarser eps never yields MORE points ---
    assert len(eo.rdp(norm, eo.EPS * 4)) <= len(simp), "a larger eps must not increase the vertex count"

    # --- 7) end-to-end: main() writes a canvas-UV outline file and wires --into ---
    with tempfile.TemporaryDirectory() as td:
        ref = os.path.join(td, "ref.png")
        im.save(ref)
        out = os.path.join(td, "outline.json")
        prev = os.path.join(td, "preview.png")
        cfgp = os.path.join(td, "card-config.json")
        with open(cfgp, "w", encoding="utf8") as f:
            json.dump({"title": "t", "assets": {}}, f)
        eo.main([ref, out, prev, "--into", cfgp])

        data = json.load(open(out, encoding="utf8"))
        pts = data["points"]
        assert isinstance(pts, list) and len(pts) >= 8, "outline.json must hold >= 8 points"
        for u, v in pts:
            assert 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0, f"point ({u},{v}) left canvas UV space"
            assert round(u, 5) == u and round(v, 5) == v, "points must be rounded to 5 decimals"
        assert os.path.exists(prev) and os.path.getsize(prev) > 0, "preview PNG was not written"

        cfg = json.load(open(cfgp, encoding="utf8"))
        assert cfg["outline"] == "./outline.json", f"--into must set a relative path, got {cfg['outline']!r}"

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
