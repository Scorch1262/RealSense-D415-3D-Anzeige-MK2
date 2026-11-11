# -*- coding: utf-8 -*-
"""
Demo-Raum für RealSense-Viewer-MK2: ein möbliertes Zimmer (6 × 5 × 2,6 m), das per
Strahlverfolgung aus Sicht einer langsam umhergehenden Kamera gerendert wird.
So lassen sich Raumscan und Begehen auch ohne angeschlossene D415 ausprobieren.
"""

import math

import numpy as np

RAUM = (np.array([-3.0, -2.5, 0.0], np.float32), np.array([3.0, 2.5, 2.6], np.float32))

# (unten, oben, Farbe) – Welt: Meter, Z nach oben
MOEBEL = [
    ((0.6, -0.5, 0.0), (1.6, 0.3, 0.75), (150, 95, 50)),       # Tisch
    ((0.7, -0.4, 0.75), (0.95, -0.15, 1.05), (220, 60, 60)),   # Karton auf dem Tisch
    ((1.2, 0.0, 0.75), (1.35, 0.15, 1.0), (60, 200, 90)),      # Dose auf dem Tisch
    ((-2.95, 1.0, 0.0), (-2.4, 2.45, 1.9), (200, 200, 205)),   # Schrank
    ((-2.95, -1.8, 1.2), (-2.6, -0.4, 1.25), (170, 120, 70)),  # Wandregal
    ((-2.95, -1.5, 1.25), (-2.7, -1.2, 1.5), (60, 110, 220)),  # Ordner
    ((-1.0, -2.0, 0.0), (-0.5, -1.5, 0.5), (255, 176, 0)),     # Kiste (Amber)
    ((1.8, 1.2, 0.0), (2.1, 1.5, 2.6), (90, 90, 100)),         # Säule
    ((-0.5, 2.0, 0.0), (1.0, 2.45, 0.8), (25, 150, 140)),      # Sofa (Teal)
    ((2.5, -2.45, 0.0), (2.95, -1.2, 1.1), (120, 60, 70)),     # Kommode
    ((-1.6, 0.6, 0.0), (-1.1, 1.1, 0.45), (110, 70, 160)),     # Hocker
    ((-1.6, 1.05, 0.45), (-1.1, 1.1, 1.0), (110, 70, 160)),    # Lehne
    ((0.3, -2.45, 0.9), (1.5, -2.42, 1.7), (230, 230, 230)),   # Bild an der Wand
    ((-0.2, -0.2, 2.45), (0.2, 0.2, 2.6), (240, 240, 200)),    # Lampe
]
_MOEBEL = [(np.array(a, np.float32), np.array(b, np.float32), np.array(c, np.float32)) for a, b, c in MOEBEL]


def kamera_pose(t):
    """Position (x, y, z), Gierwinkel und Neigung der Demo-Kamera zur Zeit t (s)."""
    a = t * 0.09
    pos = (0.6 * math.cos(a * 0.7), 0.4 * math.sin(a * 0.7), 1.45 + 0.05 * math.sin(a * 1.3))
    return pos, a, math.radians(-12 + 8 * math.sin(a * 0.9))


def render(w, h, fx, fy, ppx, ppy, pos, yaw, pitch):
    """Liefert (Tiefe in m als float32 h×w, RGB uint8 h×w×3)."""
    u = (np.arange(w, dtype=np.float32) - ppx) / fx
    v = (np.arange(h, dtype=np.float32) - ppy) / fy
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    fwd = np.array([cy * cp, sy * cp, sp], np.float32)
    right = np.array([sy, -cy, 0.0], np.float32)
    up = np.cross(right, fwd)
    d = u[None, :, None] * right + (-v)[:, None, None] * up + fwd      # d·fwd = 1 -> t ist z-Tiefe
    o = np.asarray(pos, np.float32)
    inv = 1.0 / np.where(np.abs(d) < 1e-9, 1e-9, d)

    lo, hi = RAUM
    tmax = np.maximum((lo - o) * inv, (hi - o) * inv)
    t = tmax.min(-1)
    ax = tmax.argmin(-1)
    obj = np.full(t.shape, -1, np.int16)
    for k, (blo, bhi, _) in enumerate(_MOEBEL):
        a1, a2 = (blo - o) * inv, (bhi - o) * inv
        tn = np.minimum(a1, a2)
        te, tx = tn.max(-1), np.maximum(a1, a2).min(-1)
        hit = (te <= tx) & (te > 0.05) & (te < t)
        if hit.any():
            t = np.where(hit, te, t)
            ax = np.where(hit, tn.argmax(-1), ax)
            obj = np.where(hit, k, obj)

    p = o + d * t[..., None]
    X, Y, Z = p[..., 0], p[..., 1], p[..., 2]
    rgb = np.zeros((h, w, 3), np.float32)
    room = obj < 0
    floor = room & (ax == 2) & (Z < 0.01)
    ceil = room & (ax == 2) & (Z >= 0.01)
    wall = room & (ax != 2)

    chk = (np.floor(X * 4) + np.floor(Y * 4)) % 2
    rgb[floor] = np.stack([70 + 50 * chk, 66 + 46 * chk, 60 + 40 * chk], -1)[floor]
    cchk = 0.85 + 0.15 * ((np.floor(X * 2) + np.floor(Y * 2)) % 2)
    rgb[ceil] = (np.array([200, 200, 195], np.float32) * cchk[..., None])[ceil]
    s = np.where(ax == 0, Y, X)
    wc = np.stack([90 + 60 * (np.sin(s * 3.1) * 0.5 + 0.5),
                   100 + 50 * (np.cos(Z * 4.3 + s) * 0.5 + 0.5),
                   140 + 60 * (np.sin(s * 1.3 + Z * 2.1) * 0.5 + 0.5)], -1)
    tile = (np.floor(s * 5) + np.floor(Z * 5)) % 2
    wc *= (0.8 + 0.2 * tile)[..., None]
    stripe = (np.abs(Z - 1.0) < 0.04) | (np.abs(Z - 0.1) < 0.1)
    wc[stripe] = (40, 40, 45)
    rgb[wall] = wc[wall]

    shade = np.where(ax == 2, 1.0, np.where(ax == 0, 0.8, 0.65))
    pat = 0.85 + 0.15 * ((np.floor(X * 8) + np.floor(Y * 8) + np.floor(Z * 8)) % 2)
    for k, (_, _, c) in enumerate(_MOEBEL):
        m = obj == k
        if m.any():
            rgb[m] = c * (shade * pat)[m][:, None]
    return t.astype(np.float32), np.clip(rgb, 0, 255).astype(np.uint8)
