# -*- coding: utf-8 -*-
"""
Raumscan für RealSense-Viewer-MK2.

Die Kamera wird durch den Raum bewegt; jedes Tiefenbild wird per ICP (Punkt-zu-Ebene,
projektive Zuordnung, grob-nach-fein) an ein Schlüsselbild angelegt. Es werden nur die
Tiefendaten der Stereo-IR-Kameras genutzt, das Farbbild wird nicht gebraucht – der Scan
funktioniert daher auch bei völliger Dunkelheit (IR-Projektor an). Daraus ergibt
sich die Kamerabahn, mit der alle Punkte in eine gemeinsame Voxel-Karte eingetragen
werden. Diese Karte kann anschließend in der Ich-Perspektive begangen werden.

Koordinaten der Karte (wie im Viewer): Meter, Z nach oben; die erste Kameraposition
liegt im Ursprung und blickt in +Y.
"""

import logging
import math
import threading
import time

import numpy as np

log = logging.getLogger("RealSense-Viewer-MK2")

# Kamera (x rechts, y unten, z vorne) -> Viewer-Welt (X rechts, Y vorne, Z oben)
C_VIEW = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], np.float64)

ARBEITS_BREITE = 320          # Arbeitsauflösung für Tracking und Karte (Pixel)


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def se3_exp(x):
    """x = (wx, wy, wz, tx, ty, tz) -> 4x4-Matrix."""
    w, v = x[:3], x[3:]
    th = float(np.linalg.norm(w))
    K = np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]])
    if th < 1e-9:
        R = np.eye(3) + K
    else:
        R = np.eye(3) + math.sin(th) / th * K + (1 - math.cos(th)) / th ** 2 * (K @ K)
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = v
    return T


def rot_angle(R):
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))


def vertex_map(depth, fx, fy, ppx, ppy):
    h, w = depth.shape
    u = (np.arange(w, dtype=np.float32) - ppx) / fx
    v = (np.arange(h, dtype=np.float32) - ppy) / fy
    V = np.empty((h, w, 3), np.float32)
    V[..., 0] = u[None, :] * depth
    V[..., 1] = v[:, None] * depth
    V[..., 2] = depth
    return V


def normal_map(V, valid):
    """Normalen aus Nachbarpixeln; zeigen zur Kamera. Rand und Tiefensprünge ungültig."""
    h, w, _ = V.shape
    N = np.zeros_like(V)
    M = np.zeros((h, w), bool)
    c = V[1:-1, 1:-1]
    dx = V[1:-1, 2:] - V[1:-1, :-2]
    dy = V[2:, 1:-1] - V[:-2, 1:-1]
    n = np.cross(dy, dx)
    ln = np.linalg.norm(n, axis=-1)
    ok = (valid[1:-1, 1:-1] & valid[1:-1, 2:] & valid[1:-1, :-2] & valid[2:, 1:-1] & valid[:-2, 1:-1]
          & (ln > 1e-12))
    z = c[..., 2]
    ok &= (np.abs(dx[..., 2]) < 0.08 * z) & (np.abs(dy[..., 2]) < 0.08 * z)
    n = n / np.where(ln > 1e-12, ln, 1)[..., None]
    flip = (n * c).sum(-1) > 0
    n[flip] *= -1
    # sehr flach angeschnittene Flächen sind bei der D415 unzuverlässig
    cosv = -(n * c).sum(-1) / np.maximum(np.linalg.norm(c, axis=-1), 1e-6)
    ok &= cosv > 0.17
    N[1:-1, 1:-1] = np.where(ok[..., None], n, 0)
    M[1:-1, 1:-1] = ok
    return N, M


def glaetten(depth, valid, r=2):
    """Box-Filter (2r+1)² nur über gültige Pixel; an Kanten bleibt der Originalwert."""
    def box(a):
        c = np.cumsum(np.cumsum(np.pad(a, ((r + 1, r), (r + 1, r))), 0), 1)
        return c[2 * r + 1:, 2 * r + 1:] - c[:-2 * r - 1, 2 * r + 1:] - c[2 * r + 1:, :-2 * r - 1] + c[:-2 * r - 1, :-2 * r - 1]
    d = np.where(valid, depth, 0).astype(np.float64)
    s, n = box(d), box(valid.astype(np.float64))
    m = s / np.maximum(n, 1)
    out = np.where((n >= 4) & (np.abs(m - depth) < 0.04 * depth + 0.01), m, depth)
    return np.where(valid, out, 0).astype(np.float32)


class Bild:
    """Ein für das Tracking aufbereitetes Tiefenbild (Arbeitsauflösung)."""
    __slots__ = ("V", "N", "M", "fx", "fy", "ppx", "ppy")

    def __init__(self, depth_m, fx, fy, ppx, ppy, zmin, zmax):
        valid = (depth_m > zmin) & (depth_m < zmax)
        d = glaetten(depth_m, valid)
        self.V = vertex_map(d, fx, fy, ppx, ppy)
        self.N, self.M = normal_map(self.V, valid)
        self.fx, self.fy, self.ppx, self.ppy = fx, fy, ppx, ppy


def icp(cur, ref, T, stufen=((4, 6, 0.20), (2, 5, 0.10), (1, 4, 0.06))):
    """Projektives Punkt-zu-Ebene-ICP. T: Startschätzung cur->ref.
    Liefert (T, inlier_anteil, median_abweichung, min_eigenwert)."""
    T, frac, rmse, H = _icp(cur, ref, T, stufen)
    try:
        # Konditionierung: kleinster Eigenwert der normierten Hesse-Matrix
        ev = float(np.linalg.eigvalsh(H / max(np.trace(H), 1e-12))[0]) if H is not None else 0.0
    except Exception:
        ev = 0.0
    return T, frac, rmse, ev


def _icp(cur, ref, T, stufen):
    h, w = ref.M.shape
    fx, fy, ppx, ppy = ref.fx, ref.fy, ref.ppx, ref.ppy
    res = (T, 0.0, 1.0, None)
    for stride, iters, thr in stufen:
        Mc = cur.M[::stride, ::stride]
        P = cur.V[::stride, ::stride][Mc].astype(np.float64)
        Nc = cur.N[::stride, ::stride][Mc].astype(np.float64)
        if len(P) < 60:
            return res
        for _ in range(iters):
            R, t = T[:3, :3], T[:3, 3]
            p = P @ R.T + t
            z = p[:, 2]
            zs = np.where(z > 0.05, z, 1.0)
            u = np.rint(fx * p[:, 0] / zs + ppx).astype(np.int32)
            v = np.rint(fy * p[:, 1] / zs + ppy).astype(np.int32)
            ok = (z > 0.05) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
            ui, vi = np.where(ok, u, 0), np.where(ok, v, 0)
            ok &= ref.M[vi, ui]
            q = ref.V[vi, ui].astype(np.float64)
            n = ref.N[vi, ui].astype(np.float64)
            d = p - q
            ok &= (d * d).sum(1) < thr * thr
            ok &= ((Nc @ R.T) * n).sum(1) > 0.6
            cnt = int(ok.sum())
            if cnt < 40:
                return res
            p, n, d = p[ok], n[ok], d[ok]
            r = (d * n).sum(1)
            J = np.empty((cnt, 6))
            J[:, :3] = np.cross(p, n)
            J[:, 3:] = n
            k = 0.4 * thr
            wgt = np.where(np.abs(r) < k, 1.0, k / np.maximum(np.abs(r), 1e-9))
            wgt = wgt / np.maximum(p[:, 2], 0.5) ** 2      # Tiefenrauschen wächst mit z²
            Jw = J * wgt[:, None]
            H = Jw.T @ J
            g = Jw.T @ r
            try:
                x = -np.linalg.solve(H + np.eye(6) * 1e-6 * np.trace(H), g)
            except np.linalg.LinAlgError:
                return res
            T = se3_exp(x) @ T
            rmse = float(np.median(np.abs(r)))
            res = (T, cnt / max(len(P), 1), rmse, H)
            if np.linalg.norm(x[:3]) < 1e-5 and np.linalg.norm(x[3:]) < 1e-5:
                break
    return res


# --------------------------------------------------------------------------
# Voxel-Karte
# --------------------------------------------------------------------------
class VoxelKarte:
    """Gemittelte Punkte und Flächennormalen je Voxel (sortierte Hash-Schlüssel, nur numpy)."""
    OFF = 1 << 20
    MAXW = 25.0

    def __init__(self, voxel):
        self.voxel = float(voxel)
        self.keys = np.zeros(0, np.int64)
        self.pos = np.zeros((0, 3), np.float32)     # gewichtete Summe
        self.nor = np.zeros((0, 3), np.float32)
        self.w = np.zeros(0, np.float32)
        self.version = 0

    def __len__(self):
        return len(self.keys)

    def _hash(self, p):
        i = np.floor(p / self.voxel).astype(np.int64) + self.OFF
        return (i[:, 0] << 42) | (i[:, 1] << 21) | i[:, 2]

    def eintragen(self, p, c, wt=None):
        """p: Punkte, c: Normalen (je Nx3), wt: Gewichte."""
        if len(p) == 0:
            return
        k = self._hash(p)
        uk, inv = np.unique(k, return_inverse=True)
        wt = np.ones(len(p), np.float32) if wt is None else wt.astype(np.float32)
        cnt = np.bincount(inv, weights=wt).astype(np.float32)
        sp = np.stack([np.bincount(inv, weights=p[:, j] * wt) for j in range(3)], 1).astype(np.float32)
        sc = np.stack([np.bincount(inv, weights=c[:, j] * wt) for j in range(3)], 1).astype(np.float32)
        idx = np.searchsorted(self.keys, uk)
        if len(self.keys):
            idx_c = np.minimum(idx, len(self.keys) - 1)
            found = self.keys[idx_c] == uk
        else:
            idx_c, found = idx, np.zeros(len(uk), bool)
        if found.any():
            j = idx_c[found]
            w_old = self.w[j]
            # gleitender Mittelwert mit begrenztem Gewicht (alte Fehler verblassen)
            scale = np.where(w_old + cnt[found] > self.MAXW, (self.MAXW - cnt[found]).clip(1) / np.maximum(w_old, 1e-6), 1.0)
            self.pos[j] = self.pos[j] * scale[:, None] + sp[found]
            self.nor[j] = self.nor[j] * scale[:, None] + sc[found]
            self.w[j] = w_old * scale + cnt[found]
        nf = ~found
        if nf.any():
            ins = idx[nf]
            self.keys = np.insert(self.keys, ins, uk[nf])
            self.pos = np.insert(self.pos, ins, sp[nf], axis=0)
            self.nor = np.insert(self.nor, ins, sc[nf], axis=0)
            self.w = np.insert(self.w, ins, cnt[nf])
        self.version += 1

    def punkte(self, min_w=1.5):
        m = self.w >= min_w
        if m.sum() < 1000:
            m = self.w > 0
        w = self.w[m][:, None]
        n = self.nor[m]
        n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-6)
        return self.pos[m] / w, n


# --------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------
class Tracker:
    def __init__(self, voxel=0.02, zmin=0.2, zmax=3.5):
        self.voxel, self.zmin, self.zmax = voxel, zmin, zmax
        self.reset()

    def reset(self):
        self.karte = VoxelKarte(self.voxel)
        self.key = None             # Schlüsselbild
        self.key_pose = np.eye(4)   # Pose des Schlüsselbilds (Kamera -> Welt der ersten Kamera)
        self.pose = np.eye(4)       # aktuelle Pose
        self.prev_pose = np.eye(4)
        self.keys_alt = []          # frühere Schlüsselbilder zum Wiederfinden
        self.bahn = []              # Kamerapositionen
        self.verloren = 0
        self.bilder = 0
        self.letzte_eintragung = None
        self.guete = 0.0

    @staticmethod
    def arbeitsbild(depth_m, fx, fy, ppx, ppy):
        h, w = depth_m.shape
        s = max(1, int(round(w / ARBEITS_BREITE)))
        if s > 1:
            depth_m = depth_m[::s, ::s]
            fx, fy = fx / s, fy / s
            ppx, ppy = (ppx + 0.5) / s - 0.5, (ppy + 0.5) / s - 0.5
        return depth_m, fx, fy, ppx, ppy

    def verarbeiten(self, depth_m, fx, fy, ppx, ppy):
        """Ein neues Tiefenbild (Meter). Gibt 'ok', 'start' oder 'verloren' zurück."""
        depth_m, fx, fy, ppx, ppy = self.arbeitsbild(depth_m, fx, fy, ppx, ppy)
        cur = Bild(depth_m, fx, fy, ppx, ppy, self.zmin, self.zmax)
        if cur.M.sum() < 500:
            self.verloren += 1
            return "verloren"
        self.bilder += 1
        if self.key is None:
            self.key, self.key_pose = cur, np.eye(4)
            self.pose = self.prev_pose = np.eye(4)
            self._eintragen(cur, self.pose)
            self.bahn.append(self.pose[:3, 3].copy())
            return "start"

        # Bewegungsmodell: konstante Geschwindigkeit (nicht nach Verlust)
        if self.verloren == 0:
            vel = np.linalg.inv(self.prev_pose) @ self.pose
            if np.linalg.norm(vel[:3, 3]) > 0.15 or rot_angle(vel[:3, :3]) > 15:
                vel = np.eye(4)
            pred = self.pose @ vel
        else:
            pred = self.pose
        T0 = np.linalg.inv(self.key_pose) @ pred
        T, frac, rmse, ev = icp(cur, self.key, T0)
        good = frac > 0.30 and rmse < 0.02 and ev > 2e-4
        if good:
            step = np.linalg.inv(self.pose) @ (self.key_pose @ T)
            if np.linalg.norm(step[:3, 3]) > 0.35 or rot_angle(step[:3, :3]) > 30:
                good = False
        if not good:
            # Wiederfinden über frühere Schlüsselbilder in der Nähe
            for kb, kp in sorted(self.keys_alt, key=lambda e: np.linalg.norm(e[1][:3, 3] - self.pose[:3, 3]))[:3]:
                T2, f2, r2, e2 = icp(cur, kb, np.linalg.inv(kp) @ self.pose)
                if f2 > 0.35 and r2 < 0.02 and e2 > 2e-4:
                    self.key, self.key_pose = kb, kp
                    T, frac, rmse, good = T2, f2, r2, True
                    break
        self.guete = frac if good else 0.0
        if not good:
            self.verloren += 1
            return "verloren"
        self.verloren = 0
        self.prev_pose = self.pose
        self.pose = self.key_pose @ T
        self.bahn.append(self.pose[:3, 3].copy())

        # Karte ergänzen, wenn sich die Kamera bewegt hat (oder alle 10 Bilder)
        le = self.letzte_eintragung
        mv = np.linalg.inv(le[0]) @ self.pose
        if (np.linalg.norm(mv[:3, 3]) > 0.03 or rot_angle(mv[:3, :3]) > 3 or self.bilder - le[1] >= 10):
            self._eintragen(cur, self.pose)

        # neues Schlüsselbild?
        rel = np.linalg.inv(self.key_pose) @ self.pose
        if np.linalg.norm(rel[:3, 3]) > 0.12 or rot_angle(rel[:3, :3]) > 10 or frac < 0.55:
            self.keys_alt.append((self.key, self.key_pose))
            if len(self.keys_alt) > 400:
                self.keys_alt = self.keys_alt[-400:]
            self.key, self.key_pose = cur, self.pose.copy()
        return "ok"

    def _eintragen(self, cur, pose):
        m = cur.M
        P = cur.V[m]
        R = pose[:3, :3].astype(np.float32)
        z = P[:, 2]
        wt = np.clip(1.5 / np.maximum(z, 0.3), 0.3, 1.5)  # nahe Punkte sind genauer
        Pw = P @ R.T + pose[:3, 3].astype(np.float32)
        Nw = cur.N[m] @ R.T
        self.karte.eintragen(Pw, Nw, wt)
        self.letzte_eintragung = (pose.copy(), self.bilder)


# --------------------------------------------------------------------------
# Boden finden und ausrichten
# --------------------------------------------------------------------------
def rot_von_nach(a, b):
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K * (1 / (1 + c))


def boden_ausrichten(pos, start_xyz, rng=None):
    """Sucht die Bodenebene (waagerecht, unterhalb des Startpunkts) per RANSAC.
    Liefert 4x4-Matrix, die die Karte so dreht/verschiebt, dass der Boden bei Z=0 liegt."""
    rng = rng or np.random.default_rng(1)
    if len(pos) < 300:
        return np.eye(4), False
    sub = pos[rng.choice(len(pos), min(len(pos), 40000), replace=False)].astype(np.float64)
    kandidaten = []
    for _ in range(500):
        a, b, c = sub[rng.choice(len(sub), 3, replace=False)]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n /= ln
        if n[2] < 0:
            n = -n
        if n[2] < math.cos(math.radians(30)):
            continue
        d = -float(n @ a)
        unter = float(n @ start_xyz) + d          # Abstand des Startpunkts über der Ebene
        if unter < 0.4:
            continue
        inl = int((np.abs(sub @ n + d) < 0.025).sum())
        if inl >= 0.02 * len(sub):
            kandidaten.append((inl, unter, n, d))
    if not kandidaten:
        return np.eye(4), False
    # Tischplatten o. Ä. nicht für den Boden halten: unter den gut gestützten Ebenen die tiefste nehmen
    best = max(k[0] for k in kandidaten)
    _, _, best_n, best_d = max((k for k in kandidaten if k[0] >= 0.35 * best), key=lambda k: k[1])
    inl = sub[np.abs(sub @ best_n + best_d) < 0.025]
    # Ebene mit allen Inliern verfeinern
    ctr = inl.mean(0)
    _, _, vt = np.linalg.svd(inl - ctr, full_matrices=False)
    n = vt[2] if vt[2][2] > 0 else -vt[2]
    R = rot_von_nach(n, np.array([0.0, 0.0, 1.0]))
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = -np.array([0, 0, float((R @ ctr)[2])])
    return T, True


# --------------------------------------------------------------------------
# Hindernis-Raster für das Gehen (Kollision mit Wänden/Möbeln)
# --------------------------------------------------------------------------
class Hindernisse:
    def __init__(self, pos, zelle=0.05, z_von=0.25, z_bis=1.9):
        self.zelle = zelle
        m = (pos[:, 2] > z_von) & (pos[:, 2] < z_bis)
        p = pos[m]
        if len(p) == 0:
            self.grid = None
            return
        self.x0, self.y0 = float(p[:, 0].min()) - 1, float(p[:, 1].min()) - 1
        ix = ((p[:, 0] - self.x0) / zelle).astype(np.int32)
        iy = ((p[:, 1] - self.y0) / zelle).astype(np.int32)
        w, h = int(ix.max()) + 3, int(iy.max()) + 3
        cnt = np.zeros((w, h), np.int32)
        np.add.at(cnt, (ix, iy), 1)
        self.grid = cnt >= 2           # einzelne Ausreißer ignorieren

    def frei(self, x, y, radius=0.30):
        if self.grid is None:
            return True
        r = int(math.ceil(radius / self.zelle))
        cx, cy = int((x - self.x0) / self.zelle), int((y - self.y0) / self.zelle)
        w, h = self.grid.shape
        x0, x1, y0, y1 = max(cx - r, 0), min(cx + r + 1, w), max(cy - r, 0), min(cy + r + 1, h)
        if x0 >= x1 or y0 >= y1:
            return True
        return not self.grid[x0:x1, y0:y1].any()


# --------------------------------------------------------------------------
# Scan-Thread
# --------------------------------------------------------------------------
class ScanWorker(threading.Thread):
    """Holt Bilder vom Kamera-Thread und baut die Karte auf, solange der Scan läuft."""

    def __init__(self, cam, voxel=0.02):
        super().__init__(daemon=True, name="Raumscan")
        self.cam = cam
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.aktiv = False
        self.tracker = Tracker(voxel)
        self._reset_wunsch = None
        self._last_seq = -1
        self._snap = None
        self._snap_version = -1
        self.status = {"zustand": "bereit", "bilder": 0, "voxel": 0, "fps": 0.0, "guete": 0.0,
                       "verloren": 0, "pose": np.eye(4), "bahn": np.zeros((0, 3))}
        self._t = time.time()
        self._n = 0

    # ---- Schnittstelle
    def starten(self):
        self.aktiv = True

    def pausieren(self):
        self.aktiv = False

    def zuruecksetzen(self, voxel=None):
        with self.lock:
            self._reset_wunsch = voxel if voxel else self.tracker.voxel

    def stop(self):
        self.stop_flag.set()

    def get_status(self):
        with self.lock:
            return dict(self.status)

    def karte(self):
        """(version, pos Nx3 in Viewer-Welt, normalen Nx3, bahn Mx3, pose 4x4) – oder None."""
        with self.lock:
            return self._snap

    # ---- Thread
    def run(self):
        last_snap = 0.0
        while not self.stop_flag.is_set():
            with self.lock:
                rw, self._reset_wunsch = self._reset_wunsch, None
            if rw is not None:
                self.tracker.voxel = rw
                self.tracker.reset()
                with self.lock:
                    self._snap = None
                    self._snap_version = -1
                    self.status.update(zustand="bereit", bilder=0, voxel=0, guete=0.0, verloren=0,
                                       pose=np.eye(4), bahn=np.zeros((0, 3)))
            f = self.cam.latest() if self.aktiv else None
            if f is None or f.seq == self._last_seq:
                if not self.aktiv:
                    with self.lock:
                        if self.status["zustand"] not in ("bereit",):
                            self.status["zustand"] = "pause"
                time.sleep(0.01)
                continue
            self._last_seq = f.seq
            try:
                depth_m = f.depth.astype(np.float32) * f.units
                # nur Tiefe – das Farbbild wird bewusst nicht verwendet
                z = self.tracker.verarbeiten(depth_m, f.fx, f.fy, f.ppx, f.ppy)
            except Exception as exc:  # pragma: no cover
                log.exception("Raumscan: %s", exc)
                z = "verloren"
            self._n += 1
            now = time.time()
            fps = None
            if now - self._t >= 1.0:
                fps = self._n / (now - self._t)
                self._t, self._n = now, 0
            tr = self.tracker
            pose_v = C_VIEW @ tr.pose @ np.linalg.inv(C_VIEW)
            with self.lock:
                self.status.update(zustand=z if z != "start" else "ok", bilder=tr.bilder, voxel=len(tr.karte),
                                   guete=tr.guete, verloren=tr.verloren, pose=pose_v)
                if fps is not None:
                    self.status["fps"] = fps
            if now - last_snap > 0.6 and tr.karte.version != self._snap_version:
                last_snap = now
                self._make_snap()

    def _make_snap(self):
        tr = self.tracker
        pos, nor = tr.karte.punkte()
        pos_v = np.empty_like(pos)
        pos_v[:, 0], pos_v[:, 1], pos_v[:, 2] = pos[:, 0], pos[:, 2], -pos[:, 1]
        nor_v = np.empty_like(nor)
        nor_v[:, 0], nor_v[:, 1], nor_v[:, 2] = nor[:, 0], nor[:, 2], -nor[:, 1]
        bahn = np.array(tr.bahn, np.float32) if tr.bahn else np.zeros((0, 3), np.float32)
        bahn_v = np.empty_like(bahn)
        if len(bahn):
            bahn_v[:, 0], bahn_v[:, 1], bahn_v[:, 2] = bahn[:, 0], bahn[:, 2], -bahn[:, 1]
        pose_v = C_VIEW @ tr.pose @ np.linalg.inv(C_VIEW)
        with self.lock:
            self._snap_version = tr.karte.version
            self._snap = (tr.karte.version, pos_v.astype(np.float32), nor_v.astype(np.float32), bahn_v, pose_v)
            self.status["bahn"] = bahn_v


# --------------------------------------------------------------------------
# Raum speichern / laden (PLY)
# --------------------------------------------------------------------------
META_PREFIX = "mk2 "


def ply_schreiben(path, pos, col01, meta=None, programm="", nor=None):
    """Binäres PLY (Meter, Z oben), optional mit Normalen.
    meta: dict -> Kommentarzeilen 'mk2 schluessel wert'."""
    n = len(pos)
    felder = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if nor is not None:
        felder += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")]
    dt = np.dtype(felder + [("r", "u1"), ("g", "u1"), ("b", "u1")])
    arr = np.empty(n, dt)
    arr["x"], arr["y"], arr["z"] = pos[:, 0], pos[:, 1], pos[:, 2]
    if nor is not None:
        arr["nx"], arr["ny"], arr["nz"] = nor[:, 0], nor[:, 1], nor[:, 2]
    c = (np.clip(col01, 0, 1) * 255 + 0.5).astype(np.uint8)
    arr["r"], arr["g"], arr["b"] = c[:, 0], c[:, 1], c[:, 2]
    head = ["ply", "format binary_little_endian 1.0",
            f"comment {programm} - Raumscan, Meter, Z nach oben, Boden bei Z=0".strip()]
    for k, v in (meta or {}).items():
        head.append(f"comment {META_PREFIX}{k} {v}")
    head += [f"element vertex {n}", "property float x", "property float y", "property float z"]
    if nor is not None:
        head += ["property float nx", "property float ny", "property float nz"]
    head += ["property uchar red", "property uchar green", "property uchar blue", "end_header"]
    with open(path, "wb") as fh:
        fh.write(("\n".join(head) + "\n").encode("ascii", "replace"))
        fh.write(arr.tobytes())


_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
              "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
              "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def ply_lesen(path):
    """Liest die Punkte (Vertices) einer PLY-Datei.
    Liefert (pos Nx3 float32, farbe Nx3 0..1, normalen Nx3 oder None, meta dict).
    Flächen werden ignoriert – ein Netz wird als Punktwolke seiner Eckpunkte geladen."""
    with open(path, "rb") as fh:
        if fh.readline().strip() != b"ply":
            raise ValueError("Keine PLY-Datei")
        fmt, elements, meta = None, [], {}
        while True:
            line = fh.readline()
            if not line:
                raise ValueError("PLY-Kopf unvollständig")
            t = line.decode("latin-1").strip().split()
            if not t:
                continue
            if t[0] == "format":
                fmt = t[1]
            elif t[0] == "comment" and len(t) >= 3 and " ".join(t[1:]).startswith(META_PREFIX.strip()):
                meta[t[2]] = " ".join(t[3:])
            elif t[0] == "element":
                elements.append([t[1], int(t[2]), []])
            elif t[0] == "property":
                if t[1] == "list":
                    elements[-1][2].append((t[4], "list", t[2], t[3]))
                else:
                    elements[-1][2].append((t[2], _PLY_TYPES.get(t[1], "f4"), None, None))
            elif t[0] == "end_header":
                break
        if fmt not in ("binary_little_endian", "binary_big_endian", "ascii"):
            raise ValueError(f"PLY-Format {fmt} nicht unterstützt")
        vi = next((i for i, e in enumerate(elements) if e[0] == "vertex"), None)
        if vi is None:
            raise ValueError("PLY enthält keine Punkte")
        end = "<" if fmt == "binary_little_endian" else ">"
        if fmt == "ascii":
            for e in elements[:vi]:
                for _ in range(e[1]):
                    fh.readline()
            name, n, props = elements[vi]
            names = [p[0] for p in props if p[1] != "list"]
            data = np.loadtxt(fh, max_rows=n, ndmin=2,
                              usecols=range(len(names)) if all(p[1] != "list" for p in props) else None)
            cols = {nm: data[:, i] for i, nm in enumerate(names)}
        else:
            for e in elements[:vi]:
                if any(p[1] == "list" for p in e[2]):
                    raise ValueError("PLY: Punkte stehen hinter Listen-Elementen (nicht unterstützt)")
                dt = np.dtype([(p[0], end + p[1]) for p in e[2]])
                fh.seek(dt.itemsize * e[1], 1)
            name, n, props = elements[vi]
            if any(p[1] == "list" for p in props):
                raise ValueError("PLY: Listen in Punkten nicht unterstützt")
            dt = np.dtype([(p[0], end + p[1]) for p in props])
            raw = np.frombuffer(fh.read(dt.itemsize * n), dt, count=n)
            cols = {nm: raw[nm] for nm in dt.names}
    if not all(k in cols for k in ("x", "y", "z")):
        raise ValueError("PLY: Koordinaten x/y/z fehlen")
    pos = np.stack([cols["x"], cols["y"], cols["z"]], 1).astype(np.float32)
    keys = next((k for k in (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green", "diffuse_blue"))
                 if all(c in cols for c in k)), None)
    if keys:
        col = np.stack([cols[k] for k in keys], 1).astype(np.float32)
        if col.max() > 1.0:
            col /= 255.0
    else:
        col = np.full((len(pos), 3), 0.7, np.float32)
    nor = None
    if all(k in cols for k in ("nx", "ny", "nz")):
        nor = np.stack([cols["nx"], cols["ny"], cols["nz"]], 1).astype(np.float32)
    ok = np.isfinite(pos).all(1)
    return pos[ok], np.clip(col[ok], 0, 1), (nor[ok] if nor is not None else None), meta


# --------------------------------------------------------------------------
# Einfärben (ohne Farbkamera): Schattierung aus den Flächennormalen
# --------------------------------------------------------------------------
LICHT = np.array([0.35, 0.55, 0.76], np.float32) / np.linalg.norm([0.35, 0.55, 0.76])


def einfaerben(pos, nor, modus, lut=None):
    """modus 'hoehe': Farbskala nach Höhe (Z), 'grau': neutral. Beides schattiert.
    pos/nor in der ausgerichteten Welt (Boden Z=0). lut: 256x3 float (0..1)."""
    shade = 0.32 + 0.68 * np.abs(nor @ LICHT)
    if modus == "hoehe" and lut is not None and len(pos):
        z = pos[:, 2]
        top = max(2.0, float(np.percentile(z, 98)))
        idx = np.clip((z / top * 255), 0, 255).astype(np.int32)
        base = lut[idx] * 0.85 + 0.15
    else:
        base = np.broadcast_to(np.array([0.80, 0.84, 0.86], np.float32), (len(pos), 3))
    return np.clip(base * shade[:, None], 0, 1).astype(np.float32)
