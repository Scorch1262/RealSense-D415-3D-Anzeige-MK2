# Änderungen

## MK2 1.2.0
- **Wiederfinden nach Tracking-Abbruch neu:** Statt nur in der Nähe der letzten (veralteten) Position zu
  suchen, werden alle früheren Schlüsselbilder über einen Tiefen-Fingerabdruck verglichen; die ähnlichsten
  werden per ICP angelegt. Damit findet sich das Tracking wieder, sobald ein bereits gescannter Bereich
  erneut ins Bild kommt – auch aus etwas anderem Blickwinkel
- Jeder Wiederfinden-Treffer wird gegen die Gesamtkarte geprüft (≥ 55 % der Punkte auf kartierten
  Flächen); vorher konnte das Tracking an ähnlichen, aber falschen Wänden einrasten
- Schlüsselbilder platzsparend gespeichert (float16), bis zu 1500 statt 400
- **Begehen:** Punkte haben eine feste Bildschirmgröße wie in der Scan-Ansicht und werden in der Nähe
  nicht mehr aufgebläht; Regler „Punktgröße beim Begehen“ (1–8 px) ersetzt die Flächenfüllung;
  config.ini `[raum] punkt_px`

## MK2 1.1.0
- **3Dconnexion-3D-Maus** beim Begehen: Kappe schieben = gehen, drehen = umdrehen, neigen = hoch/runter
  sehen, heben/drücken = hoch/runter (Fliegen), linke Taste = Startpunkt, rechte Taste = Gehen/Fliegen;
  Anbindung über Windows Raw Input (ohne Zusatzbibliothek, auch neben 3DxWare)
- Neues Feld **3D-MAUS**: Gerätestatus, Live-Achsen, Empfindlichkeit, Achsen umkehren; config.ini `[3dmaus]`
- **Raumscan nur noch aus Tiefe:** das Farbbild wird vollständig ignoriert; während des Scans läuft die
  Kamera ohne Farbstrom und mit eingeschaltetem IR-Projektor – funktioniert auch bei völliger Dunkelheit
- Karte speichert Flächennormalen statt Farben; **Kartenfarbe** HÖHE (Farbskala nach Höhe) oder GRAU,
  jeweils schattiert; config.ini `[raum] farbe`
- Live-Vorschau beim Scan zeigt das Tiefenbild statt des Farbbilds
- PLY-Export des Raums enthält Normalen; beim Laden werden sie für die Kartenfarbe genutzt
- Selbsttest prüft zusätzlich die 3D-Maus-Auswertung

## MK2 1.0.0
Neuer Name **RealSense-Viewer-MK2** (baut auf MK1 2.0.2 auf).
- **Raumscan:** Kamera durch den Raum bewegen, alle Bilder werden per ICP (Punkt-zu-Ebene mit
  Helligkeitsterm) zu einer gemeinsamen 3D-Karte zusammengesetzt; Wiederfinden nach Tracking-Verlust
  über frühere Schlüsselbilder
- **Begehen:** Ich-Perspektive in Augenhöhe, W A S D / Pfeile gehen, Maus ziehen umsehen, Modi Gehen
  und Fliegen, Kollision mit Wänden und Möbeln, Minikarte mit Position und Blickrichtung, Startpunkt
- Boden wird automatisch erkannt (RANSAC, Tischplatten werden nicht für den Boden gehalten) und
  waagerecht bei Z = 0 ausgerichtet
- Neuer Reiter **RAUM** (Taste 5) mit Live-Vorschaubild, Kamerabahn und Kamerasymbol während des Scans
- Raum als PLY speichern und wieder laden (auch ohne Kamera); fremde PLY-Dateien (binär/ASCII) ladbar
- Punktabstand der Karte 1 / 2 / 3 cm; Augenhöhe, Gehtempo und Flächenfüllung einstellbar
- Demo-Szene ersetzt durch einen **möblierten Demo-Raum** mit umhergehender Kamera, damit Scan und
  Begehen auch ohne D415 ausprobiert werden können
- Neue Einstellungen im Abschnitt `[raum]` der config.ini
- Selbsttest prüft zusätzlich den Raumscan

# Ältere Versionen (MK1)

## 2.0.2
- README zeigt einen Screenshot des Programms (`docs/screenshot.png`)

## 2.0.1
- Fehler „Couldn't resolve requests“ behoben: Das Programm fragt jetzt die tatsächlich von der Kamera
  angebotenen Formate ab und wählt das passendste (z. B. an USB 2 automatisch 640×480 @ 30 statt 1280×720)
- Abweichung von der Wunschauflösung wird in der Statuszeile angezeigt
- Farbstrom auch im BGR-Format nutzbar, sonst automatisch nur Tiefe
- Diagnose im Log: Gerät, USB-Typ, alle verfügbaren Tiefen-/Farbformate, gewähltes Format
- Klare Meldung, wenn die Kamera erkannt wird, aber keine Formate liefert (Windows-Kamera-Datenschutz, belegte Kamera)
- Gleiche Fehlermeldung wird nicht mehr alle paar Sekunden ins Log geschrieben

## 2.0.0
- Komplett auf reines Desktop-Programm umgestellt (PySide6/Qt + OpenGL), kein Browser und kein Webserver mehr
- 3D-Ansicht mit pyqtgraph-OpenGL, Tiefen- und Farbbild als native Ansichten
- Gleiches Dashboard-Design (dunkel, Amber/Teal, HUD-Ecken), dunkle Windows-Titelleiste
- Tastenkürzel (Leertaste, Strg+S, Strg+P, 1–4, F11), Fenstergröße wird gespeichert
- Speichern von PLY/PNG über Windows-Dateidialog
- Protokolldatei `realsense-viewer.log` neben der exe
- Build: Fenster-exe ohne Konsole, Selbsttest im Workflow

## 1.0.0
- Erste Version (Browser-Oberfläche)
