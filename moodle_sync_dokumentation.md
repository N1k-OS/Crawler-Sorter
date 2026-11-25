# Moodle Sync & KI-Sortierer für NotebookLM

Ein lokales CLI-Tool, das Moodle-Kurse automatisiert crawlt, Dateien (inkl. Inline-PDFs) herunterlädt, passwortgeschützte PDFs entschlüsselt, Moodle-Tests abfotografiert, Links gruppiert und alle Unterlagen mithilfe der Google Gemini API nach deinen Regeln für **NotebookLM** vorsortiert.

---

## 1. Schnellstart

Da `C:\Users\Super\Documents\Scripts\Crawler&Sorter` in deinen Windows-Umgebungsvariablen (`PATH`) hinterlegt ist, kannst du das Tool aus **jedem Terminal** (oder direkt über **Raycast**) starten:

```powershell
moodle-sync
```

---

## 2. Alle CLI-Optionen & Befehle

### Hauptmodi (nicht kombinierbar)

| Befehl | Beschreibung |
| :--- | :--- |
| `moodle-sync` | **Standard-Modus:** Arbeitet nacheinander alle Kurse aus `STANDARD_KURSE` in `moodle_sync.py` ab. |
| `moodle-sync -l "URL"` | **Einzelkurs-Modus:** Verarbeitet genau diesen einen Moodle-Kurs. Der Zielordner wird automatisch aus dem Kurstitel (ohne Professoren-Klammern und ohne Leerzeichen) in deinem `PDFs`-Ordner erstellt. |
| `moodle-sync -l "URL" -o "PFAD"` | **Einzelkurs mit Zielordner:** Wie `-l`, speichert aber alles in den von dir angegebenen Ordnerpfad. |
| `moodle-sync -b "datei.txt"` | **Batch-Datei-Modus:** Liest eine Textdatei Zeile für Zeile ein und arbeitet alle enthaltenen Kurse ab (Format: reine `URL` oder `Zielordner \| URL`, Zeilen mit `#` werden ignoriert). |
| `moodle-sync --sort "ORDNERPFAD"` | **Offline-Sortierer:** Entschlüsselt und sortiert bereits lokal vorhandene Dateien im Root eines bestehenden Fachordners (ohne Moodle-Login). |

### Zusatz-Flags (mit allen Modi kombinierbar)

| Flag | Beschreibung |
| :--- | :--- |
| `--login` | Erzwingt das Öffnen des sichtbaren Chrome-Fensters für einen frischen Login (nützlich, falls die Session abgelaufen ist). |
| `--force` | Ignoriert die gespeicherten Status-Dateien (`.sort_state.json` und `.quiz_state.json`), klassifiziert alle Dateien im Root neu und prüft alle Tests erneut. |
| `--quiz-critical-attempt` | Erlaubt dem Skript, auch offene Moodle-Tests mit **Zeitbegrenzung** oder **begrenzten Versuchen** zu starten und abzufotografieren (standardmäßig aus Sicherheitsgründen deaktiviert). |
| `-h` / `--help` | Zeigt eine Kurzübersicht aller Parameter im Terminal an. |

**Praxis-Beispiele:**
```powershell
# Alle Semesterkurse aktualisieren
moodle-sync

# Spontan einen Kurs herunterladen, aber kritische Tests sicher überspringen
moodle-sync -l "https://moodle.hm.edu/course/view.php?id=95"

# Einen bereits vorhandenen lokalen Ordner aufräumen
moodle-sync --sort "C:\Users\Super\Documents\Studium\Hochschule\PDFs\IT-Systeme"
```

---

## 3. Was im Hintergrund passiert (Ablauf & Logik)

### A. Login & Passkey-Automatisierung
* Das Tool nutzt ein eigenes persistentes Chrome-Profil (`chrome_moodle_profile` im Skript-Ordner), damit deine Session-Cookies gespeichert bleiben.
* Wenn ein Login nötig ist, öffnet sich Chrome, klickt automatisch auf **„Login mit HM Benutzerkennung“**, füllt Benutzername und Passwort aus und löst auf der 2FA-Seite den Button **„Bestätigung mittels Sicherheitsschlüssel oder Passkey“** aus.
* Du musst nur noch deine **Windows-PIN** eingeben (oder bei Abbruch den 6-stelligen Authenticator-Code eintippen). Danach übernimmt das Skript vollautomatisch.

### B. Datei-Download & Passwort-Entfernung
* Das Skript scannt die Kurs-Hauptseite sowie alle verlinkten Unterabschnitte (`/course/section.php`).
* Über die direkte Session-Request-API werden auch PDFs heruntergeladen, die Moodle normalerweise nur im Browser anzeigt (*Inline-PDFs*).
* **Filter:** Web-Artefakte wie `favicon.ico`, Schriftarten oder Moodle-Theme-Icons werden blockiert. Fachrelevante Code- und Datendateien (`.py`, `.js`, `.css`, `.lp`, `.xlsx`, `.txt`, `.md`) werden normal heruntergeladen.
* **Entschlüsselung:** Jede neue PDF wird gegen deine Liste `PASSWORDS` geprüft. Ist sie geschützt, wird automatisch eine unverschlüsselte Version gespeichert (da NotebookLM keine passwortgeschützten PDFs lesen kann).

### C. KI-Klassifizierung & 3-Dateien-Regel
* Neue Dateien werden in 20er-Blöcken (Dateiname + Textauszug der ersten Seiten) an die kostenlose **Google Gemini API** (`gemini-3.5-flash-lite` mit automatischem Fallback auf weitere Flash-Modelle) gesendet.
* **Relevanz-Filter:** Erkennt die KI bedeutungslose Web-Reste ohne Lerninhalt, werden diese automatisch gelöscht.
* **Ordner-Logik:**
  * **Skripte, Vorlesungsfolien, Handouts & Orga** bleiben immer direkt im **Kurs-Root**.
  * **Alle anderen Kategorien** (z. B. `Uebungen`, `Praktika`, `Altklausuren`, `Selbsttests`) bleiben im Root, solange es nur 1 oder 2 Dateien sind. Sobald **mindestens 3 Dateien** derselben Kategorie vorliegen, erstellt das Skript automatisch den entsprechenden Unterordner und verschiebt sie hinein.

### D. Moodle-Tests (`MoodleTests/`)
* Gefundene Tests (`/mod/quiz/view.php`) werden im Unterordner `MoodleTests/` als Ganzseiten-Screenshots gesichert und alphanumerisch nummeriert (z. B. `01_Minitest_Lineare_Optimierung-1.png`, `01_Minitest_Lineare_Optimierung-2.png`).
* **Bereits abgeschlossene Tests:** Das Skript öffnet deine letzte Überprüfung (`review.php`) mit `&showall=1`, sodass alle Fragen samt Lösungen auf einem Screenshot landen.
* **Offene Selbsttests ohne Zeitlimit:** Das Skript klickt sich Seite für Seite durch den Versuch und fotografiert jede Frage ab, **ohne** den Versuch final abzugeben.
* **Gesperrte oder zeitbegrenzte Tests:** Werden sicher übersprungen (außer du nutzt `--quiz-critical-attempt`).

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
* **`.sort_state.json`** (im jeweiligen Kursordner): Merkt sich, welche Dateien bereits von Gemini klassifiziert wurden. Beim nächsten Lauf werden sie in 0 Sekunden übersprungen.
* **`.quiz_state.json`** (im Ordner `MoodleTests/`): Merkt sich bereits abfotografierte Tests und die fortlaufende Nummerierung (`01_`, `02_`, ...).
* **`chrome_moodle_profile/`** (im Skript-Ordner): Speichert deine Chrome-Session für Moodle.

---

## 5. Optimaler Upload-Workflow in NotebookLM Plus

1. Im Terminal `moodle-sync` ausführen.
2. Im Windows Explorer den jeweiligen Fachordner öffnen und alle Dateien aus dem Root (sowie ggf. den Inhalt von `Uebungen/`, `MoodleTests/` etc.) per **Drag & Drop** ins NotebookLM-Quellenfenster ziehen.
3. Die Datei `_LINKS_FUER_NOTEBOOKLM.txt` öffnen, die URLs unter **Punkt 1** kopieren und in NotebookLM unter *Quelle hinzufügen -> Website* auf einmal einfügen.

---

## 6. Wartung zu Beginn eines neuen Semesters

In `moodle_sync.py` im Bereich `KONFIGURATION` anpassen:
1. **`STANDARD_KURSE`:** Alte Kurse auskommentieren/ersetzen und die neuen Kurs-IDs eintragen.
2. **`PASSWORDS`:** Falls Dozenten neue PDF-Passwörter für das Semester vergeben haben, einfach an die Liste anhängen.