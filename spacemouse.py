# -*- coding: utf-8 -*-
"""
3Dconnexion-3D-Maus (SpaceMouse, SpaceNavigator, SpacePilot …) für RealSense-Viewer-MK2.

Unter Windows über Raw Input (WM_INPUT, HID-Nutzungsseite 1 / Nutzung 8 = Mehrachsen-Controller).
Es wird keine Zusatzbibliothek benötigt, und es funktioniert auch, wenn der 3DxWare-Treiber
installiert ist. Die Eingaben kommen nur an, solange das Programmfenster im Vordergrund ist.

Rohachsen der 3D-Maus (HID): X nach rechts, Y zum Benutzer hin, Z nach unten;
RX/RY/RZ sind die Drehungen um diese Achsen. Werte etwa −350 … +350.
"""

import ctypes
import struct
import sys
import time

IST_WINDOWS = sys.platform.startswith("win")

WM_INPUT = 0x00FF
RIM_TYPEHID = 2
RID_INPUT = 0x10000003
RIDI_DEVICEINFO = 0x2000000B
RIDI_DEVICENAME = 0x20000007
USAGE_PAGE_GENERIC = 0x01
USAGE_MULTIAXIS = 0x08
HERSTELLER = {0x046D: "Logitech/3Dconnexion", 0x256F: "3Dconnexion"}
VOLLAUSSCHLAG = 350.0


def berichte_auswerten(daten, anzahl, groesse):
    """Zerlegt die HID-Rohdaten in (achsen_teil, werte). achsen_teil: 'trans', 'rot', 'alle', 'tasten'.
    Ältere Geräte senden Bericht 1 (Verschiebung, 3 Werte) und Bericht 2 (Drehung, 3 Werte),
    neuere Bericht 1 mit allen 6 Werten. Bericht 3 = Tasten (Bitmaske)."""
    out = []
    for i in range(anzahl):
        r = daten[i * groesse:(i + 1) * groesse]
        if len(r) < 2:
            continue
        rid, nutz = r[0], r[1:]
        if rid == 1 and len(nutz) >= 12:
            out.append(("alle", struct.unpack_from("<6h", nutz)))
        elif rid == 1 and len(nutz) >= 6:
            out.append(("trans", struct.unpack_from("<3h", nutz)))
        elif rid == 2 and len(nutz) >= 6:
            out.append(("rot", struct.unpack_from("<3h", nutz)))
        elif rid == 3:
            out.append(("tasten", (int.from_bytes(bytes(nutz[:4]), "little"),)))
    return out


class SpaceMouse:
    """Hält den aktuellen Zustand der 3D-Maus. Die Oberfläche ruft verarbeite_nachricht() aus
    nativeEvent() auf und liest achsen()/tasten() ab."""

    def __init__(self):
        self.roh = [0, 0, 0, 0, 0, 0]       # tx ty tz rx ry rz
        self.tasten = 0
        self.zeit = 0.0
        self.registriert = False
        self.geraete = []                   # Beschreibungen gefundener Geräte
        self.fehler = None
        self.empfangen = False
        self._puffer = None

    # ---------------------------------------------------------------- Windows
    def registrieren(self, hwnd):
        """Raw Input für Mehrachsen-Controller beim Fenster anmelden."""
        if not IST_WINDOWS:
            self.fehler = "nur unter Windows"
            return False
        try:
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)

            class RAWINPUTDEVICE(ctypes.Structure):
                _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                            ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

            user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
            user32.RegisterRawInputDevices.restype = wintypes.BOOL
            user32.GetRawInputData.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.UINT), wintypes.UINT]
            user32.GetRawInputData.restype = wintypes.UINT
            self._user32 = user32
            self._wintypes = wintypes
            dev = RAWINPUTDEVICE(USAGE_PAGE_GENERIC, USAGE_MULTIAXIS, 0, wintypes.HWND(int(hwnd)))
            if not user32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(dev)):
                self.fehler = f"Anmeldung fehlgeschlagen (Fehler {ctypes.get_last_error()})"
                return False
            self.registriert = True
            self.geraete = self._geraete_suchen()
            return True
        except Exception as exc:  # pragma: no cover
            self.fehler = str(exc)
            return False

    def _geraete_suchen(self):
        """Angeschlossene Mehrachsen-Controller auflisten (nur zur Anzeige)."""
        try:
            wt = self._wintypes
            u = self._user32

            class RAWINPUTDEVICELIST(ctypes.Structure):
                _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]

            class RID_DEVICE_INFO_HID(ctypes.Structure):
                _fields_ = [("dwVendorId", wt.DWORD), ("dwProductId", wt.DWORD), ("dwVersionNumber", wt.DWORD),
                            ("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT)]

            class _U(ctypes.Union):
                _fields_ = [("hid", RID_DEVICE_INFO_HID), ("_platz", wt.DWORD * 6)]

            class RID_DEVICE_INFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("dwType", wt.DWORD), ("u", _U)]

            u.GetRawInputDeviceList.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.UINT), wt.UINT]
            u.GetRawInputDeviceList.restype = wt.UINT
            u.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.UINT)]
            u.GetRawInputDeviceInfoW.restype = wt.UINT
            n = wt.UINT(0)
            u.GetRawInputDeviceList(None, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST))
            if n.value == 0:
                return []
            arr = (RAWINPUTDEVICELIST * n.value)()
            got = u.GetRawInputDeviceList(arr, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST))
            if got == 0xFFFFFFFF:
                return []
            gefunden = []
            for i in range(got):
                if arr[i].dwType != RIM_TYPEHID:
                    continue
                info = RID_DEVICE_INFO()
                info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
                sz = wt.UINT(info.cbSize)
                if u.GetRawInputDeviceInfoW(arr[i].hDevice, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(sz)) \
                        in (0, 0xFFFFFFFF):
                    continue
                h = info.u.hid
                if h.usUsagePage == USAGE_PAGE_GENERIC and h.usUsage == USAGE_MULTIAXIS:
                    name = HERSTELLER.get(h.dwVendorId, f"VID {h.dwVendorId:04X}")
                    gefunden.append(f"{name} PID {h.dwProductId:04X}")
            return sorted(set(gefunden))
        except Exception:
            return []

    def verarbeite_nachricht(self, message):
        """Aus QWidget.nativeEvent() aufrufen. Liefert True, wenn es eine 3D-Maus-Nachricht war."""
        if not self.registriert:
            return False
        try:
            msg = self._wintypes.MSG.from_address(_adresse(message))
        except Exception:
            return False
        if msg.message != WM_INPUT:
            return False
        try:
            u, wt = self._user32, self._wintypes
            kopf = 8 + 2 * ctypes.sizeof(ctypes.c_void_p)       # RAWINPUTHEADER
            sz = wt.UINT(0)
            h = ctypes.c_void_p(msg.lParam)
            u.GetRawInputData(h, RID_INPUT, None, ctypes.byref(sz), kopf)
            if sz.value == 0 or sz.value > 4096:
                return True
            if self._puffer is None or len(self._puffer) < sz.value:
                self._puffer = ctypes.create_string_buffer(max(sz.value, 256))
            n = u.GetRawInputData(h, RID_INPUT, self._puffer, ctypes.byref(sz), kopf)
            if n in (0, 0xFFFFFFFF):
                return True
            raw = self._puffer.raw[:n]
            typ = struct.unpack_from("<I", raw, 0)[0]
            if typ != RIM_TYPEHID:
                return True
            groesse, anzahl = struct.unpack_from("<II", raw, kopf)
            self.verarbeite_hid(raw[kopf + 8:kopf + 8 + groesse * anzahl], anzahl, groesse)
        except Exception as exc:  # pragma: no cover
            self.fehler = str(exc)
        return True

    # ---------------------------------------------------------------- Auswertung
    def verarbeite_hid(self, daten, anzahl, groesse):
        for teil, werte in berichte_auswerten(daten, anzahl, groesse):
            if teil == "alle":
                self.roh = list(werte)
            elif teil == "trans":
                self.roh[0:3] = werte
            elif teil == "rot":
                self.roh[3:6] = werte
            elif teil == "tasten":
                self.tasten = werte[0]
            self.zeit = time.time()
            self.empfangen = True

    def achsen(self, totzone=0.06):
        """Normierte Achsen (−1 … 1) mit Totzone; nach 1 s ohne Bericht gilt die Kappe als losgelassen."""
        if time.time() - self.zeit > 1.0:
            return [0.0] * 6
        out = []
        for v in self.roh:
            x = max(-1.0, min(1.0, v / VOLLAUSSCHLAG))
            if abs(x) < totzone:
                out.append(0.0)
            else:
                x = (abs(x) - totzone) / (1 - totzone) * (1 if x > 0 else -1)
                out.append(x * abs(x) ** 0.5)           # feinfühlig bei kleinen Ausschlägen
        return out


def _adresse(message):
    try:
        return int(message)
    except TypeError:
        return ctypes.cast(message, ctypes.c_void_p).value
