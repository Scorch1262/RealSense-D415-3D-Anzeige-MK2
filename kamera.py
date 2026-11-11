# -*- coding: utf-8 -*-
"""
Kamera-Anbindung und Konfiguration für RealSense-Viewer-MK2.
Läuft in einem eigenen Thread; die Oberfläche holt sich jeweils das neueste Bild ab.
"""

import configparser
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np

try:
    import pyrealsense2 as rs
    RS_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    rs = None
    RS_IMPORT_ERROR = str(exc)

log = logging.getLogger("RealSense-Viewer-MK2")

# --------------------------------------------------------------------------
# Pfade
# --------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    if sys.platform == "darwin":
        # In einer Mac-App darf nicht ins Programmpaket geschrieben werden:
        # config.ini, Protokoll und Selbsttest liegen im Benutzerordner
        BASE_DIR = Path.home() / "Library" / "Application Support" / "RealSense-Viewer-MK2"
        BASE_DIR.mkdir(parents=True, exist_ok=True)
else:
    BASE_DIR = Path(__file__).resolve().parent
    RES_DIR = BASE_DIR

CONFIG_PATH = BASE_DIR / "config.ini"
ASSET_DIR = RES_DIR / "assets"

# --------------------------------------------------------------------------
# Konfiguration (menschenlesbar, neben der exe)
# --------------------------------------------------------------------------
DEFAULTS = {
    "kamera": {
        "aufloesung": "848x480",
        "fps": "30",
        "seriennummer": "",
        "demo_modus": "auto",          # auto | ja | nein
        "laser": "ja",
        "laserleistung": "150",
        "preset": "",
    },
    "verarbeitung": {
        "punktabstand": "2",
        "spatial_filter": "ja",
        "temporal_filter": "ja",
        "loecher_fuellen": "nein",
        "min_distanz_m": "0.15",
        "max_distanz_m": "3.0",
    },
    "ansicht": {
        "farbmodus": "rgb",            # rgb | tiefe | amber | teal
        "punktgroesse": "2.0",
        "ansicht": "3d",               # 3d | tiefe | farbe | geteilt | raum
        "gitter": "ja",
        "achsen": "ja",
    },
    "raum": {
        "voxel_cm": "2",               # Punktabstand der Raumkarte: 1 | 2 | 3
        "modus": "gehen",              # gehen | fliegen
        "kollision": "ja",             # Wände und Möbel blockieren beim Gehen
        "augenhoehe_m": "1.60",
        "tempo_m_s": "1.4",
        "punkt_px": "2.0",             # Punktgröße beim Begehen in Pixeln (wie in der Scan-Ansicht)
        "letzter_ordner": "",
        "farbe": "hoehe",              # Kartenfarbe: hoehe | grau (schattiert, ohne Farbkamera)
    },
    "3dmaus": {
        "aktiv": "ja",                 # 3Dconnexion-3D-Maus beim Begehen verwenden
        "empfindlichkeit": "1.0",
        "umkehr_vor": "nein",          # Achsen einzeln umkehren: ja | nein
        "umkehr_seite": "nein",
        "umkehr_hoch": "nein",
        "umkehr_neigen": "nein",
        "umkehr_drehen": "nein",
    },
    "fenster": {
        "breite": "1480",
        "hoehe": "880",
        "maximiert": "nein",
    },
}

AUFLOESUNGEN = ["1280x720", "848x480", "640x480", "640x360", "480x270", "424x240"]
FPS_WERTE = [6, 15, 30]

_cfg_lock = threading.Lock()


def truthy(v):
    return str(v).strip().lower() in ("1", "ja", "yes", "true", "an", "on")


def _load():
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.read(CONFIG_PATH, encoding="utf-8")
        except Exception as exc:
            log.warning("config.ini nicht lesbar (%s) – Standardwerte", exc)
    return cfg


CFG = _load()


def cfg_get(section, key):
    with _cfg_lock:
        return CFG.get(section, key, fallback=DEFAULTS[section][key])


def cfg_set(section, key, value):
    if isinstance(value, bool):
        value = "ja" if value else "nein"
    with _cfg_lock:
        CFG.set(section, key, str(value))


def cfg_save():
    header = (
        "# RealSense-Viewer-MK2 – Konfiguration\n"
        "# Liegt neben der exe und kann mit jedem Texteditor bearbeitet werden.\n"
        "# Werte für ja/nein: ja | nein.  demo_modus: auto | ja | nein\n"
        "# [raum] modus: gehen | fliegen.  voxel_cm: 1 | 2 | 3.  farbe: hoehe | grau\n\n"
    )
    with _cfg_lock:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                fh.write(header)
                CFG.write(fh)
        except Exception as exc:
            log.warning("config.ini nicht schreibbar: %s", exc)


if not CONFIG_PATH.exists():
    cfg_save()


# --------------------------------------------------------------------------
# Kamera-Thread
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Profil-Auswahl
# --------------------------------------------------------------------------
def _info(dev, key):
    try:
        return dev.get_info(key) if dev.supports(key) else ""
    except Exception:
        return ""


def list_profiles(dev):
    """Liefert (tiefe, farbe) als Listen von (breite, hoehe, fps, format)."""
    depth, color = set(), set()
    color_ok = {rs.format.rgb8: 0, rs.format.bgr8: 1}
    for s in dev.query_sensors():
        try:
            profiles = s.get_stream_profiles()
        except Exception as exc:
            log.warning("Sensorprofile nicht lesbar: %s", exc)
            continue
        for p in profiles:
            if not p.is_video_stream_profile():
                continue
            vp = p.as_video_stream_profile()
            entry = (vp.width(), vp.height(), p.fps(), p.format())
            if p.stream_type() == rs.stream.depth and p.format() == rs.format.z16:
                depth.add(entry)
            elif p.stream_type() == rs.stream.color and p.format() in color_ok:
                color.add(entry)
    return sorted(depth, key=lambda e: (-e[0] * e[1], -e[2])), \
        sorted(color, key=lambda e: (-e[0] * e[1], -e[2]))


def choose_profile(profiles, w, h, fps, prefer_rgb=False):
    """Wählt das Profil, das Wunschauflösung und -bildrate am nächsten kommt.
    Exakte Auflösung gewinnt, solange sie mindestens die halbe Wunsch-Bildrate schafft;
    sonst zählt zuerst die Bildrate, dann die Nähe der Auflösung."""
    want_px = w * h

    def score(p):
        pw, ph, pf, fmt = p
        res = abs(pw * ph - want_px) / max(want_px, 1)
        exact = 0 if (pw, ph) == (w, h) and pf >= fps * 0.5 else 1
        fps_pen = 0 if pf == fps else (1 + (fps - pf) / max(fps, 1) if pf < fps else 2 + (pf - fps))
        fmt_pen = 0 if (not prefer_rgb or rs is None or fmt == rs.format.rgb8) else 1
        return (exact, fps_pen, res, fmt_pen)
    return min(profiles, key=score)


def fmt_profiles(profiles):
    out = []
    for p in profiles:
        if p is None:
            continue
        name = str(p[3]).split(".")[-1]
        out.append(f"{p[0]}x{p[1]}@{p[2]}({name})")
    return ", ".join(out)


class Frame:
    __slots__ = ("seq", "depth", "rgb", "fx", "fy", "ppx", "ppy", "units", "demo", "fps")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class CameraWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="Kamera")
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.restart_flag = threading.Event()
        self.stop_flag = threading.Event()
        self.option_queue = []
        self.nur_tiefe = False          # Raumscan: Farbsensor aus, nur Tiefe (funktioniert im Dunkeln)
        self.status = {
            "verbunden": False, "demo": False, "meldung": "Starte …", "geraet": {},
            "fps": 0.0, "aufloesung": "", "temperatur": None, "optionen": {},
            "sdk": (getattr(rs, "__version__", "?") if rs else None),
            "sdk_fehler": RS_IMPORT_ERROR,
        }
        self._fps_t = time.time()
        self._fps_n = 0
        self._fps = 0.0

    # ---------------- Schnittstelle für die Oberfläche ----------------
    def request_restart(self):
        self.restart_flag.set()

    def stop(self):
        self.stop_flag.set()
        self.restart_flag.set()

    def set_nur_tiefe(self, on):
        """Für den Raumscan: Kamera ohne Farbstrom betreiben, IR-Projektor an. Startet die Kamera neu."""
        on = bool(on)
        if on != self.nur_tiefe:
            self.nur_tiefe = on
            with self.lock:
                laeuft = self.status.get("verbunden")
            if laeuft:
                self.restart_flag.set()

    def queue_option(self, name, value):
        with self.lock:
            self.option_queue.append((name, value))

    def latest(self):
        with self.lock:
            return self.frame

    def get_status(self):
        with self.lock:
            st = dict(self.status)
            st["geraet"] = dict(st["geraet"])
            st["optionen"] = dict(st["optionen"])
            return st

    def _set_status(self, **kw):
        with self.lock:
            self.status.update(kw)

    def _publish(self, depth, rgb, fx, fy, ppx, ppy, units, demo):
        with self.lock:
            self.seq += 1
            self.frame = Frame(seq=self.seq, depth=depth, rgb=rgb, fx=fx, fy=fy, ppx=ppx,
                               ppy=ppy, units=units, demo=demo, fps=self._fps)

    def _tick_fps(self):
        self._fps_n += 1
        now = time.time()
        if now - self._fps_t >= 1.0:
            self._fps = self._fps_n / (now - self._fps_t)
            self._fps_n = 0
            self._fps_t = now
            self._set_status(fps=round(self._fps, 1))

    def _idle(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.restart_flag.is_set():
            time.sleep(0.05)

    @staticmethod
    def _step():
        try:
            return max(1, min(8, int(float(cfg_get("verarbeitung", "punktabstand")))))
        except ValueError:
            return 1

    # ---------------- Hauptschleife ----------------
    def run(self):
        while not self.stop_flag.is_set():
            self.restart_flag.clear()
            mode = cfg_get("kamera", "demo_modus").strip().lower()
            if truthy(mode):
                self._set_status(meldung="Demo-Szene aktiv")
                self._run_demo(forever=True)
                continue
            if rs is None:
                self._set_status(meldung="RealSense-SDK (pyrealsense2) nicht verfügbar – Demo-Szene")
                self._run_demo(forever=True)
                continue
            try:
                n = len(rs.context().query_devices())
            except Exception as exc:
                n = 0
                log.warning("Geräteabfrage fehlgeschlagen: %s", exc)
            if n == 0:
                self._set_status(verbunden=False, geraet={}, optionen={},
                                 meldung="Keine RealSense-Kamera gefunden – USB-Kabel prüfen (USB 3 empfohlen)")
                if mode == "auto":
                    self._run_demo(forever=False, dauer=3.0)
                else:
                    with self.lock:
                        self.frame = None
                    self._set_status(demo=False, aufloesung="", temperatur=None)
                    self._idle(2.0)
                continue
            try:
                self._run_camera()
                self._last_err = None
            except Exception as exc:
                if str(exc) != getattr(self, "_last_err", None):
                    self._last_err = str(exc)
                    log.error("Kamerafehler: %s", exc)
                self._set_status(verbunden=False, demo=False, meldung=f"Kamerafehler: {exc}")
                with self.lock:
                    self.frame = None
                self._idle(3.0)

    # ---------------- echte Kamera ----------------
    def _run_camera(self):
        try:
            want_w, want_h = [int(x) for x in cfg_get("kamera", "aufloesung").lower().split("x")]
        except Exception:
            want_w, want_h = 848, 480
        want_fps = int(float(cfg_get("kamera", "fps") or 30))
        serial_cfg = cfg_get("kamera", "seriennummer").strip()

        # 1) Gerät auswählen und Infos lesen (auch wenn der Start später scheitert)
        devs = list(rs.context().query_devices())
        dev = None
        for d in devs:
            if not serial_cfg or _info(d, rs.camera_info.serial_number) == serial_cfg:
                dev = d
                break
        if dev is None:
            raise RuntimeError(f"Kamera mit Seriennummer {serial_cfg} nicht gefunden")
        geraet = {
            "name": _info(dev, rs.camera_info.name),
            "seriennummer": _info(dev, rs.camera_info.serial_number),
            "firmware": _info(dev, rs.camera_info.firmware_version),
            "usb": _info(dev, rs.camera_info.usb_type_descriptor),
        }
        self._set_status(geraet=geraet)
        serial = geraet["seriennummer"]

        # 2) Tatsächlich angebotene Bildformate abfragen
        depth_list, color_list = list_profiles(dev)
        sig = (serial, len(depth_list), len(color_list), geraet["usb"])
        if sig != getattr(self, "_last_diag", None):
            self._last_diag = sig
            log.info("Kamera: %s  SN %s  FW %s  USB %s", *geraet.values())
            log.info("Tiefenformate (z16): %s", fmt_profiles(depth_list) or "KEINE")
            log.info("Farbformate: %s", fmt_profiles(color_list) or "KEINE")
        if not depth_list:
            raise RuntimeError(
                "Kamera erkannt, liefert aber keine Tiefenformate. Mögliche Ursachen: Windows-Datenschutz "
                "(Einstellungen → Datenschutz → Kamera → Desktop-Apps Zugriff erlauben), Kamera wird von einem "
                "anderen Programm benutzt, oder USB-Problem – Kamera ab- und wieder anstecken.")

        # 3) Bestes passendes Profil wählen und prüfen, ob das SDK es auflösen kann
        d_choice = choose_profile(depth_list, want_w, want_h, want_fps)
        nur_tiefe = self.nur_tiefe
        c_candidates = [c for c in color_list if c[2] == d_choice[2]] if not nur_tiefe else []
        c_choice = choose_profile(c_candidates, d_choice[0], d_choice[1], d_choice[2], prefer_rgb=True) \
            if c_candidates else None

        pipeline = rs.pipeline()
        wrapper = rs.pipeline_wrapper(pipeline)

        def build(with_color):
            c = rs.config()
            c.enable_device(serial)
            c.enable_stream(rs.stream.depth, d_choice[0], d_choice[1], rs.format.z16, d_choice[2])
            if with_color and c_choice:
                c.enable_stream(rs.stream.color, c_choice[0], c_choice[1], c_choice[3], c_choice[2])
            return c

        has_color = c_choice is not None
        config = build(has_color)
        if not config.can_resolve(wrapper):
            log.warning("Kombination Tiefe %s + Farbe %s nicht auflösbar – nur Tiefe",
                        fmt_profiles([d_choice]), fmt_profiles([c_choice]) if c_choice else "-")
            has_color = False
            config = build(False)
            if not config.can_resolve(wrapper):
                raise RuntimeError(f"SDK kann Tiefenformat {fmt_profiles([d_choice])} nicht öffnen")

        self._set_status(meldung=f"Verbinde mit Kamera ({d_choice[0]}x{d_choice[1]} @ {d_choice[2]} fps) …")
        profile = pipeline.start(config)
        log.info("Gestartet: Tiefe %s, Farbe %s", fmt_profiles([d_choice]),
                 fmt_profiles([c_choice]) if has_color else "aus")
        color_bgr = has_color and c_choice[3] == rs.format.bgr8
        fps = d_choice[2]

        dev = profile.get_device()
        sensor = dev.first_depth_sensor()
        units = sensor.get_depth_scale()
        abweichung = ""
        if (d_choice[0], d_choice[1], d_choice[2]) != (want_w, want_h, want_fps):
            abweichung = f" – {want_w}x{want_h} @ {want_fps} nicht verfügbar, nutze {d_choice[0]}x{d_choice[1]} @ {d_choice[2]}"
        self._apply_initial_options(sensor)
        if nur_tiefe:
            # Im Dunkeln liefert die Stereo-Tiefe nur mit Projektor-Muster Werte
            try:
                if sensor.supports(rs.option.emitter_enabled):
                    sensor.set_option(rs.option.emitter_enabled, 1.0)
            except Exception as exc:
                log.warning("Projektor für Raumscan: %s", exc)

        intr = profile.get_stream(rs.stream.depth).as_video_stream_profile().get_intrinsics()
        align = rs.align(rs.stream.depth) if has_color else None
        spatial, temporal, holes = rs.spatial_filter(), rs.temporal_filter(), rs.hole_filling_filter()

        hint = (" – Achtung: nur USB 2 erkannt" if geraet["usb"].startswith("2") else "") + abweichung
        self._set_status(verbunden=True, demo=False, geraet=geraet,
                         aufloesung=f"{intr.width}x{intr.height} @ {fps} fps" + (
                             "" if has_color else (" (nur Tiefe – Raumscan)" if nur_tiefe else " (ohne Farbe)")),
                         meldung="Kamera läuft" + hint, optionen=self._read_options(sensor))
        last_temp = 0.0
        try:
            while not self.restart_flag.is_set():
                self._process_option_queue(sensor)
                if self.nur_tiefe != nur_tiefe:
                    break
                try:
                    frames = pipeline.wait_for_frames(3000)
                except Exception as exc:
                    raise RuntimeError(f"Keine Bilder mehr von der Kamera ({exc})")
                if align is not None:
                    frames = align.process(frames)
                depth = frames.get_depth_frame()
                if not depth:
                    continue
                color = frames.get_color_frame() if has_color else None
                if truthy(cfg_get("verarbeitung", "spatial_filter")):
                    depth = spatial.process(depth)
                if truthy(cfg_get("verarbeitung", "temporal_filter")):
                    depth = temporal.process(depth)
                if truthy(cfg_get("verarbeitung", "loecher_fuellen")):
                    depth = holes.process(depth)

                d = np.asanyarray(depth.get_data())
                rgb = np.asanyarray(color.get_data()) if color else None
                if rgb is not None and color_bgr:
                    rgb = rgb[..., ::-1]
                step = self._step()
                if step > 1:
                    d = d[::step, ::step]
                    rgb = rgb[::step, ::step] if rgb is not None else None
                self._tick_fps()
                self._publish(np.ascontiguousarray(d), None if rgb is None else np.ascontiguousarray(rgb),
                              intr.fx / step, intr.fy / step,
                              (intr.ppx + 0.5) / step - 0.5, (intr.ppy + 0.5) / step - 0.5,
                              units, False)
                now = time.time()
                if now - last_temp > 2.0:
                    last_temp = now
                    self._set_status(temperatur=self._read_temp(sensor))
        finally:
            try:
                pipeline.stop()
            except Exception:
                pass
            self._set_status(verbunden=False)

    def _apply_initial_options(self, sensor):
        try:
            preset = cfg_get("kamera", "preset").strip()
            if preset and sensor.supports(rs.option.visual_preset):
                self._set_preset(sensor, preset)
        except Exception as exc:
            log.warning("Preset: %s", exc)
        try:
            if sensor.supports(rs.option.emitter_enabled):
                sensor.set_option(rs.option.emitter_enabled, 1.0 if truthy(cfg_get("kamera", "laser")) else 0.0)
        except Exception as exc:
            log.warning("Laser: %s", exc)
        try:
            if sensor.supports(rs.option.laser_power):
                r = sensor.get_option_range(rs.option.laser_power)
                val = float(cfg_get("kamera", "laserleistung") or r.default)
                sensor.set_option(rs.option.laser_power, min(max(val, r.min), r.max))
        except Exception as exc:
            log.warning("Laserleistung: %s", exc)

    def _set_preset(self, sensor, name):
        r = sensor.get_option_range(rs.option.visual_preset)
        for i in range(int(r.min), int(r.max) + 1):
            try:
                if sensor.get_option_value_description(rs.option.visual_preset, i) == name:
                    sensor.set_option(rs.option.visual_preset, i)
                    return True
            except Exception:
                continue
        return False

    def _read_options(self, sensor):
        out = {}
        try:
            if sensor.supports(rs.option.emitter_enabled):
                out["laser"] = sensor.get_option(rs.option.emitter_enabled) > 0.5
            if sensor.supports(rs.option.laser_power):
                r = sensor.get_option_range(rs.option.laser_power)
                out.update(laserleistung=sensor.get_option(rs.option.laser_power),
                           laserleistung_min=r.min, laserleistung_max=r.max,
                           laserleistung_step=r.step or 30)
            if sensor.supports(rs.option.visual_preset):
                r = sensor.get_option_range(rs.option.visual_preset)
                names = []
                for i in range(int(r.min), int(r.max) + 1):
                    try:
                        names.append(sensor.get_option_value_description(rs.option.visual_preset, i))
                    except Exception:
                        pass
                out["presets"] = [n for n in names if n]
                out["preset"] = sensor.get_option_value_description(
                    rs.option.visual_preset, int(sensor.get_option(rs.option.visual_preset)))
        except Exception as exc:
            log.debug("Optionen lesen: %s", exc)
        return out

    def _read_temp(self, sensor):
        for opt in ("asic_temperature", "projector_temperature"):
            try:
                o = getattr(rs.option, opt)
                if sensor.supports(o):
                    return round(sensor.get_option(o), 1)
            except Exception:
                continue
        return None

    def _process_option_queue(self, sensor):
        with self.lock:
            queue, self.option_queue = self.option_queue, []
        if not queue:
            return
        for name, value in queue:
            try:
                if name == "laser" and sensor.supports(rs.option.emitter_enabled):
                    sensor.set_option(rs.option.emitter_enabled, 1.0 if value else 0.0)
                elif name == "laserleistung" and sensor.supports(rs.option.laser_power):
                    sensor.set_option(rs.option.laser_power, float(value))
                elif name == "preset" and sensor.supports(rs.option.visual_preset):
                    self._set_preset(sensor, str(value))
            except Exception as exc:
                log.warning("Option %s=%s fehlgeschlagen: %s", name, value, exc)
        self._set_status(optionen=self._read_options(sensor))

    # ---------------- Demo-Raum ----------------
    def _run_demo(self, forever=True, dauer=3.0):
        """Möbliertes Zimmer, durch das eine Kamera langsam geht (zum Ausprobieren von Scan und Begehen)."""
        import demoraum
        w, h = 640, 360
        fx = fy = 450.0
        ppx, ppy = (w - 1) / 2.0, (h - 1) / 2.0
        units = 0.001
        rng = np.random.default_rng()
        self._set_status(demo=True, verbunden=False, optionen={}, temperatur=None,
                         geraet={"name": "Demo-Raum", "seriennummer": "—", "firmware": "—", "usb": "—"},
                         aufloesung=f"{w}x{h} @ 15 fps (Demo-Raum)")
        start = time.time()
        t0 = getattr(self, "_demo_t", 0.0)       # Kamerafahrt läuft nach Neustart weiter
        while not self.restart_flag.is_set():
            t = time.time()
            if not forever and t - start > dauer:
                break
            tm = t0 + (t - start)
            self._demo_t = tm
            z, rgb = demoraum.render(w, h, fx, fy, ppx, ppy, *demoraum.kamera_pose(tm))
            z = z + rng.normal(0, 0.002, z.shape).astype(np.float32) * z * z
            z[rng.random(z.shape) < 0.01] = 0
            depth = np.clip(np.nan_to_num(z / units, posinf=0), 0, 65535).astype(np.uint16)

            step = self._step()
            sfx, sfy, sppx, sppy = fx, fy, ppx, ppy
            if step > 1:
                depth, rgb = depth[::step, ::step], rgb[::step, ::step]
                sfx, sfy = fx / step, fy / step
                sppx, sppy = (ppx + 0.5) / step - 0.5, (ppy + 0.5) / step - 0.5
            self._tick_fps()
            self._publish(np.ascontiguousarray(depth), None if self.nur_tiefe else np.ascontiguousarray(rgb),
                          sfx, sfy, sppx, sppy, units, True)
            rest = 1 / 15 - (time.time() - t)
            if rest > 0:
                time.sleep(rest)
        self._set_status(demo=False)
