#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RealSense-Viewer-MK2 – Desktop-Programm
Zeigt das 3D-Bild (Punktwolke), das Tiefenbild und das Farbbild einer per USB
angeschlossenen Intel RealSense D415 in einem eigenen Programmfenster an
(Qt/PySide6 + OpenGL), im Design der Dashboards.
Neu in MK2: Raumscan – die Kamera wird durch den Raum bewegt, daraus entsteht eine
zusammenhängende 3D-Karte, durch die man anschließend wie in einem virtuellen Raum
gehen und sich umsehen kann – auch mit einer 3Dconnexion-3D-Maus. Der Raumscan nutzt nur
die Tiefenkameras (Stereo-IR) und funktioniert daher auch in völliger Dunkelheit.
"""

import logging
import math
import os
import struct
import sys
import time
from pathlib import Path

APP_NAME = "RealSense-Viewer-MK2"
APP_VERSION = "1.3.1"

import numpy as np  # noqa: E402

import kamera  # noqa: E402
import raumscan  # noqa: E402
import spacemouse  # noqa: E402
from kamera import (AUFLOESUNGEN, ASSET_DIR, BASE_DIR, CONFIG_PATH, FPS_WERTE,  # noqa: E402
                    CameraWorker, cfg_get, cfg_save, cfg_set, truthy)

# --------------------------------------------------------------------------
# Protokoll in Datei (bei Fenster-exe gibt es keine Konsole)
# --------------------------------------------------------------------------
LOG_PATH = BASE_DIR / "realsense-viewer.log"
_handlers = []
try:
    _handlers.append(logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"))
except Exception:
    pass
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler(sys.stderr))
logging.basicConfig(level=logging.WARNING, handlers=_handlers or [logging.NullHandler()],
                    format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(APP_NAME)
log.setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Selbsttest (für den GitHub-Actions-Build)
# --------------------------------------------------------------------------
def selbsttest():
    lines, ok = [f"{APP_NAME} v{APP_VERSION} – Selbsttest"], True

    def chk(name, fn):
        nonlocal ok
        try:
            info = fn()
            lines.append(f"  [OK] {name} {info or ''}")
        except Exception as exc:
            ok = False
            lines.append(f"  [FEHLER] {name}: {exc}")

    chk("Symbole", lambda: ((ASSET_DIR / "icon.png").stat().st_size, (ASSET_DIR / "pfeil.png").stat().st_size) and "")
    chk("PySide6", lambda: __import__("PySide6").__version__)
    chk("pyqtgraph", lambda: __import__("pyqtgraph").__version__)
    chk("pyqtgraph.opengl", lambda: __import__("pyqtgraph.opengl") and "")
    chk("PyOpenGL", lambda: __import__("OpenGL").__version__)

    def rs_check():
        if kamera.rs is None:
            raise RuntimeError(kamera.RS_IMPORT_ERROR)
        n = len(kamera.rs.context().query_devices())
        return f"{getattr(kamera.rs, '__version__', '?')} – {n} Gerät(e)"
    chk("pyrealsense2", rs_check)

    def scan_check():
        import demoraum
        w, h, f = 320, 180, 225.0
        tr = raumscan.Tracker(0.02)
        for i in range(3):
            z, _rgb = demoraum.render(w, h, f, f, (w - 1) / 2, (h - 1) / 2, *demoraum.kamera_pose(i / 15))
            ergebnis = tr.verarbeiten(z, f, f, (w - 1) / 2, (h - 1) / 2)     # nur Tiefe
        if ergebnis != "ok":
            raise RuntimeError(f"Tracking: {ergebnis}")
        return f"– {len(tr.karte)} Voxel"
    chk("Raumscan", scan_check)

    def maus_check():
        bericht = bytes([1]) + struct.pack("<6h", 100, -200, 0, 0, 50, -350)
        sm = spacemouse.SpaceMouse()
        sm.verarbeite_hid(bericht, 1, len(bericht))
        if sm.roh != [100, -200, 0, 0, 50, -350]:
            raise RuntimeError(f"HID-Auswertung falsch: {sm.roh}")
        return "– Raw Input" if spacemouse.IST_WINDOWS else "– (nur Windows)"
    chk("3D-Maus", maus_check)
    lines.append("Ergebnis: " + ("OK" if ok else "FEHLER"))
    text = "\n".join(lines)
    try:
        (BASE_DIR / "selbsttest.txt").write_text(text + "\n", encoding="utf-8")
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            print(text)
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__" and "--selbsttest" in sys.argv:
    sys.exit(selbsttest())

# --------------------------------------------------------------------------
# Qt
# --------------------------------------------------------------------------
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QImage, QKeySequence,  # noqa: E402
                           QLinearGradient, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,  # noqa: E402
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QStackedWidget,
                               QVBoxLayout, QWidget)

import pyqtgraph as pg  # noqa: E402
import pyqtgraph.opengl as gl  # noqa: E402

from raumansicht import Minikarte, RaumView  # noqa: E402

# Farben wie in den Dashboards
BG, BG2, PANEL, PANEL2 = "#0b0e11", "#0f1317", "#12161b", "#171c22"
LINE, LINE2 = "#232a32", "#2e3740"
TEXT, MUTED = "#d7dde3", "#7d8893"
AMBER, TEAL, RED = "#ffb000", "#19d3c5", "#ff5a5f"
MONO = "Cascadia Mono, Consolas, JetBrains Mono, DejaVu Sans Mono, monospace"

ARROW = (ASSET_DIR / "pfeil.png").as_posix()

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, #root {{ background: {BG}; }}
#topbar {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #11161b, stop:1 #0d1115); border-bottom: 1px solid {LINE}; }}
#brandTitle {{ color: {AMBER}; font-weight: 700; font-size: 11.5pt; letter-spacing: 2px; }}
#brandSub {{ color: {MUTED}; font-size: 7.5pt; letter-spacing: 2px; }}
#tabs {{ background: {BG}; border: 1px solid {LINE}; border-radius: 8px; }}
#tabs QPushButton {{ background: transparent; border: 0; border-radius: 6px; padding: 6px 16px; color: {MUTED};
    font-size: 8.5pt; font-weight: 600; letter-spacing: 2px; }}
#tabs QPushButton:hover {{ color: {TEXT}; }}
#tabs QPushButton:checked {{ background: rgba(255,176,0,36); color: {AMBER}; border: 1px solid rgba(255,176,0,90); }}
#pill {{ background: {PANEL}; border: 1px solid {LINE2}; border-radius: 13px; padding: 4px 12px;
    font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
#pill[state="ok"] {{ color: {TEAL}; border-color: rgba(25,211,197,110); }}
#pill[state="demo"] {{ color: {AMBER}; border-color: rgba(255,176,0,110); }}
#pill[state="err"] {{ color: {RED}; border-color: rgba(255,90,95,110); }}
#clock {{ color: {MUTED}; font-family: {MONO}; font-size: 9pt; }}

#sidebar, #sidebarInner {{ background: {BG2}; border: 0; }}
#sidebar {{ border-right: 1px solid {LINE}; }}
#panel {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; }}
#panelTitle {{ color: {MUTED}; font-size: 8pt; font-weight: 700; letter-spacing: 3px; }}
#tick {{ background: {AMBER}; }}
#kvKey {{ color: {MUTED}; font-size: 9pt; }}
#kvVal {{ font-size: 9pt; }}
#kvValMono {{ font-family: {MONO}; font-size: 9pt; }}
#fieldLabel {{ color: {MUTED}; font-size: 9pt; }}
#fieldValue {{ color: {AMBER}; font-family: {MONO}; font-size: 9pt; }}
#hint {{ color: {MUTED}; font-size: 8.5pt; }}

QComboBox, QLineEdit {{ background: {PANEL2}; border: 1px solid {LINE2}; border-radius: 7px; padding: 6px 9px; }}
QComboBox:focus, QLineEdit:focus {{ border-color: {AMBER}; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox::down-arrow {{ image: url("{ARROW}"); width: 10px; height: 6px; margin-right: 8px; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {LINE2}; selection-background-color: rgba(255,176,0,50);
    selection-color: {AMBER}; outline: 0; }}
QComboBox:disabled, QSlider:disabled {{ color: #4a535c; }}

QSlider::groove:horizontal {{ height: 4px; background: {LINE2}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: rgba(255,176,0,120); border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {AMBER}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 2px solid {BG}; }}
QSlider::handle:horizontal:disabled {{ background: #4a535c; }}

#seg {{ background: {BG}; border: 1px solid {LINE2}; border-radius: 7px; }}
#seg QPushButton {{ background: transparent; border: 0; border-radius: 5px; padding: 6px 0; color: {MUTED};
    font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
#seg QPushButton:hover {{ color: {TEXT}; }}
#seg QPushButton:checked {{ background: rgba(25,211,197,33); color: {TEAL}; border: 1px solid rgba(25,211,197,90); }}

QPushButton#btn {{ background: {PANEL2}; border: 1px solid {LINE2}; border-radius: 7px; padding: 8px 10px;
    font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
QPushButton#btn:hover {{ border-color: {TEAL}; color: {TEAL}; }}
QPushButton#btn:checked {{ border-color: {AMBER}; color: {AMBER}; background: rgba(255,176,0,36); }}
QPushButton#btnAccent {{ background: rgba(255,176,0,36); border: 1px solid rgba(255,176,0,130); border-radius: 7px;
    padding: 8px 10px; color: {AMBER}; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
QPushButton#btnAccent:hover {{ background: rgba(255,176,0,56); }}

#stage {{ background: {BG}; }}
#pane {{ background: #07090b; border: 1px solid {LINE}; border-radius: 10px; }}
#paneLabel {{ color: {MUTED}; font-size: 8pt; font-weight: 700; letter-spacing: 3px; }}
#hud {{ color: {TEAL}; font-family: {MONO}; font-size: 8.5pt; }}
#hudDim {{ color: #4c5660; font-size: 7.5pt; letter-spacing: 1px; }}
#badge {{ color: {AMBER}; background: rgba(255,176,0,36); border: 1px solid rgba(255,176,0,115); border-radius: 6px;
    padding: 3px 9px; font-size: 8pt; font-weight: 700; letter-spacing: 2px; }}
#badgeTeal {{ color: {TEAL}; background: rgba(25,211,197,33); border: 1px solid rgba(25,211,197,115); border-radius: 6px;
    padding: 3px 9px; font-size: 8pt; font-weight: 700; letter-spacing: 2px; }}
#badgeRed {{ color: {RED}; background: rgba(255,90,95,33); border: 1px solid rgba(255,90,95,115); border-radius: 6px;
    padding: 3px 9px; font-size: 8pt; font-weight: 700; letter-spacing: 2px; }}
#scanState {{ font-family: {MONO}; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }}
#scanState[state="ok"] {{ color: {TEAL}; }}
#scanState[state="lost"] {{ color: {RED}; }}
#scanState[state="idle"] {{ color: {MUTED}; }}
#scanState[state="file"] {{ color: {AMBER}; }}
#noSignalTitle {{ color: {RED}; font-size: 15pt; font-weight: 700; letter-spacing: 6px; }}
#noSignalText {{ color: {MUTED}; }}

#statusbar {{ background: #0d1115; border-top: 1px solid {LINE}; }}
#statusMsg {{ font-family: {MONO}; font-size: 8.5pt; }}
#statusDim {{ color: {MUTED}; font-family: {MONO}; font-size: 8.5pt; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {LINE2}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {LINE2}; padding: 4px; }}
QMessageBox {{ background: {PANEL}; }}
"""

# --------------------------------------------------------------------------
# Turbo-Farbskala
# --------------------------------------------------------------------------
def _turbo_lut():
    x = np.linspace(0, 1, 256)
    r = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))))
    g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))))
    b = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))))
    return np.clip(np.stack([r, g, b], -1), 0, 1).astype(np.float32)


TURBO_F = _turbo_lut()                              # 256x3 float
TURBO_U8 = (TURBO_F * 255).astype(np.uint8)         # 256x3 uint8
AMBER_F = np.array([1.0, 176 / 255, 0.0], np.float32)
TEAL_F = np.array([25 / 255, 211 / 255, 197 / 255], np.float32)


# --------------------------------------------------------------------------
# Eigene Bedienelemente
# --------------------------------------------------------------------------
class Toggle(QCheckBox):
    """Schiebeschalter im Dashboard-Stil."""

    def __init__(self, text):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(24)

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(44 + fm.horizontalAdvance(self.text()), 24)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        alpha = 1.0 if self.isEnabled() else 0.35
        p.setOpacity(alpha)
        y = (self.height() - 18) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(25, 211, 197, 90) if on else QColor(LINE2))
        p.drawRoundedRect(QRectF(0, y, 34, 18), 9, 9)
        p.setBrush(QColor(TEAL) if on else QColor("#9aa5b0"))
        p.drawEllipse(QRectF(18 if on else 2, y + 2, 14, 14))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(44, 0, self.width() - 44, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


class Segmented(QFrame):
    changed = Signal(str)

    def __init__(self, items, name="seg"):
        super().__init__()
        self.setObjectName(name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for value, label in items:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.changed.emit(v))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[value] = b

    def set_value(self, value):
        b = self.buttons.get(str(value))
        if b:
            b.setChecked(True)
        else:
            self.group.setExclusive(False)
            for x in self.buttons.values():
                x.setChecked(False)
            self.group.setExclusive(True)


class Panel(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("panel")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 11, 12, 12)
        self.lay.setSpacing(9)
        head = QHBoxLayout()
        head.setSpacing(8)
        tick = QFrame()
        tick.setObjectName("tick")
        tick.setFixedSize(10, 2)
        lbl = QLabel(title)
        lbl.setObjectName("panelTitle")
        head.addWidget(tick, 0, Qt.AlignVCenter)
        head.addWidget(lbl)
        head.addStretch()
        self.lay.addLayout(head)

    def add(self, w):
        if isinstance(w, QWidget):
            self.lay.addWidget(w)
        else:
            self.lay.addLayout(w)
        return w


def gruppe(panel):
    """Unsichtbarer Behälter in einem Panel, damit mehrere Zeilen gemeinsam ein-/ausgeblendet werden."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(9)
    panel.add(w)
    return w, lay


ALLE_ANSICHTEN = ("3d", "tiefe", "farbe", "geteilt", "raum")


def field_header(text):
    row = QHBoxLayout()
    l = QLabel(text)
    l.setObjectName("fieldLabel")
    v = QLabel("")
    v.setObjectName("fieldValue")
    row.addWidget(l)
    row.addStretch()
    row.addWidget(v)
    return row, v


def make_label(text, name):
    l = QLabel(text)
    l.setObjectName(name)
    return l


def make_button(text, name="btn", checkable=False):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    return b


class Pane(QFrame):
    """Rahmen mit Titel, HUD-Ecken (Amber) und Fußzeile."""

    def __init__(self, title, body):
        super().__init__()
        self.setObjectName("pane")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(make_label(title, "paneLabel"))
        head.addStretch()
        self.badges = QHBoxLayout()
        self.badges.setSpacing(6)
        head.addLayout(self.badges)
        lay.addLayout(head)
        lay.addWidget(body, 1)
        foot = QHBoxLayout()
        self.hud = make_label("—", "hud")
        self.hud_right = make_label("", "hudDim")
        foot.addWidget(self.hud)
        foot.addStretch()
        foot.addWidget(self.hud_right)
        lay.addLayout(foot)

    def add_badge(self, text, name="badge"):
        b = make_label(text, name)
        b.hide()
        self.badges.addWidget(b)
        return b

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setPen(QPen(QColor(255, 176, 0, 150), 2))
        w, h, s, m = self.width(), self.height(), 14, 5
        for (x, y, dx, dy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * s, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * s))


class ImageView(QWidget):
    """Zeigt ein QImage seitenrichtig skaliert an und meldet die Pixelposition unter der Maus."""
    hovered = Signal(int, int)
    left = Signal()

    def __init__(self):
        super().__init__()
        self.img = None
        self.setMouseTracking(True)
        self.setMinimumSize(160, 100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, img):
        self.img = img
        self.update()

    def _target(self):
        if self.img is None:
            return None
        iw, ih = self.img.width(), self.img.height()
        s = min(self.width() / iw, self.height() / ih)
        w, h = iw * s, ih * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), s

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#07090b"))
        t = self._target()
        if t:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(t[0], self.img)

    def mouseMoveEvent(self, e):
        t = self._target()
        if not t:
            return
        r, s = t
        u = int((e.position().x() - r.x()) / s)
        v = int((e.position().y() - r.y()) / s)
        if 0 <= u < self.img.width() and 0 <= v < self.img.height():
            self.hovered.emit(u, v)
        else:
            self.left.emit()

    def leaveEvent(self, _):
        self.left.emit()


class ColorBar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedWidth(58)
        self.near, self.far = 0.15, 3.0

    def set_range(self, near, far):
        self.near, self.far = near, far
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        top, bot = 24, h - 24
        bar = QRectF(24, top, 10, max(10, bot - top))
        g = QLinearGradient(0, bar.top(), 0, bar.bottom())
        for i in range(0, 256, 16):
            c = TURBO_U8[255 - i]
            g.setColorAt(i / 255, QColor(int(c[0]), int(c[1]), int(c[2])))
        g.setColorAt(1.0, QColor(*[int(x) for x in TURBO_U8[0]]))
        p.setPen(QPen(QColor(LINE2), 1))
        p.setBrush(g)
        p.drawRoundedRect(bar, 4, 4)
        p.setPen(QColor(TEXT))
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas", "DejaVu Sans Mono"])
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(QRectF(0, 2, self.width(), 18), Qt.AlignCenter, f"{self.near:.2f} m")
        p.drawText(QRectF(0, h - 20, self.width(), 18), Qt.AlignCenter, f"{self.far:.1f} m")


# --------------------------------------------------------------------------
# Hauptfenster
# --------------------------------------------------------------------------
CAM_PRESETS = {
    # center (x, y, z), Abstand, Elevation, Azimut  –  Kamera sitzt im Ursprung und blickt in +Y
    "kamera": ((0, 1.5, 0), 1.52, 0, -90),
    "schraeg": ((0, 1.4, 0), 3.3, 22, -125),
    "oben": ((0, 1.4, 0), 3.6, 89, -90),
    "seite": ((0, 1.4, 0), 3.3, 4, 0),
}


def frustum_segmente(d=0.35):
    """Kamerasymbol mit Sichtkegel (D415 ca. 65° x 40°); Kamera im Ursprung, Blick in +Y, Z oben."""
    hx, hz = np.tan(np.radians(32.5)) * d, np.tan(np.radians(20)) * d
    corners = [(-hx, d, hz), (hx, d, hz), (hx, d, -hz), (-hx, d, -hz)]
    seg = []
    for c in corners:
        seg += [(0, 0, 0), c]
    for i in range(4):
        seg += [corners[i], corners[(i + 1) % 4]]
    bx, by, bz = 0.05, 0.01, 0.0115
    box = [(-bx, -by, -bz), (bx, -by, -bz), (bx, by, -bz), (-bx, by, -bz),
           (-bx, -by, bz), (bx, -by, bz), (bx, by, bz), (-bx, by, bz)]
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
        seg += [box[a], box[b]]
    return np.array(seg, np.float32)


def transformieren(T, p):
    return (p @ T[:3, :3].T.astype(np.float32) + T[:3, 3].astype(np.float32)).astype(np.float32)


class MainWindow(QMainWindow):
    def __init__(self, cam, scan):
        super().__init__()
        self.cam = cam
        self.scan = scan
        # Raumkarte (Viewer-Welt, noch nicht am Boden ausgerichtet)
        self.raum_quelle = "scan"      # scan | datei
        self.raum_pos = None
        self.raum_col = None           # Farben aus Datei (ohne Normalen)
        self.raum_nor = None           # Flächennormalen (Scan oder MK2-Datei)
        self.raum_farbe = cfg_get("raum", "farbe") if cfg_get("raum", "farbe") in ("hoehe", "grau") else "hoehe"
        self.maus = spacemouse.SpaceMouse()
        self._maus_angemeldet = False
        self.raum_bahn = np.zeros((0, 3), np.float32)
        self.raum_pose = np.eye(4)
        self.raum_meta = {}
        self.raum_voxel = 0.02
        self.raum_datei = ""
        self.align = np.eye(4)
        self.align_done = False
        self._raum_id = 0              # wechselt, wenn eine andere Karte geladen/begonnen wird
        self._walk_id = -1
        self._scan_ver = -1
        self._raum_dirty = False
        self.last_seq = -1
        self.last = None
        self.frozen = False
        self.view = "3d"
        self.rays_key = None
        self.rays = None
        self.cloud_pos = np.zeros((0, 3), np.float32)
        self.cloud_col = np.zeros((0, 4), np.float32)
        self.mean_z = 0.0
        self.hover_text = None
        self._opt_sig = None
        self._updating_ui = False

        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        icon_path = ASSET_DIR / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_topbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_stage(), 1)
        v.addLayout(body, 1)
        v.addWidget(self._build_statusbar())

        self._load_settings()
        self._build_shortcuts()

        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=cfg_save)
        self.scan_timer = QTimer(self, interval=200, timeout=self._poll_scan)
        self.scan_timer.start()
        self.maus_timer = QTimer(self, interval=100, timeout=self._poll_maus)
        self.maus_timer.start()
        self.frame_timer = QTimer(self, interval=15, timeout=self._poll_frame)
        self.status_timer = QTimer(self, interval=500, timeout=self._poll_status)
        self.clock_timer = QTimer(self, interval=1000, timeout=self._tick_clock)
        self.frame_timer.start()
        self.status_timer.start()
        self.clock_timer.start()
        self._tick_clock()
        self._poll_status()

    # ------------------------------------------------------------------ Aufbau
    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(60)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 0, 16, 0)
        h.setSpacing(14)

        logo = QLabel()
        pm = QPixmap(str(ASSET_DIR / "icon.png"))
        if not pm.isNull():
            logo.setPixmap(pm.scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        h.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel(f"REALSENSE<span style='color:{MUTED}'>-VIEWER</span>")
        title.setObjectName("brandTitle")
        brand.addStretch()
        brand.addWidget(title)
        brand.addWidget(make_label(f"MK2 · D415 RAUMSCAN · v{APP_VERSION}", "brandSub"))
        brand.addStretch()
        h.addLayout(brand)
        h.addStretch(1)

        self.tabs = Segmented([("3d", "3D"), ("tiefe", "TIEFE"), ("farbe", "FARBE"), ("geteilt", "GETEILT"),
                               ("raum", "RAUM")], "tabs")
        self.tabs.changed.connect(lambda v: self.set_view(v, True))
        self.tabs.setFixedHeight(38)
        h.addWidget(self.tabs, 0, Qt.AlignVCenter)
        h.addStretch(1)

        self.pill_cam = make_label("● SUCHE", "pill")
        self.pill_fps = make_label("— FPS", "pill")
        self.pill_fps.setStyleSheet(f"font-family: {MONO};")
        self.clock = make_label("--:--:--", "clock")
        for pill in (self.pill_cam, self.pill_fps):
            pill.setFixedHeight(28)
            h.addWidget(pill, 0, Qt.AlignVCenter)
        h.addWidget(self.clock)
        return bar

    def _build_sidebar(self):
        scroll = QScrollArea()
        scroll.setObjectName("sidebar")
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(310)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("sidebarInner")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        # Welche Panels/Einstellungen in welcher Ansicht etwas bewirken
        self._sichtbar = []

        def nur(w, *ansichten):
            self._sichtbar.append((w, set(ansichten)))
            return w

        # GERÄT
        p = Panel("GERÄT")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        self.kv = {}
        for i, (key, label, mono) in enumerate([
                ("name", "Modell", False), ("seriennummer", "Seriennr.", True), ("firmware", "Firmware", True),
                ("usb", "USB", True), ("stream", "Stream", True), ("temp", "Temperatur", True), ("sdk", "SDK", True)]):
            grid.addWidget(make_label(label, "kvKey"), i, 0)
            val = make_label("—", "kvValMono" if mono else "kvVal")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(val, i, 1)
            self.kv[key] = val
        grid.setColumnStretch(1, 1)
        p.add(grid)
        lay.addWidget(p)

        # RAUMSCAN
        p = nur(Panel("RAUMSCAN"), "raum")
        row = QHBoxLayout()
        row.addWidget(make_label("Zustand", "kvKey"))
        row.addStretch()
        self.lbl_scan = make_label("BEREIT", "scanState")
        self.lbl_scan.setProperty("state", "idle")
        row.addWidget(self.lbl_scan)
        p.add(row)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        self.kv_scan = {}
        for i, (key, label) in enumerate([("punkte", "Kartenpunkte"), ("bilder", "Bilder"), ("tempo", "Tracking")]):
            grid.addWidget(make_label(label, "kvKey"), i, 0)
            val = make_label("—", "kvValMono")
            val.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(val, i, 1)
            self.kv_scan[key] = val
        grid.setColumnStretch(1, 1)
        p.add(grid)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_scan = make_button("SCAN STARTEN", "btnAccent", checkable=True)
        self.btn_scan_neu = make_button("NEU")
        self.btn_scan_neu.setFixedWidth(64)
        row.addWidget(self.btn_scan, 1)
        row.addWidget(self.btn_scan_neu)
        p.add(row)
        p.add(make_label("Punktabstand der Karte", "fieldLabel"))
        self.seg_voxel = p.add(Segmented([("1", "1 CM"), ("2", "2 CM"), ("3", "3 CM")]))
        p.add(make_label("Kartenfarbe (ohne Farbkamera)", "fieldLabel"))
        self.seg_farbe = p.add(Segmented([("hoehe", "HÖHE"), ("grau", "GRAU")]))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_raum_laden = make_button("LADEN")
        self.btn_raum_speichern = make_button("SPEICHERN")
        row.addWidget(self.btn_raum_laden)
        row.addWidget(self.btn_raum_speichern)
        p.add(row)
        hint = make_label("Nur Tiefenkameras – funktioniert auch im Dunkeln (IR-Projektor wird eingeschaltet). "
                          "Kamera langsam schwenken und gehen (0,5–3 m Abstand). "
                          "Bei VERLOREN zur letzten erfassten Stelle zurückkehren.", "hint")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        p.add(hint)
        lay.addWidget(p)

        # BEGEHEN
        p = nur(Panel("BEGEHEN"), "raum")
        self.btn_walk = p.add(make_button("RAUM BEGEHEN", "btnAccent", checkable=True))
        self.seg_walkmode = p.add(Segmented([("gehen", "GEHEN"), ("fliegen", "FLIEGEN")]))
        self.chk_kollision = p.add(Toggle("Wände und Möbel blockieren"))
        row, self.lbl_auge = field_header("Augenhöhe")
        p.add(row)
        self.rng_auge = p.add(QSlider(Qt.Horizontal))
        self.rng_auge.setRange(100, 200)       # cm
        row, self.lbl_tempo = field_header("Gehtempo")
        p.add(row)
        self.rng_tempo = p.add(QSlider(Qt.Horizontal))
        self.rng_tempo.setRange(3, 30)         # dm/s
        row, self.lbl_fuell = field_header("Punktgröße beim Begehen")
        p.add(row)
        self.rng_fuell = p.add(QSlider(Qt.Horizontal))
        self.rng_fuell.setRange(2, 16)         # halbe Pixel
        self.btn_start = p.add(make_button("ZUM STARTPUNKT"))
        hint = make_label("W A S D / Pfeile: gehen · Maus ziehen: umsehen · Q/E: runter/hoch · "
                          "Umschalt: schnell · R: Start · F: Gehen/Fliegen · Esc: beenden", "hint")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        p.add(hint)
        lay.addWidget(p)

        # 3D-MAUS (nur unter Windows verfügbar)
        p = nur(Panel("3D-MAUS"), *(("raum",) if spacemouse.IST_WINDOWS else ()))
        row = QHBoxLayout()
        row.addWidget(make_label("Gerät", "kvKey"))
        row.addStretch()
        self.lbl_maus = make_label("—", "scanState")
        self.lbl_maus.setProperty("state", "idle")
        row.addWidget(self.lbl_maus)
        p.add(row)
        self.lbl_maus_achsen = p.add(make_label("T  +0.00 +0.00 +0.00\nR  +0.00 +0.00 +0.00", "kvValMono"))
        self.chk_maus = p.add(Toggle("Beim Begehen verwenden"))
        row, self.lbl_maus_empf = field_header("Empfindlichkeit")
        p.add(row)
        self.rng_maus_empf = p.add(QSlider(Qt.Horizontal))
        self.rng_maus_empf.setRange(2, 30)      # × 0,1
        self.chk_maus_vor = p.add(Toggle("Vor/Zurück umkehren"))
        self.chk_maus_neigen = p.add(Toggle("Neigen umkehren"))
        self.chk_maus_drehen = p.add(Toggle("Drehen umkehren"))
        hint = make_label("Kappe schieben: gehen · drehen: umdrehen · neigen: hoch/runter sehen · "
                          "heben/drücken: hoch/runter (Fliegen) · linke Taste: Startpunkt · rechte Taste: Gehen/Fliegen",
                          "hint")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        p.add(hint)
        lay.addWidget(p)

        # KAMERA
        p = Panel("KAMERA")
        p.add(make_label("Auflösung", "fieldLabel"))
        self.sel_res = p.add(QComboBox())
        for r in AUFLOESUNGEN:
            self.sel_res.addItem(r.replace("x", " × "), r)
        p.add(make_label("Bildrate", "fieldLabel"))
        self.sel_fps = p.add(QComboBox())
        for f in FPS_WERTE:
            self.sel_fps.addItem(f"{f} fps", str(f))
        p.add(make_label("Betriebsart", "fieldLabel"))
        self.sel_mode = p.add(QComboBox())
        for val, txt in (("auto", "Automatisch (Demo ohne Kamera)"), ("nein", "Nur Kamera"), ("ja", "Demo-Szene")):
            self.sel_mode.addItem(txt, val)
        self.cam_opts = QWidget()
        co = QVBoxLayout(self.cam_opts)
        co.setContentsMargins(0, 4, 0, 0)
        co.setSpacing(9)
        self.chk_laser = Toggle("IR-Projektor (Laser)")
        co.addWidget(self.chk_laser)
        row, self.lbl_laser = field_header("Laserleistung")
        co.addLayout(row)
        self.rng_laser = QSlider(Qt.Horizontal)
        self.rng_laser.setRange(0, 360)
        self.rng_laser.setSingleStep(30)
        co.addWidget(self.rng_laser)
        co.addWidget(make_label("Preset", "fieldLabel"))
        self.sel_preset = QComboBox()
        co.addWidget(self.sel_preset)
        p.add(self.cam_opts)
        # Projektor und Preset betreffen nur die Tiefe, nicht das Farbbild
        nur(self.cam_opts, "3d", "tiefe", "geteilt", "raum")
        self.cam_hint = p.add(make_label("Optionen nur mit angeschlossener Kamera.", "hint"))
        self.btn_restart = p.add(make_button("KAMERA NEU VERBINDEN"))
        lay.addWidget(p)

        # VERARBEITUNG
        p = Panel("VERARBEITUNG")
        p.add(make_label("Punktabstand (Pixel)", "fieldLabel"))
        self.seg_step = p.add(Segmented([(str(i), str(i)) for i in (1, 2, 3, 4)]))
        # Tiefenfilter: wirken auf 3D, Tiefenbild und Raumscan – nicht auf das Farbbild
        w, g = gruppe(p)
        nur(w, "3d", "tiefe", "geteilt", "raum")
        self.chk_spatial = Toggle("Glättung (räumlich)")
        self.chk_temporal = Toggle("Glättung (zeitlich)")
        self.chk_holes = Toggle("Löcher füllen")
        for t in (self.chk_spatial, self.chk_temporal, self.chk_holes):
            g.addWidget(t)
        # Distanzbereich: nur Anzeige (der Raumscan nutzt fest 0,2–3,5 m)
        w, g = gruppe(p)
        nur(w, "3d", "tiefe", "geteilt")
        row, self.lbl_min = field_header("Min. Distanz")
        g.addLayout(row)
        self.rng_min = QSlider(Qt.Horizontal)
        self.rng_min.setRange(0, 300)          # cm
        g.addWidget(self.rng_min)
        row, self.lbl_max = field_header("Max. Distanz")
        g.addLayout(row)
        self.rng_max = QSlider(Qt.Horizontal)
        self.rng_max.setRange(3, 100)          # dm
        g.addWidget(self.rng_max)
        lay.addWidget(p)

        # DARSTELLUNG
        p = nur(Panel("DARSTELLUNG"), "3d", "geteilt", "raum")
        w, g = gruppe(p)
        nur(w, "3d", "geteilt")
        g.addWidget(make_label("Punktfarbe", "fieldLabel"))
        self.seg_color = Segmented([("rgb", "RGB"), ("tiefe", "TIEFE"), ("amber", "AMBER"), ("teal", "TEAL")])
        g.addWidget(self.seg_color)
        row, self.lbl_size = field_header("Punktgröße")
        p.add(row)
        self.rng_size = p.add(QSlider(Qt.Horizontal))
        self.rng_size.setRange(1, 16)          # halbe Pixel
        self.chk_grid = p.add(Toggle("Bodenraster"))
        w, g = gruppe(p)
        nur(w, "3d", "geteilt")
        self.chk_axes = Toggle("Kamera & Achsen")
        g.addWidget(self.chk_axes)
        g.addWidget(make_label("Blickwinkel", "fieldLabel"))
        self.seg_cam = Segmented([("kamera", "KAMERA"), ("schraeg", "SCHRÄG"), ("oben", "OBEN"), ("seite", "SEITE")])
        g.addWidget(self.seg_cam)
        lay.addWidget(p)

        # AUFNAHME
        p = Panel("AUFNAHME")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_freeze = nur(make_button("EINFRIEREN", checkable=True), "3d", "tiefe", "farbe", "geteilt")
        self.btn_shot = make_button("BILD (PNG)")
        row.addWidget(self.btn_freeze)
        row.addWidget(self.btn_shot)
        p.add(row)
        self.btn_ply = nur(p.add(make_button("PUNKTWOLKE SPEICHERN (PLY)", "btnAccent")), "3d", "tiefe", "geteilt")
        nur(p.add(make_label("Leertaste: Einfrieren · Strg+S: PLY · F11: Vollbild", "hint")),
            "3d", "tiefe", "farbe", "geteilt")
        hint = nur(p.add(make_label("Raum: Strg+O laden · Strg+Umschalt+S speichern · B begehen · F11: Vollbild",
                                    "hint")), "raum")
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(p)

        # SYSTEM
        p = Panel("SYSTEM")
        cfg_lbl = make_label(f"Konfiguration:\n{CONFIG_PATH}", "hint")
        cfg_lbl.setWordWrap(True)
        cfg_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        cfg_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        p.add(cfg_lbl)
        self.btn_folder = p.add(make_button("DATENORDNER ÖFFNEN" if sys.platform == "darwin" else "PROGRAMMORDNER ÖFFNEN"))
        lay.addWidget(p)
        lay.addStretch()

        for combo in (self.sel_res, self.sel_fps, self.sel_mode, self.sel_preset):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        scroll.setWidget(inner)

        # Signale
        self.sel_res.currentIndexChanged.connect(lambda: self._set("kamera", "aufloesung", self.sel_res.currentData(), restart=True))
        self.sel_fps.currentIndexChanged.connect(lambda: self._set("kamera", "fps", self.sel_fps.currentData(), restart=True))
        self.sel_mode.currentIndexChanged.connect(lambda: self._set("kamera", "demo_modus", self.sel_mode.currentData(), restart=True))
        self.chk_laser.toggled.connect(lambda v: self._set("kamera", "laser", v, live=("laser", v)))
        self.rng_laser.valueChanged.connect(lambda v: self.lbl_laser.setText(f"{v} mW"))
        self.rng_laser.sliderReleased.connect(
            lambda: self._set("kamera", "laserleistung", self.rng_laser.value(), live=("laserleistung", self.rng_laser.value())))
        self.sel_preset.activated.connect(
            lambda: self._set("kamera", "preset", self.sel_preset.currentText(), live=("preset", self.sel_preset.currentText())))
        self.btn_restart.clicked.connect(lambda: (self.cam.request_restart(), self.flash("Kamera wird neu verbunden …")))
        self.seg_step.changed.connect(lambda v: self._set("verarbeitung", "punktabstand", v))
        self.chk_spatial.toggled.connect(lambda v: self._set("verarbeitung", "spatial_filter", v))
        self.chk_temporal.toggled.connect(lambda v: self._set("verarbeitung", "temporal_filter", v))
        self.chk_holes.toggled.connect(lambda v: self._set("verarbeitung", "loecher_fuellen", v))
        self.rng_min.valueChanged.connect(self._on_range)
        self.rng_max.valueChanged.connect(self._on_range)
        self.seg_color.changed.connect(self._on_color)
        self.rng_size.valueChanged.connect(self._on_size)
        self.chk_grid.toggled.connect(self._on_helpers)
        self.chk_axes.toggled.connect(self._on_helpers)
        self.seg_cam.changed.connect(self._on_cam_preset)
        self.btn_freeze.toggled.connect(self._on_freeze)
        self.btn_shot.clicked.connect(self.save_png)
        self.btn_ply.clicked.connect(self.save_ply)
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_scan.toggled.connect(self._on_scan)
        self.btn_scan_neu.clicked.connect(self._scan_neu)
        self.seg_voxel.changed.connect(self._on_voxel)
        self.seg_farbe.changed.connect(self._on_farbe)
        for w in (self.chk_maus, self.chk_maus_vor, self.chk_maus_neigen, self.chk_maus_drehen):
            w.toggled.connect(self._on_maus_opts)
        self.rng_maus_empf.valueChanged.connect(self._on_maus_opts)
        self.btn_raum_laden.clicked.connect(self.raum_laden)
        self.btn_raum_speichern.clicked.connect(self.raum_speichern)
        self.btn_walk.toggled.connect(self.begehen)
        self.seg_walkmode.changed.connect(self._on_walkmode)
        self.chk_kollision.toggled.connect(self._on_walk_opts)
        self.rng_auge.valueChanged.connect(self._on_walk_opts)
        self.rng_tempo.valueChanged.connect(self._on_walk_opts)
        self.rng_fuell.valueChanged.connect(self._on_walk_opts)
        self.btn_start.clicked.connect(lambda: (self.raum.zum_start(), self.raum.setFocus()))
        return scroll

    def _build_stage(self):
        stage = QWidget()
        stage.setObjectName("stage")
        self.stage_grid = QGridLayout(stage)
        self.stage_grid.setContentsMargins(10, 10, 10, 10)
        self.stage_grid.setSpacing(10)

        # 3D-Ansicht
        self.gl = gl.GLViewWidget()
        self.gl.setBackgroundColor(QColor("#07090b"))
        self.gl.opts["fov"] = 55
        self.scatter = gl.GLScatterPlotItem(pos=np.zeros((1, 3), np.float32), size=2.0,
                                            color=np.zeros((1, 4), np.float32), pxMode=True)
        self.scatter.setGLOptions("opaque")
        self.gl.addItem(self.scatter)

        self.grid_item = gl.GLGridItem()
        self.grid_item.setSize(8, 8)
        self.grid_item.setSpacing(0.25, 0.25)
        self.grid_item.setColor((25, 211, 197, 70))
        self.grid_item.translate(0, 4, -0.6)
        self.gl.addItem(self.grid_item)

        # Kamerasymbol mit Sichtkegel
        self.frustum = gl.GLLinePlotItem(pos=frustum_segmente(0.35), mode="lines",
                                         color=(1.0, 0.69, 0.0, 0.75), width=1.5, antialias=True)
        self.axes = gl.GLAxisItem(size=pg.Vector(0.12, 0.12, 0.12))
        self.gl.addItem(self.frustum)
        self.gl.addItem(self.axes)
        self.gl.installEventFilter(self)
        self.gl.setCameraPosition(pos=pg.Vector(0, 1.4, 0), distance=3.3, elevation=22, azimuth=-125)

        self.colorbar = ColorBar()
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        hl.addWidget(self.gl, 1)
        hl.addWidget(self.colorbar)

        self.nosignal = QWidget()
        nl = QVBoxLayout(self.nosignal)
        nl.addStretch()
        t = make_label("KEIN SIGNAL", "noSignalTitle")
        t.setAlignment(Qt.AlignCenter)
        self.nosignal_text = make_label("Warte auf Kamera …", "noSignalText")
        self.nosignal_text.setAlignment(Qt.AlignCenter)
        self.nosignal_text.setWordWrap(True)
        nl.addWidget(t)
        nl.addWidget(self.nosignal_text)
        nl.addStretch()

        self.stack3d = QStackedWidget()
        self.stack3d.addWidget(holder)
        self.stack3d.addWidget(self.nosignal)

        self.pane3d = Pane("PUNKTWOLKE · 3D", self.stack3d)
        self.pane3d.hud_right.setText("LINKS DREHEN · MITTE VERSCHIEBEN · RAD ZOOM")
        self.badge_demo = self.pane3d.add_badge("DEMO-SZENE")
        self.badge_frozen = self.pane3d.add_badge("EINGEFROREN", "badgeTeal")

        self.view_depth = ImageView()
        self.view_depth.hovered.connect(self._on_hover)
        self.view_depth.left.connect(lambda: self._on_hover(-1, -1))
        self.pane_depth = Pane("TIEFENBILD", self.view_depth)
        self.pane_depth.hud_right.setText("MAUS: ENTFERNUNG")
        self.view_color = ImageView()
        self.pane_color = Pane("FARBBILD · AUSGERICHTET", self.view_color)

        # Raum (Scan-Karte, begehbar)
        self.raum = RaumView()
        self.raum.setBackgroundColor(QColor("#07090b"))
        self.raum.opts["fov"] = 70
        self.raum_cloud = gl.GLScatterPlotItem(pos=np.zeros((1, 3), np.float32), size=2.0,
                                               color=np.zeros((1, 4), np.float32), pxMode=True)
        self.raum_cloud.setGLOptions("opaque")
        self.raum.addItem(self.raum_cloud)
        self.raum_grid = gl.GLGridItem()
        self.raum_grid.setSize(12, 12)
        self.raum_grid.setSpacing(0.5, 0.5)
        self.raum_grid.setColor((25, 211, 197, 60))
        self.raum_grid.hide()
        self.raum.addItem(self.raum_grid)
        self.raum_bahn_item = gl.GLLinePlotItem(pos=np.zeros((2, 3), np.float32), mode="line_strip",
                                                color=(0.1, 0.83, 0.77, 0.9), width=2, antialias=True)
        self.raum.addItem(self.raum_bahn_item)
        self._frustum_seg = frustum_segmente(0.3)
        self.raum_cam = gl.GLLinePlotItem(pos=self._frustum_seg, mode="lines",
                                          color=(1.0, 0.69, 0.0, 0.9), width=2, antialias=True)
        self.raum.addItem(self.raum_cam)
        self.raum_cam.hide()
        self.raum_bahn_item.hide()
        self.raum.setCameraPosition(pos=pg.Vector(0, 1.5, 0), distance=6.0, elevation=35, azimuth=-110)
        self.raum.walkChanged.connect(self._on_walk_changed)
        self.raum.modusChanged.connect(self._on_walkmode_key)
        self.raum.moved.connect(self._update_raum_hud)
        self.pane_raum = Pane("RAUM · BEGEHBARE 3D-KARTE", self.raum)
        self.pane_raum.hud_right.setText("LINKS DREHEN · MITTE VERSCHIEBEN · RAD ZOOM · B: BEGEHEN")
        self.badge_scan = self.pane_raum.add_badge("SCAN LÄUFT", "badgeTeal")
        self.badge_lost = self.pane_raum.add_badge("TRACKING VERLOREN", "badgeRed")
        self.badge_walk = self.pane_raum.add_badge("BEGEHEN")
        self.badge_file = self.pane_raum.add_badge("DATEI")
        return stage

    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusbar")
        bar.setFixedHeight(28)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(18)
        self.status_msg = make_label("Starte …", "statusMsg")
        h.addWidget(self.status_msg, 1)
        self.status_pts = make_label("", "statusDim")
        h.addWidget(self.status_pts)
        h.addWidget(make_label(f"{APP_NAME} v{APP_VERSION}", "statusDim"))
        self._flash_until = 0
        return bar

    def _build_shortcuts(self):
        def act(key, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.setShortcutContext(Qt.ApplicationShortcut)
            a.triggered.connect(fn)
            self.addAction(a)
        act("Space", lambda: self.btn_freeze.toggle())
        act("Ctrl+S", self.save_ply)
        act("Ctrl+P", self.save_png)
        act("F11", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        act("1", lambda: self.set_view("3d", True))
        act("2", lambda: self.set_view("tiefe", True))
        act("3", lambda: self.set_view("farbe", True))
        act("4", lambda: self.set_view("geteilt", True))
        act("5", lambda: self.set_view("raum", True))
        act("B", lambda: self.btn_walk.toggle())
        act("Ctrl+O", self.raum_laden)
        act("Ctrl+Shift+S", self.raum_speichern)

    # ------------------------------------------------------------ Einstellungen
    def _load_settings(self):
        self._updating_ui = True

        def sel(combo, value):
            i = combo.findData(value)
            combo.setCurrentIndex(i if i >= 0 else 0)
        sel(self.sel_res, cfg_get("kamera", "aufloesung"))
        sel(self.sel_fps, cfg_get("kamera", "fps"))
        sel(self.sel_mode, cfg_get("kamera", "demo_modus"))
        self.chk_laser.setChecked(truthy(cfg_get("kamera", "laser")))
        self.rng_laser.setValue(int(float(cfg_get("kamera", "laserleistung") or 150)))
        self.lbl_laser.setText(f"{self.rng_laser.value()} mW")
        self.seg_step.set_value(cfg_get("verarbeitung", "punktabstand"))
        self.chk_spatial.setChecked(truthy(cfg_get("verarbeitung", "spatial_filter")))
        self.chk_temporal.setChecked(truthy(cfg_get("verarbeitung", "temporal_filter")))
        self.chk_holes.setChecked(truthy(cfg_get("verarbeitung", "loecher_fuellen")))
        self.min_d = float(cfg_get("verarbeitung", "min_distanz_m") or 0.15)
        self.max_d = float(cfg_get("verarbeitung", "max_distanz_m") or 3.0)
        self.rng_min.setValue(int(round(self.min_d * 100)))
        self.rng_max.setValue(int(round(self.max_d * 10)))
        self.color_mode = cfg_get("ansicht", "farbmodus")
        self.seg_color.set_value(self.color_mode)
        self.point_size = float(cfg_get("ansicht", "punktgroesse") or 2)
        self.rng_size.setValue(int(round(self.point_size * 2)))
        self.chk_grid.setChecked(truthy(cfg_get("ansicht", "gitter")))
        self.chk_axes.setChecked(truthy(cfg_get("ansicht", "achsen")))
        vox = cfg_get("raum", "voxel_cm")
        self.seg_voxel.set_value(vox if vox in ("1", "2", "3") else "2")
        self.raum_voxel = int(vox if vox in ("1", "2", "3") else "2") / 100
        self.scan.zuruecksetzen(self.raum_voxel)
        modus = cfg_get("raum", "modus")
        self.seg_walkmode.set_value(modus if modus in ("gehen", "fliegen") else "gehen")
        self.raum.set_modus(modus)
        self.chk_kollision.setChecked(truthy(cfg_get("raum", "kollision")))

        def num(key, default):
            try:
                return float(cfg_get("raum", key))
            except ValueError:
                return default
        self.rng_auge.setValue(int(round(num("augenhoehe_m", 1.6) * 100)))
        self.rng_tempo.setValue(int(round(num("tempo_m_s", 1.4) * 10)))
        self.rng_fuell.setValue(int(round(num("punkt_px", 2.0) * 2)))
        self._on_walk_opts()
        self.seg_farbe.set_value(self.raum_farbe)
        self.chk_maus.setChecked(truthy(cfg_get("3dmaus", "aktiv")))
        try:
            self.rng_maus_empf.setValue(int(round(float(cfg_get("3dmaus", "empfindlichkeit")) * 10)))
        except ValueError:
            self.rng_maus_empf.setValue(10)
        self.chk_maus_vor.setChecked(truthy(cfg_get("3dmaus", "umkehr_vor")))
        self.chk_maus_neigen.setChecked(truthy(cfg_get("3dmaus", "umkehr_neigen")))
        self.chk_maus_drehen.setChecked(truthy(cfg_get("3dmaus", "umkehr_drehen")))
        self.raum.maus = self.maus
        self._on_maus_opts()
        self._updating_ui = False
        self._apply_local()
        self.set_view(cfg_get("ansicht", "ansicht") or "3d", False)

    def _set(self, section, key, value, restart=False, live=None):
        if self._updating_ui:
            return
        old = cfg_get(section, key)
        cfg_set(section, key, value)
        self.save_timer.start()
        if restart and old != cfg_get(section, key):
            self.cam.request_restart()
            self.flash("Kamera wird mit neuen Einstellungen neu gestartet …")
        if live:
            self.cam.queue_option(*live)

    def _on_range(self):
        if self._updating_ui:
            return
        self.min_d = self.rng_min.value() / 100
        self.max_d = self.rng_max.value() / 10
        if self.max_d <= self.min_d + 0.1:
            self.max_d = self.min_d + 0.1
        cfg_set("verarbeitung", "min_distanz_m", f"{self.min_d:.2f}")
        cfg_set("verarbeitung", "max_distanz_m", f"{self.max_d:.1f}")
        self.save_timer.start()
        self._apply_local()
        self._rerender()

    def _on_color(self, v):
        self.color_mode = v
        self._set("ansicht", "farbmodus", v)
        self._apply_local()
        self._rerender()

    def _on_size(self, v):
        self.point_size = v / 2
        self._set("ansicht", "punktgroesse", self.point_size)
        self._apply_local()
        self._rerender()

    def _on_helpers(self):
        self._set("ansicht", "gitter", self.chk_grid.isChecked())
        self._set("ansicht", "achsen", self.chk_axes.isChecked())
        self._apply_local()

    def _on_cam_preset(self, v):
        self.set_cam_preset(v)
        QTimer.singleShot(600, lambda: self.seg_cam.set_value(""))

    def _on_freeze(self, on):
        self.frozen = on
        self.btn_freeze.setText("FORTSETZEN" if on else "EINFRIEREN")
        self.badge_frozen.setVisible(on)

    def _apply_local(self):
        self.lbl_min.setText(f"{self.min_d:.2f} m")
        self.lbl_max.setText(f"{self.max_d:.1f} m")
        self.lbl_size.setText(f"{self.point_size:.1f} px")
        self.grid_item.setVisible(self.chk_grid.isChecked())
        helpers = self.chk_axes.isChecked() and not getattr(self, "in_cam_view", False)
        self.frustum.setVisible(helpers)
        self.axes.setVisible(helpers)
        self.colorbar.setVisible(self.color_mode == "tiefe")
        self.colorbar.set_range(self.min_d, self.max_d)

    def _open_folder(self):
        path = str(BASE_DIR)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
        except Exception as exc:
            self.flash(f"Ordner konnte nicht geöffnet werden: {exc}", warn=True)

    # ---------------------------------------------------------------- Ansicht
    def set_view(self, v, persist):
        if v not in ("3d", "tiefe", "farbe", "geteilt", "raum"):
            v = "3d"
        if v != "raum" and self.raum.walk:
            self.btn_walk.setChecked(False)
        self.view = v
        self.tabs.set_value(v)
        # Seitenleiste: nur Einstellungen zeigen, die in dieser Ansicht etwas bewirken
        for w, ansichten in self._sichtbar:
            w.setVisible(v in ansichten)
        g = self.stage_grid
        for w in (self.pane3d, self.pane_depth, self.pane_color, self.pane_raum):
            g.removeWidget(w)
            w.hide()
        for i in range(3):
            g.setRowStretch(i, 0)
            g.setColumnStretch(i, 0)
        if v == "3d":
            g.addWidget(self.pane3d, 0, 0)
        elif v == "tiefe":
            g.addWidget(self.pane_depth, 0, 0)
        elif v == "farbe":
            g.addWidget(self.pane_color, 0, 0)
        elif v == "raum":
            g.addWidget(self.pane_raum, 0, 0)
        else:
            g.addWidget(self.pane3d, 0, 0, 2, 1)
            g.addWidget(self.pane_depth, 0, 1)
            g.addWidget(self.pane_color, 1, 1)
            g.setColumnStretch(0, 2)
            g.setColumnStretch(1, 1)
            g.setRowStretch(0, 1)
            g.setRowStretch(1, 1)
        for w in (self.pane3d, self.pane_depth, self.pane_color, self.pane_raum):
            if g.indexOf(w) >= 0:
                w.show()
        if persist:
            self._set("ansicht", "ansicht", v)
        if v == "raum":
            self._raum_dirty = True
            self._raum_anzeigen()
        else:
            self.raum.thumb = None
        self._rerender()

    def set_cam_preset(self, name):
        c, dist, elev, azim = CAM_PRESETS.get(name, CAM_PRESETS["schraeg"])
        self.gl.setCameraPosition(pos=pg.Vector(*c), distance=dist, elevation=elev, azimuth=azim)
        # Aus Sicht der Kamera selbst das Kamerasymbol ausblenden, bis wieder gedreht wird
        self.in_cam_view = (name == "kamera")
        self._apply_local()

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.gl and ev.type() == QEvent.MouseButtonPress and getattr(self, "in_cam_view", False):
            self.in_cam_view = False
            self._apply_local()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------- Rendering
    def _poll_frame(self):
        if self.frozen:
            return
        f = self.cam.latest()
        if f is None or f.seq == self.last_seq:
            return
        self.last_seq = f.seq
        self.render(f)

    def _rerender(self):
        if self.last is not None:
            self.render(self.last)

    def _get_rays(self, f):
        h, w = f.depth.shape
        key = (w, h, f.fx, f.fy, f.ppx, f.ppy)
        if key != self.rays_key:
            u, v = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            self.rays = (((u - f.ppx) / f.fx).ravel(), ((v - f.ppy) / f.fy).ravel())
            self.rays_key = key
        return self.rays

    def build_cloud(self, f):
        rx, ry = self._get_rays(f)
        raw = f.depth.ravel()
        z = raw.astype(np.float32) * f.units
        m = (raw > 0) & (z >= self.min_d) & (z <= self.max_d)
        zz = z[m]
        n = zz.size
        pos = np.empty((n, 3), np.float32)
        pos[:, 0] = rx[m] * zz
        pos[:, 1] = zz
        pos[:, 2] = -ry[m] * zz
        col = np.ones((n, 4), np.float32)
        mode = self.color_mode
        if mode == "rgb" and f.rgb is None:
            mode = "tiefe"
        span = max(1e-6, self.max_d - self.min_d)
        if mode == "rgb":
            col[:, :3] = f.rgb.reshape(-1, 3)[m].astype(np.float32) * (1 / 255)
        elif mode == "tiefe":
            idx = ((1 - (zz - self.min_d) / span) * 255).astype(np.int32)
            col[:, :3] = TURBO_F[np.clip(idx, 0, 255)]
        else:
            base = TEAL_F if mode == "teal" else AMBER_F
            b = np.clip(1.05 - 0.75 * (zz - self.min_d) / span, 0, 1)[:, None]
            col[:, :3] = base[None, :] * b
        self.cloud_pos, self.cloud_col = pos, col
        self.mean_z = float(zz.mean()) if n else 0.0
        return n

    def depth_image(self, f):
        h, w = f.depth.shape
        z = f.depth.astype(np.float32) * f.units
        t = (z - self.min_d) / max(1e-6, self.max_d - self.min_d)
        idx = np.clip(((1 - t) * 255).astype(np.int32), 0, 255)
        rgb = TURBO_U8[idx]
        rgb[(t < 0) | (t > 1)] = (12, 14, 17)
        rgb[f.depth == 0] = (0, 0, 0)
        rgb = np.ascontiguousarray(rgb)
        img = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
        return img

    def render(self, f):
        self.last = f
        show3d = self.view in ("3d", "geteilt")
        if show3d:
            n = self.build_cloud(f)
            if n:
                self.scatter.setData(pos=self.cloud_pos, color=self.cloud_col, size=self.point_size)
            else:
                self.scatter.setData(pos=np.zeros((1, 3), np.float32), color=np.zeros((1, 4), np.float32))
            self.pane3d.hud.setText(f"PUNKTE {n:,} · Ø {self.mean_z:.2f} m · BEREICH {self.min_d:.2f}–{self.max_d:.2f} m"
                                    .replace(",", "."))
            self.status_pts.setText(f"{n:,} Punkte".replace(",", "."))
        h, w = f.depth.shape
        if self.view in ("tiefe", "geteilt"):
            self.view_depth.set_image(self.depth_image(f))
            if not self.hover_text:
                self.pane_depth.hud.setText(f"{w}×{h} · {self.min_d:.2f}–{self.max_d:.2f} m")
        if self.view in ("farbe", "geteilt"):
            if f.rgb is not None:
                img = QImage(f.rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
                self.view_color.set_image(img)
                self.pane_color.hud.setText(f"{w}×{h} · AUF TIEFE AUSGERICHTET")
            else:
                self.view_color.set_image(None)
                self.pane_color.hud.setText("KEIN FARBSTROM")
        if self.view == "raum" and self.scan.aktiv and not self.raum.walk:
            # Live-Vorschau aus der Tiefe (der Raumscan nutzt kein Farbbild)
            self.raum.thumb = self.depth_image(f)
            self.raum.thumb_text = "TIEFE LIVE · DEMO" if f.demo else "TIEFE LIVE"
            self.raum.update()
        self.badge_demo.setVisible(bool(f.demo))
        self.stack3d.setCurrentIndex(0)
        self.pill_fps.setText(f"{f.fps:.1f} FPS")

    def _on_hover(self, u, v):
        f = self.last
        if f is None or u < 0:
            self.hover_text = None
            if f is not None:
                h, w = f.depth.shape
                self.pane_depth.hud.setText(f"{w}×{h} · {self.min_d:.2f}–{self.max_d:.2f} m")
            return
        raw = int(f.depth[v, u])
        self.hover_text = (f"PIXEL {u},{v} · {raw * f.units:.3f} m" if raw else f"PIXEL {u},{v} · KEIN WERT")
        self.pane_depth.hud.setText(self.hover_text)

    # --------------------------------------------------------------- Status
    def _poll_status(self):
        st = self.cam.get_status()
        if st["verbunden"]:
            state, text = "ok", "● VERBUNDEN"
        elif st["demo"]:
            state, text = "demo", "● DEMO"
        else:
            state, text = "err", "● GETRENNT"
        if self.pill_cam.property("state") != state:
            self.pill_cam.setProperty("state", state)
            self.pill_cam.style().unpolish(self.pill_cam)
            self.pill_cam.style().polish(self.pill_cam)
        self.pill_cam.setText(text)

        g = st.get("geraet") or {}
        usb = g.get("usb") or ""
        self.kv["name"].setText(g.get("name") or "—")
        self.kv["seriennummer"].setText(g.get("seriennummer") or "—")
        self.kv["firmware"].setText(g.get("firmware") or "—")
        self.kv["usb"].setText((usb + ("  ⚠ USB 2" if usb.startswith("2") else "")) or "—")
        self.kv["stream"].setText(st.get("aufloesung") or "—")
        self.kv["temp"].setText(f"{st['temperatur']} °C" if st.get("temperatur") is not None else "—")
        self.kv["sdk"].setText(st.get("sdk") or ("FEHLT" if st.get("sdk_fehler") else "—"))
        if time.time() > self._flash_until:
            self.status_msg.setStyleSheet("")
            self.status_msg.setText(st.get("meldung") or "")

        o = st.get("optionen") or {}
        has = bool(st["verbunden"] and o)
        self.cam_opts.setEnabled(has)
        self.cam_hint.setVisible(not has and self.view != "farbe")
        sig = repr(sorted(o.items())) if has else None
        if has and sig != self._opt_sig and not self.rng_laser.isSliderDown():
            self._opt_sig = sig
            self._updating_ui = True
            if "laser" in o:
                self.chk_laser.setChecked(bool(o["laser"]))
            if "laserleistung" in o:
                self.rng_laser.setRange(int(o["laserleistung_min"]), int(o["laserleistung_max"]))
                self.rng_laser.setSingleStep(int(o.get("laserleistung_step") or 30))
                self.rng_laser.setValue(int(o["laserleistung"]))
                self.lbl_laser.setText(f"{int(o['laserleistung'])} mW")
            if o.get("presets"):
                self.sel_preset.clear()
                self.sel_preset.addItems(o["presets"])
                if o.get("preset"):
                    self.sel_preset.setCurrentText(o["preset"])
            self._updating_ui = False

        if not st["verbunden"] and not st["demo"] and not self.frozen:
            self.nosignal_text.setText(st.get("meldung") or "Warte auf Kamera …")
            self.stack3d.setCurrentIndex(1)
            self.pill_fps.setText("— FPS")
            self.badge_demo.hide()
            self.pane3d.hud.setText("—")
            self.status_pts.setText("")
            if self.last is not None and self.cam.latest() is None:
                self.last = None
                self.view_depth.set_image(None)
                self.view_color.set_image(None)
                self.pane_depth.hud.setText("KEIN SIGNAL")
                self.pane_color.hud.setText("KEIN SIGNAL")

    def _tick_clock(self):
        self.clock.setText(time.strftime("%H:%M:%S"))

    def flash(self, text, warn=False):
        self._flash_until = time.time() + 3
        self.status_msg.setStyleSheet(f"color: {AMBER if warn else TEAL};")
        self.status_msg.setText(text)

    # ------------------------------------------------------------- Raumscan
    def _set_scan_state(self, state, text):
        if self.lbl_scan.property("state") != state:
            self.lbl_scan.setProperty("state", state)
            self.lbl_scan.style().unpolish(self.lbl_scan)
            self.lbl_scan.style().polish(self.lbl_scan)
        self.lbl_scan.setText(text)

    def _poll_scan(self):
        st = self.scan.get_status()
        z = st["zustand"]
        aktiv = self.scan.aktiv
        if self.raum_quelle == "datei":
            self._set_scan_state("file", "DATEI GELADEN")
        elif aktiv and z == "verloren":
            self._set_scan_state("lost", "VERLOREN")
        elif aktiv:
            self._set_scan_state("ok", "SCANNT")
        elif st["bilder"]:
            self._set_scan_state("idle", "PAUSE")
        else:
            self._set_scan_state("idle", "BEREIT")
        if self.raum_quelle == "datei":
            n = 0 if self.raum_pos is None else len(self.raum_pos)
            self.kv_scan["punkte"].setText(f"{n:,}".replace(",", "."))
            self.kv_scan["bilder"].setText("—")
            self.kv_scan["tempo"].setText(f"{self.raum_voxel * 100:.0f} cm Raster")
        else:
            self.kv_scan["punkte"].setText(f"{st['voxel']:,}".replace(",", "."))
            self.kv_scan["bilder"].setText(str(st["bilder"]))
            self.kv_scan["tempo"].setText(
                f"{st['fps']:.1f} B/s · {st['guete'] * 100:.0f} %" if aktiv else "—")
        self.badge_scan.setVisible(aktiv and z != "verloren" and self.raum_quelle == "scan")
        self.badge_lost.setVisible(aktiv and z == "verloren" and self.raum_quelle == "scan")
        self.badge_file.setVisible(self.raum_quelle == "datei")
        if not aktiv and self.raum.thumb is not None:
            self.raum.thumb = None
            self.raum.update()

        if self.raum_quelle == "scan":
            snap = self.scan.karte()
            if snap is None:
                if self.raum_pos is not None:
                    self.raum_pos = None
                    self._raum_dirty = True
            elif snap[0] != self._scan_ver:
                self._scan_ver, self.raum_pos, self.raum_nor, self.raum_bahn, _ = snap
                self.raum_col = None
                self.raum_voxel = self.scan.tracker.voxel
                self._raum_dirty = True
            self.raum_pose = st["pose"]
            self._update_cam_marker()
        if self._raum_dirty and self.view == "raum":
            self._raum_anzeigen()
        if self.view == "raum":
            self._update_raum_hud()

    def _update_cam_marker(self):
        show = self.raum_quelle == "scan" and not self.raum.walk and self.scan.get_status()["bilder"] > 0
        self.raum_cam.setVisible(show)
        if show:
            T = self.align @ self.raum_pose
            self.raum_cam.setData(pos=transformieren(T, self._frustum_seg))

    def _raum_anzeigen(self):
        self._raum_dirty = False
        walk = self.raum.walk
        if self.raum_pos is None or len(self.raum_pos) == 0:
            self.raum_cloud.setData(pos=np.zeros((1, 3), np.float32), color=np.zeros((1, 4), np.float32))
            self.raum_bahn_item.setVisible(False)
            self.raum_grid.setVisible(False)
            self.pane_raum.hud.setText("NOCH KEIN RAUM · SCAN STARTEN ODER DATEI LADEN")
            return
        pos = transformieren(self.align, self.raum_pos) if self.align_done else self.raum_pos
        col = np.ones((len(pos), 4), np.float32)
        col[:, :3] = self._raum_farben(pos)
        if walk:
            # feste Bildschirmgröße wie beim Scan – nahe Punkte werden nicht aufgebläht
            self.raum_cloud.setData(pos=pos, color=col, size=self.rng_fuell.value() / 2, pxMode=True)
        else:
            self.raum_cloud.setData(pos=pos, color=col, size=max(1.5, self.point_size), pxMode=True)
        bahn = self.raum_bahn if self.raum_quelle == "scan" else np.zeros((0, 3), np.float32)
        if len(bahn) >= 2 and not walk:
            self.raum_bahn_item.setData(pos=transformieren(self.align, bahn) if self.align_done else bahn)
            self.raum_bahn_item.setVisible(True)
        else:
            self.raum_bahn_item.setVisible(False)
        self.raum_grid.setVisible(self.align_done and self.chk_grid.isChecked() and not walk)

    def _raum_farben(self, pos):
        """Farben der Karte: aus den Normalen schattiert (Höhe/Grau) oder – bei fremden Dateien – aus der Datei."""
        if self.raum_nor is None:
            if self.raum_col is not None:
                return self.raum_col
            return np.full((len(pos), 3), 0.75, np.float32)
        nor = self.raum_nor @ self.align[:3, :3].T.astype(np.float32) if self.align_done else self.raum_nor
        if not self.align_done and len(pos):
            # Boden noch nicht bestimmt: Höhe vorläufig ab dem tiefsten Bereich messen
            pos = pos.copy()
            pos[:, 2] -= np.percentile(pos[:, 2], 2)
        return raumscan.einfaerben(pos, nor, self.raum_farbe, TURBO_F)

    def _on_farbe(self, v):
        self.raum_farbe = v
        self._set("raum", "farbe", v)
        if self.raum_nor is None and self.raum_pos is not None:
            self.flash("Diese Datei enthält keine Normalen – es werden ihre eigenen Farben gezeigt", warn=True)
        self._raum_anzeigen()

    def _update_raum_hud(self):
        n = 0 if self.raum_pos is None else len(self.raum_pos)
        if self.raum.walk:
            e = self.raum.eye
            richtung = (90 - self.raum.yaw) % 360          # 0° = Startblickrichtung (+Y)
            self.pane_raum.hud.setText(f"POS {e[0]:+.2f} / {e[1]:+.2f} m · HÖHE {e[2]:.2f} m · KURS {richtung:03.0f}° · "
                                       f"{'FLIEGEN' if self.raum.modus == 'fliegen' else 'GEHEN'}")
        elif n:
            name = f" · {Path(self.raum_datei).name}" if self.raum_quelle == "datei" and self.raum_datei else ""
            self.pane_raum.hud.setText(f"PUNKTE {n:,} · RASTER {self.raum_voxel * 100:.0f} cm{name}".replace(",", "."))
        else:
            self.pane_raum.hud.setText("NOCH KEIN RAUM · SCAN STARTEN ODER DATEI LADEN")

    def _on_scan(self, on):
        self.btn_scan.setText("SCAN PAUSIEREN" if on else ("SCAN FORTSETZEN" if self.scan.get_status()["bilder"]
                                                           else "SCAN STARTEN"))
        if on:
            if self.raum_quelle == "datei":
                # zurück zur eigenen Scan-Karte
                self.raum_quelle = "scan"
                self._scan_ver = -1
                self.raum_pos = None
                self._neue_karte()
            self.cam.set_nur_tiefe(True)       # Farbsensor aus: Scan nur aus Tiefe, auch im Dunkeln
            self.scan.starten()
            if self.view != "raum":
                self.set_view("raum", True)
            self.flash("Raumscan läuft (nur Tiefe, Farbkamera aus) – Kamera langsam bewegen")
        else:
            self.scan.pausieren()
            self.cam.set_nur_tiefe(False)

    def _neue_karte(self):
        self.align = np.eye(4)
        self.align_done = False
        self._raum_id += 1
        self._raum_dirty = True

    def _scan_neu(self):
        st = self.scan.get_status()
        if st["voxel"] > 0 and self.raum_quelle == "scan":
            r = QMessageBox.question(self, APP_NAME, "Aktuellen Raumscan verwerfen und neu beginnen?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        if self.raum.walk:
            self.btn_walk.setChecked(False)
        vox = cfg_get("raum", "voxel_cm")
        self.raum_voxel = int(vox if vox in ("1", "2", "3") else "2") / 100
        self.scan.zuruecksetzen(self.raum_voxel)
        self.raum_quelle = "scan"
        self.raum_datei = ""
        self.raum_pos = None
        self.raum_bahn = np.zeros((0, 3), np.float32)
        self._scan_ver = -1
        self._neue_karte()
        self._raum_anzeigen()
        self.flash("Neuer Raumscan – SCAN STARTEN drücken")

    def _on_voxel(self, v):
        self._set("raum", "voxel_cm", v)
        if self.scan.get_status()["bilder"] == 0 and self.raum_quelle == "scan":
            self.raum_voxel = int(v) / 100
            self.scan.zuruecksetzen(self.raum_voxel)
        else:
            self.flash("Neuer Punktabstand gilt ab dem nächsten Scan (NEU)")

    # ------------------------------------------------------------- Begehen
    def _ausrichten(self):
        """Boden suchen und Karte so drehen, dass er waagerecht bei Z=0 liegt."""
        if self.align_done:
            return
        if self.raum_meta.get("boden") == "ja" and self.raum_quelle == "datei":
            self.align, self.align_done = np.eye(4), True
            return
        start = self.raum_bahn[0] if len(self.raum_bahn) and self.raum_quelle == "scan" else None
        ref = np.zeros(3) if start is None else start.astype(np.float64)
        if start is None:
            ref = np.array([*np.median(self.raum_pos[:, :2], 0), np.percentile(self.raum_pos[:, 2], 60)])
        T, ok = raumscan.boden_ausrichten(self.raum_pos, ref)
        if not ok:
            T = np.eye(4)
            if start is not None:
                T[2, 3] = 1.45 - start[2]               # Kamera wurde vermutlich in Brusthöhe gehalten
            else:
                T[2, 3] = -float(np.percentile(self.raum_pos[:, 2], 1))
            self.flash("Kein Boden erkannt – Höhe geschätzt", warn=True)
        else:
            self.flash("Boden erkannt – Raum ausgerichtet")
        self.align, self.align_done = T, True

    def _startpunkt(self):
        if self.raum_quelle == "scan" and len(self.raum_bahn):
            p0 = transformieren(self.align, self.raum_bahn[:1])[0]
            d = self.align[:3, :3] @ np.array([0.0, 1.0, 0.0])
            return float(p0[0]), float(p0[1]), math.degrees(math.atan2(d[1], d[0]))
        try:
            x, y, yaw = [float(v) for v in self.raum_meta.get("start", "").split()[:3]]
            return x, y, yaw
        except ValueError:
            pos = transformieren(self.align, self.raum_pos)
            c = np.median(pos[:, :2], 0)
            return float(c[0]), float(c[1]), 90.0

    def begehen(self, on):
        if on:
            if self.raum_pos is None or len(self.raum_pos) < 200:
                self.flash("Noch kein Raum – zuerst scannen oder eine Datei laden", warn=True)
                self.btn_walk.blockSignals(True)
                self.btn_walk.setChecked(False)
                self.btn_walk.blockSignals(False)
                return
            if self.view != "raum":
                self.set_view("raum", True)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._ausrichten()
                pos = transformieren(self.align, self.raum_pos)
                self.raum.hind = raumscan.Hindernisse(pos)
                self.raum.minikarte = Minikarte(pos)
                self.raum.start = self._startpunkt()
            finally:
                QApplication.restoreOverrideCursor()
            if self._walk_id != self._raum_id:
                self._walk_id = self._raum_id
                self.raum.zum_start()
            self.raum.thumb = None
            self.raum.set_walk(True)
        else:
            self.raum.set_walk(False)

    def _on_walk_changed(self, on):
        if self.btn_walk.isChecked() != on:
            self.btn_walk.blockSignals(True)
            self.btn_walk.setChecked(on)
            self.btn_walk.blockSignals(False)
        self.btn_walk.setText("BEGEHEN BEENDEN (ESC)" if on else "RAUM BEGEHEN")
        self.badge_walk.setVisible(on)
        self.pane_raum.hud_right.setText("W A S D · MAUS ZIEHEN · Q/E · UMSCHALT · R START · ESC ENDE" if on
                                         else "LINKS DREHEN · MITTE VERSCHIEBEN · RAD ZOOM · B: BEGEHEN")
        self._raum_anzeigen()
        self._update_cam_marker()
        self._update_raum_hud()

    def _on_walkmode(self, v):
        self._set("raum", "modus", v)
        self.raum.set_modus(v)
        if self.raum.walk:
            self.raum.setFocus()

    def _on_walkmode_key(self, v):
        self.seg_walkmode.set_value(v)
        self._set("raum", "modus", v)

    def _on_walk_opts(self):
        auge = self.rng_auge.value() / 100
        tempo = self.rng_tempo.value() / 10
        fuell = self.rng_fuell.value() / 2
        self.lbl_auge.setText(f"{auge:.2f} m")
        self.lbl_tempo.setText(f"{tempo:.1f} m/s")
        self.lbl_fuell.setText(f"{fuell:.1f} px")
        self.raum.kollision = self.chk_kollision.isChecked()
        self.raum.tempo = tempo
        if abs(self.raum.augenhoehe - auge) > 1e-6:
            self.raum.augenhoehe = auge
            if self.raum.modus == "gehen":
                self.raum.eye[2] = auge
                self.raum.update()
        if self._updating_ui:
            return
        cfg_set("raum", "kollision", self.chk_kollision.isChecked())
        cfg_set("raum", "augenhoehe_m", f"{auge:.2f}")
        cfg_set("raum", "tempo_m_s", f"{tempo:.1f}")
        cfg_set("raum", "punkt_px", f"{fuell:.1f}")
        self.save_timer.start()
        if self.raum.walk and self.sender() is self.rng_fuell:
            self._raum_anzeigen()

    # ------------------------------------------------------------- 3D-Maus
    def _on_maus_opts(self):
        e = self.rng_maus_empf.value() / 10
        self.lbl_maus_empf.setText(f"{e:.1f} ×")
        r = self.raum
        r.maus_aktiv = self.chk_maus.isChecked()
        r.maus_empf = e
        r.maus_umkehr.update(vor=self.chk_maus_vor.isChecked(), neigen=self.chk_maus_neigen.isChecked(),
                             drehen=self.chk_maus_drehen.isChecked(),
                             seite=truthy(cfg_get("3dmaus", "umkehr_seite")),
                             hoch=truthy(cfg_get("3dmaus", "umkehr_hoch")))
        if self._updating_ui:
            return
        cfg_set("3dmaus", "aktiv", self.chk_maus.isChecked())
        cfg_set("3dmaus", "empfindlichkeit", f"{e:.1f}")
        cfg_set("3dmaus", "umkehr_vor", self.chk_maus_vor.isChecked())
        cfg_set("3dmaus", "umkehr_neigen", self.chk_maus_neigen.isChecked())
        cfg_set("3dmaus", "umkehr_drehen", self.chk_maus_drehen.isChecked())
        self.save_timer.start()

    def _maus_anmelden(self):
        if self._maus_angemeldet:
            return
        self._maus_angemeldet = True
        if self.maus.registrieren(int(self.winId())):
            log.info("3D-Maus: Raw Input angemeldet, Geräte: %s", ", ".join(self.maus.geraete) or "keine")
        elif spacemouse.IST_WINDOWS:
            log.warning("3D-Maus: %s", self.maus.fehler)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._maus_anmelden()

    def nativeEvent(self, eventType, message):
        if self.maus.registriert:
            try:
                if bytes(eventType) == b"windows_generic_MSG":
                    self.maus.verarbeite_nachricht(message)
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _poll_maus(self):
        m = self.maus
        if not spacemouse.IST_WINDOWS:
            state, text = "idle", "NUR WINDOWS"
        elif not m.registriert:
            state, text = "lost", "FEHLER"
        elif m.empfangen:
            state, text = "ok", "AKTIV"
        elif m.geraete:
            state, text = "file", "GEFUNDEN"
        else:
            state, text = "idle", "NICHT GEFUNDEN"
        if self.lbl_maus.property("state") != state:
            self.lbl_maus.setProperty("state", state)
            self.lbl_maus.style().unpolish(self.lbl_maus)
            self.lbl_maus.style().polish(self.lbl_maus)
        self.lbl_maus.setText(text)
        if m.geraete:
            self.lbl_maus.setToolTip("\n".join(m.geraete))
        a = m.achsen(totzone=0.0)
        self.lbl_maus_achsen.setText(f"T  {a[0]:+.2f} {a[1]:+.2f} {a[2]:+.2f}\nR  {a[3]:+.2f} {a[4]:+.2f} {a[5]:+.2f}")

    # ------------------------------------------------------- Raum laden/speichern
    def _raum_ordner(self):
        d = cfg_get("raum", "letzter_ordner")
        return d if d and Path(d).is_dir() else self._default_dir()

    def raum_speichern(self):
        if self.raum_pos is None or len(self.raum_pos) == 0:
            return self.flash("Noch kein Raum zum Speichern", warn=True)
        self._ausrichten()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "Raum speichern",
                                              str(Path(self._raum_ordner()) / f"raum_{stamp}.ply"),
                                              "PLY-Punktwolke (*.ply)")
        if not path:
            return
        pos = transformieren(self.align, self.raum_pos)
        nor = self.raum_nor @ self.align[:3, :3].T.astype(np.float32) if self.raum_nor is not None else None
        x, y, yaw = self._startpunkt()
        meta = {"start": f"{x:.3f} {y:.3f} {yaw:.1f}", "voxel": f"{self.raum_voxel:.3f}", "boden": "ja"}
        try:
            raumscan.ply_schreiben(path, pos, self._raum_farben(pos), meta, f"{APP_NAME} v{APP_VERSION}", nor=nor)
            cfg_set("raum", "letzter_ordner", str(Path(path).parent))
            self.save_timer.start()
            self.flash(f"Raum gespeichert ({len(pos):,} Punkte): {path}".replace(",", "."))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Speichern fehlgeschlagen:\n{exc}")

    def raum_laden(self):
        path, _ = QFileDialog.getOpenFileName(self, "Raum laden", self._raum_ordner(), "PLY-Punktwolke (*.ply)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            pos, col, nor, meta = raumscan.ply_lesen(path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            return QMessageBox.warning(self, APP_NAME, f"Datei konnte nicht geladen werden:\n{exc}")
        QApplication.restoreOverrideCursor()
        if len(pos) < 200:
            return self.flash("Datei enthält zu wenige Punkte", warn=True)
        if len(pos) > 6_000_000:
            sel = np.random.default_rng(0).choice(len(pos), 6_000_000, replace=False)
            pos, col = pos[sel], col[sel]
            nor = nor[sel] if nor is not None else None
            self.flash("Sehr große Datei – auf 6 Mio. Punkte reduziert", warn=True)
        if self.raum.walk:
            self.btn_walk.setChecked(False)
        if self.btn_scan.isChecked():
            self.btn_scan.setChecked(False)
        try:
            vox = float(meta.get("voxel", ""))
        except ValueError:
            # Punktabstand schätzen: typischer Abstand zum nächsten Nachbarn ~ Rasterweite
            sub = pos[np.random.default_rng(0).choice(len(pos), min(len(pos), 3000), replace=False)]
            ext = np.ptp(pos, 0)
            vox = float(np.clip(np.sqrt((ext[0] * ext[1] + ext[1] * ext[2] + ext[0] * ext[2]) * 2 / len(pos)),
                                0.005, 0.05)) if len(sub) else 0.02
        self.raum_quelle = "datei"
        self.raum_datei = path
        self.raum_pos, self.raum_col, self.raum_nor, self.raum_meta = pos, col, nor, meta
        self.raum_voxel = vox
        self.raum_bahn = np.zeros((0, 3), np.float32)
        cfg_set("raum", "letzter_ordner", str(Path(path).parent))
        self.save_timer.start()
        self._neue_karte()
        if self.view != "raum":
            self.set_view("raum", True)
        self._raum_anzeigen()
        c = np.median(pos, 0)
        self.raum.setCameraPosition(pos=pg.Vector(*map(float, c)), distance=float(np.ptp(pos[:, :2], 0).max() * 1.1 + 1),
                                    elevation=40, azimuth=-110)
        self.flash(f"Raum geladen: {Path(path).name} – B drücken zum Begehen")

    # --------------------------------------------------------------- Export
    def _default_dir(self):
        from PySide6.QtCore import QStandardPaths
        d = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        return d or str(BASE_DIR)

    def save_png(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "Bild speichern",
                                              str(Path(self._default_dir()) / f"realsense_{stamp}.png"),
                                              "PNG-Bild (*.png)")
        if not path:
            return
        if self.view == "tiefe" and self.view_depth.img is not None:
            img = self.view_depth.img
        elif self.view == "farbe" and self.view_color.img is not None:
            img = self.view_color.img
        elif self.view == "raum":
            img = self.raum.grabFramebuffer()
        else:
            img = self.gl.grabFramebuffer()
        if img.save(path):
            self.flash(f"Bild gespeichert: {path}")
        else:
            self.flash("Bild konnte nicht gespeichert werden", warn=True)

    def save_ply(self):
        if self.last is None:
            return self.flash("Noch keine Daten", warn=True)
        n = self.build_cloud(self.last)
        if not n:
            return self.flash("Keine Punkte im eingestellten Bereich", warn=True)
        pos = self.cloud_pos.copy()
        col = (np.clip(self.cloud_col[:, :3], 0, 1) * 255).astype(np.uint8)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "Punktwolke speichern",
                                              str(Path(self._default_dir()) / f"punktwolke_{stamp}.ply"),
                                              "PLY-Punktwolke (*.ply)")
        if not path:
            return
        # Koordinaten wie in der Ansicht: Meter, Z nach oben, Kamera blickt in +Y
        dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
        arr = np.empty(n, dt)
        arr["x"], arr["y"], arr["z"] = pos[:, 0], pos[:, 1], pos[:, 2]
        arr["r"], arr["g"], arr["b"] = col[:, 0], col[:, 1], col[:, 2]
        header = ("ply\nformat binary_little_endian 1.0\n"
                  f"comment {APP_NAME} v{APP_VERSION} - Meter, Z nach oben, Kamera blickt in +Y\n"
                  f"element vertex {n}\nproperty float x\nproperty float y\nproperty float z\n"
                  "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
        try:
            with open(path, "wb") as fh:
                fh.write(header.encode("ascii"))
                fh.write(arr.tobytes())
            self.flash(f"{n:,} Punkte gespeichert: {path}".replace(",", "."))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Speichern fehlgeschlagen:\n{exc}")

    # ---------------------------------------------------------------- Fenster
    def closeEvent(self, e):
        cfg_set("fenster", "maximiert", self.isMaximized())
        if not self.isMaximized() and not self.isFullScreen():
            cfg_set("fenster", "breite", self.width())
            cfg_set("fenster", "hoehe", self.height())
        cfg_save()
        self.scan.stop()
        self.cam.stop()
        super().closeEvent(e)


def dark_titlebar(win):
    """Dunkle Titelleiste unter Windows 10/11."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (neu / alt)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        # Rahmenfarbe (Windows 11)
        color = ctypes.c_int(0x00110E0B)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{APP_VERSION}")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    if (ASSET_DIR / "icon.png").exists():
        app.setWindowIcon(QIcon(str(ASSET_DIR / "icon.png")))

    def excepthook(t, v, tb):
        import traceback
        msg = "".join(traceback.format_exception(t, v, tb))
        log.error(msg)
        QMessageBox.critical(None, APP_NAME, f"Unerwarteter Fehler:\n\n{v}\n\nDetails in {LOG_PATH}")
    sys.excepthook = excepthook

    log.info("%s v%s startet – Konfig %s", APP_NAME, APP_VERSION, CONFIG_PATH)
    cam = CameraWorker()
    cam.start()
    scan = raumscan.ScanWorker(cam)
    scan.start()
    win = MainWindow(cam, scan)
    try:
        win.resize(int(cfg_get("fenster", "breite")), int(cfg_get("fenster", "hoehe")))
    except ValueError:
        win.resize(1480, 880)
    dark_titlebar(win)
    if truthy(cfg_get("fenster", "maximiert")):
        win.showMaximized()
    else:
        win.show()
    rc = app.exec()
    scan.stop()
    cam.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
