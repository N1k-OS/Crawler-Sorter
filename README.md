# Moodle Sync & KI-Sortierer für NotebookLM

Ein lokales CLI-Tool, das Moodle-Kurse automatisiert crawlt, Dateien (inkl. Inline-PDFs und Pop-up-Ressourcen) herunterlädt, passwortgeschützte PDFs entschlüsselt, Moodle-Tests abfotografiert, Links gruppiert und alle Unterlagen mithilfe der Google Gemini API für **NotebookLM** vorsortiert.

---

## 1. Einrichtung & Schnellstart

### Voraussetzungen
1. **Abhängigkeiten installieren** (empfohlen in einer `.venv` im Projektordner oder unter `~/.venv`):
   ```bash
   pip install -r requirements.txt
   ```
   *(Hinweis: Das Skript startet Playwright mit `channel="chrome"` — Google Chrome sollte daher installiert sein.)*
2. **Konfiguration anlegen:**
   `config.example.json` nach `config.json` kopieren und API-Key, Zielordner (`base_dir`), optionale Moodle-Zugangsdaten, `pdf_passwords` sowie `standard_kurse` eintragen.

### Starten (Windows, Git Bash, Linux & macOS)

Im Repository liegen passende Starter für alle gängigen Shells:

| Umgebung | Starter-Datei | Aufruf (wenn im `PATH`) | Aufruf direkt im Ordner |
| :--- | :--- | :--- | :--- |
| **Windows (PowerShell / CMD / Raycast)** | `moodle-sync.bat` | `moodle-sync` | `.\moodle-sync.bat` |
| **Windows (Git Bash)** | `moodle-sync` / `moodle-sync.sh` | `moodle-sync` | `./moodle-sync` oder `./moodle-sync.sh` |
| **Linux / macOS** | `moodle-sync` / `moodle-sync.sh` | `moodle-sync` | `./moodle-sync` oder `./moodle-sync.sh` |

> **Hinweis für Linux / macOS:** Einmalig ausführbar machen:
> ```bash
> chmod +x moodle-sync moodle-sync.sh
> ```
> Optional lässt sich `moodle-sync` über den `PATH` oder per Symlink (z. B. `ln -s /pfad/zu/moodle-sync ~/.local/bin/moodle-sync`) systemweit verfügbar machen.

Alle Starter suchen Python automatisch in derselben Reihenfolge:
1. Lokale `.venv` im Projektordner (`.venv/Scripts/python.exe` bzw. `.venv/bin/python`)
2. Benutzer-`.venv` (`%USERPROFILE%\.venv` bzw. `~/.venv`)
3. System-Python im `PATH` (`py -3` / `python3` / `python`)

---

## 2. Alle CLI-Optionen & Befehle

### Hauptmodi (nicht kombinierbar)

| Befehl | Beschreibung |
| :--- | :--- |
| `moodle-sync` | **Standard-Modus:** Arbeitet nacheinander alle Kurse aus `standard_kurse` in `config.json` ab. |
| `moodle-sync -l "URL"` | **Einzelkurs-Modus:** Verarbeitet genau diesen einen Moodle-Kurs. Der Zielordner wird automatisch aus dem Kurstitel (ohne Professoren-Klammern und ohne Leerzeichen) in deinem `base_dir` erstellt. |
| `moodle-sync -l "URL" -o "PFAD"` | **Einzelkurs mit Zielordner:** Wie `-l`, speichert aber alles in den von dir angegebenen Ordnerpfad. |
| `moodle-sync -b "datei.txt"` | **Batch-Datei-Modus:** Liest eine Textdatei Zeile für Zeile ein und arbeitet alle enthaltenen Kurse ab (Format: reine `URL` oder `Zielordner \| URL`, Zeilen mit `#` werden ignoriert). |
| `moodle-sync --sort "ORDNERPFAD"` | **Offline-Sortierer:** Entschlüsselt und sortiert bereits lokal vorhandene Dateien im Root eines bestehenden Fachordners (ohne Moodle-Login). |

### Zusatz-Flags (mit allen Modi kombinierbar)

| Flag | Beschreibung |
| :--- | :--- |
| `--login` | Erzwingt das Öffnen des sichtbaren Chrome-Fensters für einen frischen Login (nützlich, falls die Session abgelaufen ist). |
| `--force` | Ignoriert die gespeicherten Status-Dateien (`.sort_state.json` und `.quiz_state.json`), klassifiziert alle Dateien im Root neu und prüft alle Tests erneut. |
| `--quiz-critical` | Erlaubt dem Skript, auch offene Moodle-Tests mit **Zeitbegrenzung** oder **begrenzten Versuchen** zu starten und abzufotografieren (standardmäßig aus Sicherheitsgründen deaktiviert). |
| `-h` / `--help` | Zeigt eine Kurzübersicht aller Parameter im Terminal an. |

**Praxis-Beispiele:**
```bash
# Alle Semesterkurse aktualisieren
moodle-sync

# Spontan einen Kurs herunterladen, aber kritische Tests sicher überspringen
moodle-sync -l "https://moodle.hm.edu/course/view.php?id=95"

# Einen bereits vorhandenen lokalen Ordner aufräumen (Windows-Beispiel)
moodle-sync --sort "C:\Users\Super\Documents\Studium\Hochschule\PDFs\IT-Systeme"
```

---

## 3. Was im Hintergrund passiert (Ablauf & Logik)

### A. Login & Passkey-Automatisierung
* Das Tool nutzt ein eigenes persistentes Chrome-Profil (`chrome_moodle_profile/` im Skript-Ordner), damit deine Session-Cookies gespeichert bleiben.
* Wenn ein Login nötig ist, öffnet sich Chrome, klickt automatisch auf **„Login mit HM Benutzerkennung“**, füllt `moodle_user` und `moodle_pass` (sofern in `config.json` hinterlegt) aus und löst auf der 2FA-Seite den Button **„Bestätigung mittels Sicherheitsschlüssel oder Passkey“** aus.
* Du musst nur noch deine **Windows-PIN** eingeben (oder bei Abbruch den 6-stelligen Authenticator-Code eintippen). Danach übernimmt das Skript vollautomatisch.

### B. Datei-Download & Passwort-Entfernung
* Das Skript klappt eingeklappte Moodle-4-Abschnitte automatisch auf, berücksichtigt den Kursindex sowie verlinkte Unterabschnitte (`/course/section.php`, `&section=`), Ordner (`/mod/folder/`), Aufgaben (`/mod/assign/`) und Textseiten (`/mod/page/`).
* Über die direkte Session-Request-API werden auch *Inline-PDFs* und Pop-up-Ressourcen (`window.open`, `redirect=1`, `.resourceworkaround`) heruntergeladen.
* **Namenskollisionen:** Verwenden zwei verschiedene Kurs-Links denselben Dateinamen mit unterschiedlichem Inhalt (z. B. zweimal `folien.pdf`), vergibt das Skript automatisch eindeutige Namen (`folien_2.pdf`, `folien_3.pdf`, …).
* **Filter:** Web-Artefakte wie `favicon.ico`, Schriftarten, Moodle-Theme-Icons oder Profilbilder (`/user/icon/`) werden blockiert. Fachrelevante Code- und Datendateien (`.py`, `.js`, `.css`, `.lp`, `.xlsx`, `.txt`, `.md`, `.zip`) werden normal heruntergeladen.
* **Entschlüsselung:** Jede neue PDF wird gegen `pdf_passwords` aus `config.json` geprüft. Ist sie geschützt, wird automatisch eine unverschlüsselte Version gespeichert (da NotebookLM keine passwortgeschützten PDFs lesen kann).

### C. KI-Klassifizierung & Ordner-Logik
* Neue Dateien werden in 20er-Blöcken (Dateiname + Textauszug der ersten Seiten) an die **Google Gemini API** (`gemini-3.5-flash-lite` mit automatischem Fallback auf weitere Flash-Modelle) gesendet.
* **Relevanz-Filter:** Erkennt die KI bedeutungslose Web-Reste ohne Lerninhalt, werden diese automatisch gelöscht.
* **Ordner-Logik:**
  * **`Skript_oder_Vorlesung`** (Skripte, Vorlesungsfolien, Handouts & Orga) bleibt immer direkt im **Kurs-Root**.
  * **`Altklausuren`** werden bereits ab **1 Datei** in den Unterordner `Altklausuren/` verschoben.
  * **Alle anderen Kategorien** (z. B. `Uebungen`, `Praktika`, `Selbsttests`) bleiben im Root, solange nur 1 Datei vorliegt, und werden ab **2 Dateien** (oder wenn der Zielordner bereits existiert) automatisch in den entsprechenden Unterordner verschoben.

### D. Moodle-Tests (`MoodleTests/`)
* Gefundene Tests (`/mod/quiz/view.php`) werden im Unterordner `MoodleTests/` als Ganzseiten-Screenshots gesichert und alphanumerisch nummeriert (z. B. `01_Minitest_Lineare_Optimierung-1.png`, `01_Minitest_Lineare_Optimierung-2.png`).
* **Bereits abgeschlossene Tests:** Das Skript öffnet deine letzte Überprüfung (`review.php`) mit `&showall=1`, sodass alle Fragen samt Lösungen auf einem Screenshot landen.
* **Offene Selbsttests ohne Zeitlimit:** Das Skript klickt sich Seite für Seite durch den Versuch und fotografiert jede Frage ab, **ohne** den Versuch final abzugeben.
* **Gesperrte oder zeitbegrenzte Tests:** Werden sicher übersprungen (außer du nutzt `--quiz-critical`).

### E. Gruppierte Link-Liste (`_LINKS_FUER_NOTEBOOKLM.txt`)
In jedem Kursordner wird eine Textdatei angelegt, die alle gefundenen Links sauber trennt:
1. **Direkt in NotebookLM einfügen:** Echte externe Webseiten (ohne Aufzählungszeichen, sodass du den gesamten Block markieren und in einem Rutsch in NotebookLM unter *Website* einfügen kannst).
2. **Videokonferenz / BBB-Räume:** Separat aufgeführt, damit du sie nicht versehentlich in NotebookLM importierst.
3. **Moodle-Textseiten (`/mod/page/`):** Interne Seiten, die Login erfordern (zur Info).
4. **Moodle-Tests & Quizzes:** Übersicht aller Tests im Kurs.
5. **Foren & Sonstiges:** Ankündigungen und Diskussionsforen.

---

## 4. Versteckte Status-Dateien (nicht löschen)

In den Ordnern legt das Skript kleine JSON-Dateien an, um API-Kontingent und Zeit zu sparen:
* **`.sort_state.json`** (im jeweiligen Kursordner): Merkt sich bereits klassifizierte Dateien, Dateihashes und URL-Zuordnungen.
* **`.quiz_state.json`** (im Ordner `MoodleTests/`): Merkt sich bereits abfotografierte Tests und die fortlaufende Nummerierung (`01_`, `02_`, ...).
* **`chrome_moodle_profile/`** (im Skript-Ordner): Speichert deine Chrome-Session für Moodle.

---

## 5. Optimaler Upload-Workflow in NotebookLM Plus

1. Im Terminal `moodle-sync` ausführen.
2. Im Dateimanager den jeweiligen Fachordner öffnen und alle Dateien aus dem Root (sowie ggf. den Inhalt von `Uebungen/`, `Altklausuren/`, `MoodleTests/` etc.) per **Drag & Drop** ins NotebookLM-Quellenfenster ziehen.
3. Die Datei `_LINKS_FUER_NOTEBOOKLM.txt` öffnen, die URLs unter **Punkt 1** kopieren und in NotebookLM unter *Quelle hinzufügen -> Website* auf einmal einfügen.

---

## 6. Wartung zu Beginn eines neuen Semesters

In `config.json` (Vorlage: `config.example.json`) anpassen:
1. **`standard_kurse`:** Alte Kurse ersetzen/entfernen und die neuen Kurs-Ordner + URLs eintragen.
2. **`pdf_passwords`:** Falls Dozenten neue PDF-Passwörter für das Semester vergeben haben, einfach an die Liste anhängen.
