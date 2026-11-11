# -*- coding: utf-8 -*-
"""
Begehbare 3D-Ansicht für RealSense-Viewer-MK2.

Normalbetrieb wie die übrige 3D-Ansicht (Kreisen um einen Mittelpunkt). Im Modus
„Begehen“ sitzt die Kamera in Augenhöhe im gescannten Raum:
W A S D / Pfeiltasten gehen, Maus ziehen = umsehen, Q/E runter/hoch (Fliegen),
Umschalt = schneller, R = zum Startpunkt, F = Gehen/Fliegen, Esc = beenden.
"""

import math
import time

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMatrix4x4, QPainter, QPen, QPolygonF, QVector3D
from PySide6.QtWidgets import QApplication


class Minikarte:
    """Grundriss von oben: Boden dunkel, Wände/Möbel hell."""

    def __init__(self, pos, zelle=0.05):
        self.img = None
        if len(pos) < 50:
            return
        lo = np.percentile(pos[:, :2], 0.5, axis=0) - 0.3
        hi = np.percentile(pos[:, :2], 99.5, axis=0) + 0.3
        self.x0, self.y0, self.zelle = float(lo[0]), float(lo[1]), zelle
        w = int(min(1200, max(4, (hi[0] - lo[0]) / zelle)))
        h = int(min(1200, max(4, (hi[1] - lo[1]) / zelle)))
        ix = ((pos[:, 0] - self.x0) / zelle).astype(np.int32)
        iy = ((pos[:, 1] - self.y0) / zelle).astype(np.int32)
        ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        z = pos[:, 2]
        boden = np.zeros((h, w), bool)
        wand = np.zeros((h, w), np.int32)
        m = ok & (z < 0.25)
        boden[iy[m], ix[m]] = True
        m = ok & (z >= 0.25) & (z < 1.9)
        np.add.at(wand, (iy[m], ix[m]), 1)
        rgba = np.zeros((h, w, 4), np.uint8)
        rgba[boden] = (25, 70, 68, 220)
        rgba[wand >= 2] = (215, 221, 227, 235)
        rgba = np.ascontiguousarray(rgba[::-1])            # Bild: Zeile 0 oben = größtes y
        self.w, self.h = w, h
        self.img = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


class RaumView(gl.GLViewWidget):
    walkChanged = Signal(bool)
    modusChanged = Signal(str)
    moved = Signal()

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self.walk = False
        self.eye = np.array([0.0, 0.0, 1.6])
        self.yaw, self.pitch = 90.0, 0.0
        self.modus = "gehen"
        self.kollision = True
        self.augenhoehe = 1.6
        self.tempo = 1.4
        self.hind = None
        self.minikarte = None
        self.start = (0.0, 0.0, 90.0)
        self.thumb = None             # kleines Live-Kamerabild (nur beim Scannen)
        self.thumb_text = ""
        self.keys = set()
        self._last_t = None
        self._drag = None
        self._orbit = None
        self.move_timer = QTimer(self, interval=16, timeout=self._tick)

    # ------------------------------------------------------------ Modus
    def set_walk(self, on):
        if on == self.walk:
            return
        self.walk = on
        if on:
            self._orbit = {k: self.opts[k] for k in ("distance", "center", "elevation", "azimuth")}
            self.opts["distance"] = 20.0      # Nahebene = 2 cm, Fernebene weit weg
            self._last_t = None
            self.move_timer.start()
            self.setCursor(Qt.OpenHandCursor)
            self.setFocus()
        else:
            if self._orbit:
                self.opts.update(self._orbit)
            # Kreisansicht dort weiterführen, wo man gerade steht
            self.opts["center"] = pg.Vector(*self.eye)
            self.move_timer.stop()
            self.keys.clear()
            self._drag = None
            self.unsetCursor()
        self.update()
        self.walkChanged.emit(on)

    def set_modus(self, modus):
        self.modus = "fliegen" if modus == "fliegen" else "gehen"
        if self.modus == "gehen":
            self.eye[2] = self.augenhoehe
            self.pitch = max(-60.0, min(60.0, self.pitch))
        self.update()
        self.moved.emit()

    def zum_start(self):
        x, y, yaw = self.start
        self.eye = np.array([x, y, self.augenhoehe])
        self.yaw, self.pitch = yaw, -5.0
        self.update()
        self.moved.emit()

    def _fwd(self):
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        return np.array([math.cos(y) * math.cos(p), math.sin(y) * math.cos(p), math.sin(p)])

    # ------------------------------------------------------------ Kamera
    def viewMatrix(self):
        if not self.walk:
            return super().viewMatrix()
        e, f = self.eye, self._fwd()
        tr = QMatrix4x4()
        tr.lookAt(QVector3D(*map(float, e)), QVector3D(*map(float, e + f)), QVector3D(0.0, 0.0, 1.0))
        return tr

    def cameraPosition(self):
        if not self.walk:
            return super().cameraPosition()
        return pg.Vector(*map(float, self.eye))

    # ------------------------------------------------------------ Eingaben
    def mousePressEvent(self, ev):
        self.setFocus()
        if not self.walk:
            return super().mousePressEvent(ev)
        self._drag = ev.position()
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        if not self.walk:
            return super().mouseMoveEvent(ev)
        if self._drag is None:
            return
        d = ev.position() - self._drag
        self._drag = ev.position()
        self.yaw = (self.yaw - d.x() * 0.22) % 360
        lim = 85.0 if self.modus == "fliegen" else 60.0
        self.pitch = max(-lim, min(lim, self.pitch - d.y() * 0.22))
        self.update()
        self.moved.emit()

    def mouseReleaseEvent(self, ev):
        if not self.walk:
            return super().mouseReleaseEvent(ev)
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, ev):
        if not self.walk:
            return super().wheelEvent(ev)
        self._move(0.25 if ev.angleDelta().y() > 0 else -0.25, 0.0, 0.0)

    def keyPressEvent(self, ev):
        if not self.walk:
            return super().keyPressEvent(ev)
        if ev.isAutoRepeat():
            return
        k = ev.key()
        if k == Qt.Key_Escape:
            self.set_walk(False)
        elif k == Qt.Key_R:
            self.zum_start()
        elif k == Qt.Key_F:
            self.set_modus("gehen" if self.modus == "fliegen" else "fliegen")
            self.modusChanged.emit(self.modus)
        else:
            self.keys.add(k)
        ev.accept()

    def keyReleaseEvent(self, ev):
        if not self.walk:
            return super().keyReleaseEvent(ev)
        if not ev.isAutoRepeat():
            self.keys.discard(ev.key())
        ev.accept()

    def focusOutEvent(self, ev):
        self.keys.clear()
        super().focusOutEvent(ev)

    # ------------------------------------------------------------ Bewegung
    def _tick(self):
        now = time.perf_counter()
        dt = 0.0 if self._last_t is None else min(0.1, now - self._last_t)
        self._last_t = now
        K = self.keys
        if not K or dt <= 0:
            return
        fw = int(Qt.Key_W in K or Qt.Key_Up in K) - int(Qt.Key_S in K or Qt.Key_Down in K)
        sd = int(Qt.Key_D in K) - int(Qt.Key_A in K)
        up = int(Qt.Key_E in K or Qt.Key_PageUp in K) - int(Qt.Key_Q in K or Qt.Key_PageDown in K)
        turn = int(Qt.Key_Left in K) - int(Qt.Key_Right in K)
        if turn:
            self.yaw = (self.yaw + turn * 90.0 * dt) % 360
        v = self.tempo * dt * (2.5 if QApplication.keyboardModifiers() & Qt.ShiftModifier else 1.0)
        if fw or sd or up:
            self._move(fw * v, sd * v, up * v)
        elif turn:
            self.update()
            self.moved.emit()

    def _move(self, forward, side, up):
        y = math.radians(self.yaw)
        if self.modus == "fliegen":
            f = self._fwd()
        else:
            f = np.array([math.cos(y), math.sin(y), 0.0])
        r = np.array([math.sin(y), -math.cos(y), 0.0])
        new = self.eye + f * forward + r * side
        if self.modus == "fliegen":
            new[2] += up
        else:
            new[2] = self.augenhoehe
            h = self.hind
            if self.kollision and h is not None and h.frei(self.eye[0], self.eye[1]) and not h.frei(new[0], new[1]):
                # an Wand oder Möbel entlanggleiten statt stehenbleiben
                if h.frei(new[0], self.eye[1]):
                    new[1] = self.eye[1]
                elif h.frei(self.eye[0], new[1]):
                    new[0] = self.eye[0]
                else:
                    new[:2] = self.eye[:2]
        self.eye = new
        self.update()
        self.moved.emit()

    # ------------------------------------------------------------ Überlagerung
    def paintGL(self, *args, **kwargs):
        super().paintGL(*args, **kwargs)
        if not self.walk and self.thumb is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.walk:
            self._paint_walk(p)
        elif self.thumb is not None:
            self._paint_thumb(p)
        p.end()

    def _paint_thumb(self, p):
        img = self.thumb
        w = min(260, self.width() // 3)
        h = w * img.height() / max(1, img.width())
        r = QRectF(self.width() - w - 12, 12, w, h)
        p.drawImage(r, img)
        p.setPen(QPen(QColor(255, 176, 0, 170), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)
        if self.thumb_text:
            f = QFont()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(r.x(), r.bottom() + 3, r.width(), 16), Qt.AlignRight, self.thumb_text)

    def _paint_walk(self, p):
        cx, cy = self.width() / 2, self.height() / 2
        p.setPen(QPen(QColor(255, 176, 0, 190), 1.5))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p.drawLine(QPointF(cx + dx * 5, cy + dy * 5), QPointF(cx + dx * 13, cy + dy * 13))
        mk = self.minikarte
        if mk is None or mk.img is None:
            return
        box = 190
        s = min(box / mk.w, box / mk.h)
        w, h = mk.w * s, mk.h * s
        r = QRectF(self.width() - w - 14, 14, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(7, 9, 11, 200))
        p.drawRoundedRect(r.adjusted(-6, -6, 6, 6), 6, 6)
        p.drawImage(r, mk.img)
        # eigene Position und Blickrichtung
        px = r.x() + (self.eye[0] - mk.x0) / mk.zelle * s
        py = r.bottom() - (self.eye[1] - mk.y0) / mk.zelle * s
        a = math.radians(self.yaw)
        tip = QPointF(px + math.cos(a) * 11, py - math.sin(a) * 11)
        l = QPointF(px + math.cos(a + 2.5) * 7, py - math.sin(a + 2.5) * 7)
        rr = QPointF(px + math.cos(a - 2.5) * 7, py - math.sin(a - 2.5) * 7)
        p.setBrush(QColor(255, 176, 0))
        p.setPen(QPen(QColor(7, 9, 11), 1))
        p.drawPolygon(QPolygonF([tip, l, rr]))
        sx = r.x() + (self.start[0] - mk.x0) / mk.zelle * s
        sy = r.bottom() - (self.start[1] - mk.y0) / mk.zelle * s
        p.setBrush(QColor(25, 211, 197))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(sx, sy), 3, 3)
