import os
import re
import json
import time
import shutil
import logging
import argparse
import html
import hashlib
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs, urljoin
from collections import Counter
import pikepdf
from pypdf import PdfReader
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
from pydantic import BaseModel

logging.getLogger("pypdf").setLevel(logging.ERROR)

# ==========================================
# KONFIGURATION
# ==========================================
# ==========================================
# KONFIGURATION (aus config.json laden)
# ==========================================
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"

if not CONFIG_FILE.exists():
    raise FileNotFoundError(
        f"Konfigurationsdatei fehlt: {CONFIG_FILE}\n"
        "Bitte kopiere 'config.example.json' zu 'config.json' und trage deine Daten ein."
    )

_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

os.environ["GEMINI_API_KEY"] = _cfg["gemini_api_key"]
DEFAULT_BASE_DIR = Path(_cfg["base_dir"])
PASSWORDS = _cfg.get("pdf_passwords", [])

MOODLE_USER = _cfg.get("moodle_user", "")
MOODLE_PASS = _cfg.get("moodle_pass", "")

STANDARD_KURSE = {
    DEFAULT_BASE_DIR / ordner: url
    for ordner, url in _cfg.get("standard_kurse", {}).items()
}

MODELLE = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

IGNORED_EXTENSIONS = {".ico", ".woff", ".woff2", ".ttf", ".eot"}
IGNORED_FILENAMES = {"favicon.ico", "apple-touch-icon.png", "robots.txt"}

CHROME_PROFILE_DIR = SCRIPT_DIR / "chrome_moodle_profile"
STATE_FILENAME = ".sort_state.json"

# Dateiendungen & Namen, die niemals heruntergeladen werden sollen
IGNORED_EXTENSIONS = {".ico", ".woff", ".woff2", ".ttf", ".eot"}
IGNORED_FILENAMES = {"favicon.ico", "apple-touch-icon.png", "robots.txt"}

SCRIPT_DIR = Path(__file__).resolve().parent
CHROME_PROFILE_DIR = SCRIPT_DIR / "chrome_moodle_profile"
STATE_FILENAME = ".sort_state.json"

# ==========================================
# KI-SCHEMA & CLIENT
# ==========================================
class DocumentItem(BaseModel):
    original_filename: str
    kategorie: str     # "Skript_oder_Vorlesung" ODER Gruppenname wie "Uebungen", "Altklausuren"
    ist_relevant: bool # False bei Icons, Logos, Platzhaltern oder Web-Artefakten ohne Lerninhalt

class BatchClassification(BaseModel):
    dokumente: list[DocumentItem]

http_opts = types.HttpOptions(timeout=25_000)
if hasattr(types, "HttpRetryOptions"):
    http_opts.retry_options = types.HttpRetryOptions(attempts=1)
client = genai.Client(http_options=http_opts)

# ==========================================
# HILFSFUNKTIONEN
# ==========================================
def sanitize_foldername(name: str) -> str:
    """Entfernt Professorennamen in Klammern und Leerzeichen (z.B. OperationsResearch)."""
    name = re.sub(r'\s*\([^()]*\)\s*$', '', name).strip()
    name = re.sub(r'[<>:"/\\|?*]', '', name).strip()
    parts = [p for p in re.split(r'\s+', name) if p]
    clean = "".join(p[0].upper() + p[1:] if len(p) > 1 else p.upper() for p in parts)
    return clean[:80] or "UnbekannterKurs"

def fix_mojibake(text: str) -> str:
    """Repariert UTF-8-Bytes, die fälschlicherweise als Latin-1 dekodiert wurden (z.B. Ã¼ -> ü)."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

def normalize_name(name: str) -> str:
    """Vereinheitlicht Unicode auf NFC (verhindert NFC/NFD-Duplikate unter Windows)."""
    return unicodedata.normalize("NFC", name)

def sanitize_filename(name: str) -> str:
    """Bereinigt Dateinamen für Windows und normalisiert Umlaute (NFC)."""
    name = normalize_name(fix_mojibake(name))
    name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    return name or "unbenannte_datei.pdf"

def extract_filename_from_response(resp, fallback_text: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    # 1. RFC 5987 inkl. optionalem Sprach-Tag (UTF-8''... oder UTF-8'de'...)
    m_star = re.search(r"filename\*\s*=\s*utf-8'[^']*'([^;\r\n]+)", cd, flags=re.IGNORECASE)
    if m_star:
        return sanitize_filename(unquote(m_star.group(1).strip().strip('"')))
    # 2. Klassisches filename="..." (inkl. Mojibake-Fix)
    m_std = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)', cd)
    if m_std:
        raw = m_std.group(1) or m_std.group(2)
        return sanitize_filename(unquote(raw.strip()))
    # 3. Finale URL nach Redirect
    url_name = unquote(Path(urlparse(resp.url).path).name)
    if url_name and url_name != "view.php":
        return sanitize_filename(url_name)
    # 4. Fallback aus Linktext
    clean_fallback = re.sub(r'\s+Datei$', '', fallback_text).strip()
    return sanitize_filename(f"{clean_fallback}.pdf" if clean_fallback else "download.pdf")

def is_junk_file(filename: str, content_type: str = "", url: str = "") -> bool:
    """Filtert nur Favicons und Moodle-UI-Theme-Icons, erlaubt aber .js/.css aus Kursen."""
    lower_name = filename.lower()
    lower_url = url.lower()
    if lower_name in IGNORED_FILENAMES:
        return True
    if "/theme/" in lower_url or "theme/image.php" in lower_url:
        return True
    if Path(lower_name).suffix in IGNORED_EXTENSIONS:
        return True
    if any(t in content_type.lower() for t in ("x-icon", "vnd.microsoft.icon")):
        return True
    return False

def decrypt_pdf_if_needed(file_path: Path):
    try:
        with pikepdf.open(file_path):
            return
    except pikepdf.PasswordError:
        for pw in PASSWORDS:
            try:
                temp_out = file_path.with_suffix(".unlocked.pdf")
                with pikepdf.open(file_path, password=pw) as pdf:
                    pdf.save(temp_out)
                shutil.move(temp_out, file_path)
                print(f"  [OK] Passwort entfernt: {file_path.name}")
                return
            except pikepdf.PasswordError:
                continue
        print(f"  [!] Kein passendes Passwort gefunden für: {file_path.name}")

def extract_preview(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".md", ".txt", ".py", ".csv", ".lp"}:
        try:
            return file_path.read_text(encoding="utf-8", errors="ignore")[:450].strip()
        except Exception:
            return ""
    if suffix != ".pdf":
        return f"(Dateityp: {suffix})"
    try:
        reader = PdfReader(file_path)
        text = "".join((page.extract_text() or "") for page in reader.pages[:2])
        return text[:450].strip()
    except Exception:
        return ""

def load_state(kurs_dir: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    state_file = kurs_dir / STATE_FILENAME
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            classified = {normalize_name(k): v for k, v in data.get("classified", {}).items()}
            hashes = {normalize_name(k): v for k, v in data.get("hashes", {}).items()}
            url_map = {k: normalize_name(v) for k, v in data.get("url_map", {}).items()}
            return classified, hashes, url_map
        except Exception:
            return {}, {}, {}
    return {}, {}, {}

def save_state(kurs_dir: Path, classified: dict[str, str], hashes: dict[str, str], url_map: dict[str, str] | None = None):
    state_file = kurs_dir / STATE_FILENAME
    existing_url_map = {}
    if url_map is None and state_file.exists():
        try:
            existing_url_map = json.loads(state_file.read_text(encoding="utf-8")).get("url_map", {})
        except Exception:
            pass
    state_file.write_text(
        json.dumps(
            {
                "classified": classified,
                "hashes": hashes,
                "url_map": url_map if url_map is not None else existing_url_map,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

def normalize_category(filename: str, ai_category: str) -> str:
    """Vereinheitlicht Kategorien deterministisch, damit Klausuren nie zersplittern."""
    combined = f"{filename} {ai_category}".lower()
    if any(k in combined for k in ("klausur", "pruef", "prüf", "exam")):
        return "Altklausuren"
    if any(k in combined for k in ("uebung", "übung", "tutorium", "loesung", "lösung")):
        return "Uebungen"
    if any(k in combined for k in ("praktikum", "praktika", "labor", "lab")):
        return "Praktika"
    clean = re.sub(r'[^A-Za-z0-9_-]', '', ai_category)
    return clean or "Skript_oder_Vorlesung"

def ask_gemini_chunk(file_summaries: list[dict]) -> dict[str, DocumentItem]:
    prompt = f"""
    Du sortierst Hochschul-Dateien für ein Fach.
    Analysiere die Dateinamen und Textauszüge:
    1. Setze `ist_relevant` auf `false`, wenn es offensichtlich nur ein Website-Icon, Logo oder bedeutungsloses Web-Artefakt ist (z.B. favicon.ico). Für alle Lernmaterialien, Code-Beispiele, Excel-Sheets, Skripte und Übungen setze `ist_relevant` auf `true`.
    2. Weise jeder Datei eine Kategorie zu:
       - Wenn es ein Skript, Vorlesungsfolien, Handout, Kapitel oder allgemeine Kurs-Orga (z.B. Rahmenbedingungen) ist, nutze EXAKT: "Skript_oder_Vorlesung"
       - Für alle Klausuren, Probeklausuren, Altklausuren oder Prüfungen nutze EXAKT: "Altklausuren"
       - Für Übungsblätter, Lösungen oder Tutorien nutze EXAKT: "Uebungen"
       - Für Praktika/Labs nutze EXAKT: "Praktika"
       - Für sonstige eigene Gruppen vergib einen kurzen ASCII-Namen ohne Umlaute/Leerzeichen (z. B. "Selbsttests", "Anleitungen").

    Dateien:
    {json.dumps(file_summaries, ensure_ascii=False)}
    """

    response = None
    for runde in range(2):
        for modell_name in MODELLE:
            try:
                response = client.models.generate_content(
                    model=modell_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=BatchClassification,
                        temperature=0.1,
                    ),
                )
                break
            except Exception as e:
                print(f"  [INFO] {modell_name} nicht verfügbar ({e.__class__.__name__}), wechsle...")
        if response is not None:
            break
        if runde == 0:
            print("  [INFO] Alle Modelle kurz ausgelastet, warte 3 Sekunden...")
            time.sleep(3)

    if response is None:
        raise RuntimeError("Alle konfigurierten Gemini-Modelle sind aktuell überlastet.")

    result = BatchClassification.model_validate_json(response.text)
    return {normalize_name(item.original_filename): item for item in result.dokumente}

def classify_and_sort_files(kurs_dir: Path, force: bool = False, file_hashes: dict[str, str] | None = None):
    root_files = [
        f for f in kurs_dir.iterdir()
        if f.is_file() and not f.name.startswith((".", "_"))
    ]

    classified, saved_hashes, _ = load_state(kurs_dir)
    if force:
        classified = {}
    if file_hashes is not None:
        saved_hashes.update(file_hashes)

    existing_names = {normalize_name(f.name) for f in kurs_dir.rglob("*") if f.is_file()}
    classified = {k: v for k, v in classified.items() if k in existing_names}
    saved_hashes = {k: v for k, v in saved_hashes.items() if k in existing_names}

    new_files = [f for f in root_files if normalize_name(f.name) not in classified]

    if not new_files:
        save_state(kurs_dir, classified, saved_hashes)
        print("  Bereits erledigt (keine neuen Dateien).")
        return

    print(f"  Klassifiziere {len(new_files)} neue Datei(en)...")
    for f in new_files:
        if f.suffix.lower() == ".pdf":
            decrypt_pdf_if_needed(f)

    CHUNK_SIZE = 20
    irrelevant_files = set()
    for i in range(0, len(new_files), CHUNK_SIZE):
        chunk = new_files[i : i + CHUNK_SIZE]
        summaries = [{"filename": normalize_name(f.name), "text_preview": extract_preview(f)} for f in chunk]
        chunk_mapping = ask_gemini_chunk(summaries)
        for f in chunk:
            norm_fname = normalize_name(f.name)
            item = chunk_mapping.get(norm_fname)
            if item and not item.ist_relevant:
                irrelevant_files.add(f)
            else:
                raw_kat = item.kategorie if item else "Skript_oder_Vorlesung"
                classified[norm_fname] = normalize_category(norm_fname, raw_kat)

    for f in irrelevant_files:
        print(f"  [DEL] Irrelevante Datei entfernt: {f.name}")
        f.unlink(missing_ok=True)
        root_files.remove(f)

    save_state(kurs_dir, classified, saved_hashes)

    root_categories = {
        normalize_name(f.name): classified.get(normalize_name(f.name), "Skript_oder_Vorlesung")
        for f in root_files
    }
    counts = Counter(root_categories.values())

    for f in root_files:
        norm_fname = normalize_name(f.name)
        kat = root_categories[norm_fname]
        existing_subfolder = kurs_dir / kat
        min_required = 1 if kat == "Altklausuren" else 2

        if kat != "Skript_oder_Vorlesung" and (counts[kat] >= min_required or existing_subfolder.exists()):
            target_dir = existing_subfolder
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f.name
            if f.resolve() != target_path.resolve():
                shutil.move(str(f), str(target_path))
                print(f"  -> {kat}/{f.name}")
        elif f in new_files:
            print(f"  -> {f.name} (bleibt im Root)")

def sort_existing_folder(folder_path: Path, force: bool = False):
    if not folder_path.exists():
        print(f"Ordner nicht gefunden: {folder_path}")
        return
    print(f"\n=== Prüfe Ordner: {folder_path.name} ===")
    classify_and_sort_files(folder_path, force=force)

def sort_existing_folder(folder_path: Path, force: bool = False):
    if not folder_path.exists():
        print(f"Ordner nicht gefunden: {folder_path}")
        return
    print(f"\n=== Prüfe Ordner: {folder_path.name} ===")
    classify_and_sort_files(folder_path, force=force)

def is_moodle_logged_in(context, moodle_domain: str) -> bool:
    for tab in context.pages:
        try:
            url = tab.url.lower()
            if moodle_domain in url and not any(k in url for k in ("/login", "shibboleth", "sso.hm.edu")):
                if tab.locator("a[href*='logout.php']").count() > 0:
                    return True
        except Exception:
            continue
    return False

def try_autofill_login(page):
    """Klickt automatisch auf 'Login mit HM Benutzerkennung' und füllt ggf. SSO aus."""
    try:
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(600)

        # 1. Direkt den blauen HM-Button auf moodle.hm.edu/local/login/ anklicken
        if "sso.hm.edu" not in page.url.lower():
            hm_btn = page.get_by_text("Login mit HM Benutzerkennung", exact=False).first
            if hm_btn.count() > 0:
                hm_btn.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(600)
            else:
                fallback_btn = page.locator(
                    "a[href*='shibboleth'], .login-identityprovider-btn, a[href*='login/index.php']"
                ).first
                if fallback_btn.count() > 0:
                    fallback_btn.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(600)

        # 2. Auf sso.hm.edu Benutzername + Passwort ausfüllen (falls konfiguriert)
        if MOODLE_USER and MOODLE_PASS and "HIER_DEIN" not in MOODLE_PASS:
            pw_input = page.locator("input[type='password']").first
            pw_input.wait_for(state="visible", timeout=6_000)
            user_input = page.locator(
                "input[name='j_username'], input[name='username'], input[type='text'], input[type='email']"
            ).first
            user_input.fill(MOODLE_USER)
            pw_input.fill(MOODLE_PASS)
            pw_input.press("Enter")

            # 3. Auf der 2FA-Seite automatisch den Passkey-Button auslösen
            passkey_btn = page.get_by_role(
                "button", name="Bestätigung mittels Sicherheitsschlüssel oder Passkey"
            ).first
            passkey_btn.wait_for(state="visible", timeout=6_000)
            passkey_btn.click()
            print("[INFO] Passkey-Abfrage gestartet (bei Abbruch einfach 6-stelligen Code eingeben)...")
    except Exception:
        pass

def ensure_logged_in(p, test_url: str, force_login: bool = False):
    moodle_domain = urlparse(test_url).netloc.lower()

    if CHROME_PROFILE_DIR.exists() and not force_login:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_PROFILE_DIR),
            channel="chrome",
            headless=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(test_url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if is_moodle_logged_in(context, moodle_domain):
            return context
        context.close()

    print("\n[LOGIN ERFORDERLICH] Öffne Chrome für Moodle-Login...")
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(CHROME_PROFILE_DIR),
        channel="chrome",
        headless=False,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(test_url, wait_until="domcontentloaded")

    try_autofill_login(page)

    deadline = time.time() + 180
    while time.time() < deadline:
        if is_moodle_logged_in(context, moodle_domain):
            break
        try:
            active_page = context.pages[0] if context.pages else page
            active_page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    else:
        context.close()
        raise TimeoutError("Login-Zeitüberschreitung (3 Minuten).")

    try:
        active_page = context.pages[0] if context.pages else page
        active_page.wait_for_timeout(1500)
    except Exception:
        time.sleep(1.5)

    print("[LOGIN ERFOLGREICH] Starte Download direkt in dieser Session...\n")
    return context

def parse_batch_file(file_path: Path) -> list[tuple[Path | None, str]]:
    if not file_path.exists():
        raise FileNotFoundError(f"Batch-Datei nicht gefunden: {file_path}")
    tasks = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            folder_part, url_part = line.split("|", 1)
            tasks.append((Path(folder_part.strip()), url_part.strip()))
        else:
            tasks.append((None, line))
    return tasks

def get_unique_filename(base_name: str, existing_map: dict[str, Path]) -> str:
    """Hängt _2, _3 usw. an, falls zwei verschiedene Moodle-Links denselben Dateinamen haben."""
    if base_name not in existing_map:
        return base_name
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in existing_map:
            return candidate
        counter += 1

def try_download_resource(
    context,
    href: str,
    link_text: str,
    target_folder: Path,
    existing_map: dict[str, Path],
    known_hashes: dict[str, str],
    url_map: dict[str, str],
    updated_files: list[str],
    referer: str = "",
) -> bool:
    try:
        clean_href = href.split("#")[0]
        url_basename = unquote(Path(urlparse(clean_href).path).name)
        if url_basename and is_junk_file(url_basename, url=clean_href):
            return True

        req_headers = {"Referer": referer} if referer else None
        resp = context.request.get(clean_href, headers=req_headers, timeout=25_000)
        if not resp.ok:
            return False

        ctype = resp.headers.get("content-type", "").lower()

        # HTML-Vorschauseiten & Pop-ups (inkl. window.open('.../pluginfile.php/...')) auflösen
        if "text/html" in ctype:
            page_html = resp.text()
            m = re.search(r'["\']([^"\']*?/pluginfile\.php/[^"\']+)["\']', page_html, flags=re.IGNORECASE)
            if not m:
                return False
            plugin_url = urljoin(resp.url, html.unescape(m.group(1)))
            resp = context.request.get(plugin_url, headers={"Referer": resp.url}, timeout=25_000)
            ctype = resp.headers.get("content-type", "").lower()
            if not resp.ok or "text/html" in ctype:
                return False

        raw_bytes = resp.body()
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Bekannter Link -> zugehörige Datei direkt prüfen/aktualisieren
        if clean_href in url_map and url_map[clean_href] in existing_map:
            fname = url_map[clean_href]
            old_path = existing_map[fname]
            if known_hashes.get(fname) == raw_hash and old_path.exists():
                return True
            old_path.write_bytes(raw_bytes)
            if old_path.suffix.lower() == ".pdf":
                decrypt_pdf_if_needed(old_path)
            known_hashes[fname] = raw_hash
            updated_files.append(fname)
            print(f"  [UPD] {fname} (aktualisiert)")
            return True

        fname = extract_filename_from_response(resp, link_text)
        if is_junk_file(fname, ctype, url=resp.url):
            return True

        if fname in existing_map:
            old_path = existing_map[fname]
            # Identischer Inhalt (z.B. alter manueller Download) -> nur verknüpfen
            if known_hashes.get(fname) == raw_hash and old_path.exists():
                url_map[clean_href] = fname
                return True

            # Gehört der Name schon einem anderen Moodle-Link -> eindeutigen Namen vergeben
            claimed_by_other = any(v == fname and k != clean_href for k, v in url_map.items())
            if claimed_by_other:
                fname = get_unique_filename(fname, existing_map)
            else:
                # Alte/manuelle Version ohne passenden Hash ersetzen
                old_path.write_bytes(raw_bytes)
                if old_path.suffix.lower() == ".pdf":
                    decrypt_pdf_if_needed(old_path)
                known_hashes[fname] = raw_hash
                url_map[clean_href] = fname
                updated_files.append(fname)
                print(f"  [UPD] {fname} (aktualisiert)")
                return True

        out_path = target_folder / fname
        out_path.write_bytes(raw_bytes)
        existing_map[fname] = out_path
        known_hashes[fname] = raw_hash
        url_map[clean_href] = fname
        updated_files.append(fname)
        print(f"  [DL] {fname}")
        return True
    except Exception:
        return False

def normalize_quiz_url(href: str) -> str:
    """Normalisiert Moodle-Quiz-URLs auf die reine Basis-URL mit ID."""
    parsed = urlparse(href)
    qid = parse_qs(parsed.query).get("id", [None])[0]
    if qid:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?id={qid}"
    return href.split("#")[0]

def clean_quiz_title(raw_text: str) -> str:
    """Bereinigt den Testnamen für Dateinamen (ohne doppeltes 'Test' am Ende)."""
    title = re.sub(r'\s+(Test|Quiz)$', '', raw_text.strip(), flags=re.IGNORECASE)
    title = re.sub(r'[<>:"/\\|?*]', '', title).strip()
    title = re.sub(r'\s+', '_', title)
    return title[:55] or "MoodleTest"

def capture_review_screenshots(quiz_page, review_url: str, tests_dir: Path, prefix: str) -> int:
    """Öffnet die Überprüfung (inkl. showall=1) und speichert Ganzseiten-Screenshots."""
    sep = "&" if "?" in review_url else "?"
    full_review_url = f"{review_url}{sep}showall=1" if "showall=" not in review_url else review_url

    quiz_page.goto(full_review_url, wait_until="domcontentloaded")
    quiz_page.wait_for_timeout(1000)

    count = 0
    while True:
        count += 1
        out_file = tests_dir / f"{prefix}-{count}.png"
        quiz_page.screenshot(path=str(out_file), full_page=True)

        next_link = quiz_page.locator("a:has-text('Nächste Seite'), a:has-text('Next page')").first
        if next_link.count() > 0 and "showall=1" not in quiz_page.url:
            next_link.click()
            quiz_page.wait_for_load_state("domcontentloaded")
            quiz_page.wait_for_timeout(800)
        else:
            break
    return count

def capture_attempt_screenshots(quiz_page, tests_dir: Path, prefix: str, allow_critical: bool) -> int:
    """Klickt sich durch einen offenen Versuch und fotografiert jede Seite (ohne final abzugeben)."""
    quiz_page.wait_for_timeout(800)

    # Bestätigungs-Modal abfangen (falls bei Zeitbegrenzung ein Popup erscheint)
    modal_confirm = quiz_page.locator(".modal-dialog button[data-action='save'], .modal-footer button.btn-primary").first
    if modal_confirm.count() > 0 and modal_confirm.is_visible():
        if not allow_critical:
            return 0
        modal_confirm.click()
        quiz_page.wait_for_load_state("domcontentloaded")
        quiz_page.wait_for_timeout(800)

    count = 0
    for _ in range(50):  # Sicherheitslimit gegen Endlosschleifen
        count += 1
        out_file = tests_dir / f"{prefix}-{count}.png"
        quiz_page.screenshot(path=str(out_file), full_page=True)

        next_btn = quiz_page.locator("#mod_quiz-next-nav").first
        if next_btn.count() == 0:
            break

        btn_label = ((next_btn.get_attribute("value") or "") + " " + (next_btn.inner_text() or "")).lower()
        # Auf der letzten Fragenseite steht "Versuch beenden..." -> hier stoppen, nicht abgeben!
        if "versuch beenden" in btn_label or "finish attempt" in btn_label:
            break

        next_btn.click()
        quiz_page.wait_for_load_state("domcontentloaded")
        quiz_page.wait_for_timeout(800)

    return count

def process_quizzes(context, target_folder: Path, quiz_links: list[tuple[str, str]], allow_critical: bool = False, force: bool = False):
    """Prüft Moodle-Tests, fotografiert Reviews oder sichere Versuche ab und nummeriert sie alphanumerisch."""
    if not quiz_links:
        return

    tests_dir = target_folder / "MoodleTests"
    state_file = tests_dir / ".quiz_state.json"

    state = {"next_index": 1, "done": {}}
    if state_file.exists() and not force:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    pending = [(title, url) for title, url in quiz_links if url not in state["done"]]
    if not pending:
        return

    print(f"  Pruefe {len(pending)} Moodle-Test(s)...")
    quiz_page = context.new_page()
    quiz_page.set_viewport_size({"width": 1280, "height": 900})

    try:
        for raw_title, quiz_url in pending:
            quiz_page.goto(quiz_url, wait_until="domcontentloaded")
            quiz_page.wait_for_timeout(1000)

            clean_name = clean_quiz_title(raw_title)
            idx = state["next_index"]
            prefix = f"{idx:02d}_{clean_name}"

            # 1. Fall: Bereits abgeschlossener Versuch -> Überprüfung (Review) sichern
            review_links = quiz_page.locator("a[href*='mod/quiz/review.php']").all()
            if review_links:
                tests_dir.mkdir(parents=True, exist_ok=True)
                review_href = review_links[-1].get_attribute("href")
                shots = capture_review_screenshots(quiz_page, review_href, tests_dir, prefix)
                print(f"  [TEST-REVIEW] {prefix}-1..{shots}.png ({shots} Screenshot(s))")
                state["done"][quiz_url] = prefix
                state["next_index"] = idx + 1
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                continue

            # 2. Fall: Kein Review -> Prüfen, ob ein Versuch gestartet/fortgesetzt werden kann
            start_btn = quiz_page.locator(
                "form[action*='startattempt.php'] button[type='submit'], "
                "button:has-text('Test jetzt durchführen'), "
                "button:has-text('Versuch fortsetzen'), "
                "button:has-text('Attempt quiz'), "
                "button:has-text('Continue your attempt')"
            ).first

            if start_btn.count() == 0:
                print(f"  [TEST-INFO] '{raw_title}' gesperrt oder kein Versuch moeglich.")
                continue

            # Prüfen, ob der Test kritisch ist (Countdown-Zeitbegrenzung oder begrenzte Versuche)
            region_text = quiz_page.locator("#region-main").inner_text().lower()
            is_critical = any(k in region_text for k in (
                "zeitbegrenzung", "time limit", "erlaubte versuche", "attempts allowed"
            ))

            if is_critical and not allow_critical:
                print(f"  [TEST-UEBERSPRUNGEN] '{raw_title}' hat Zeitlimit/Versuchslimit (nutze --quiz-critical-attempt zum Ausfuehren).")
                continue

            tests_dir.mkdir(parents=True, exist_ok=True)
            start_btn.click()
            quiz_page.wait_for_load_state("domcontentloaded")

            shots = capture_attempt_screenshots(quiz_page, tests_dir, prefix, allow_critical=allow_critical)
            if shots > 0:
                print(f"  [TEST-SCREENSHOTS] {prefix}-1..{shots}.png ({shots} Seite(n) gesichert)")
                state["done"][quiz_url] = prefix
                state["next_index"] = idx + 1
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        quiz_page.close()

def write_grouped_links(target_folder: Path, externe_links: set[str], interne_links: set[tuple[str, str]]):
    """Schreibt Links sauber gruppiert und ohne Bulletpoints (leicht kopierbar) in eine .txt-Datei."""
    notebooklm_urls = []
    bbb_urls = []
    textseiten = []
    tests = []
    foren_und_sonstiges = []

    for url in sorted(externe_links):
        if "vc-bbb." in url or "bigbluebutton" in url.lower():
            bbb_urls.append(url)
        else:
            notebooklm_urls.append(url)

    for title, url in sorted(interne_links, key=lambda x: x[0].lower()):
        entry = f"{title}\n  {url}"
        if "/mod/page/" in url:
            textseiten.append(entry)
        elif "/mod/quiz/" in url or "/mod/studentquiz/" in url:
            tests.append(entry)
        else:
            foren_und_sonstiges.append(entry)

    links_file = target_folder / "_LINKS_FUER_NOTEBOOKLM.txt"
    sections = [
        f"LINKS FUER {target_folder.name.upper()}",
        "=" * 55,
        "1. DIREKT IN NOTEBOOKLM EINFUEGEN (Website-Quellen - einfach Block kopieren):",
        "\n".join(notebooklm_urls) or "(Keine externen Website-Quellen vorhanden)",
    ]

    if bbb_urls:
        sections += ["", "-" * 55, "2. VIDEOKONFERENZ / BBB-RAEUME (nicht fuer NotebookLM):", "\n".join(bbb_urls)]
    if textseiten:
        sections += ["", "-" * 55, "3. MOODLE-TEXTSEITEN (Login noetig):", "\n\n".join(textseiten)]
    if tests:
        sections += ["", "-" * 55, "4. MOODLE-TESTS & QUIZZES (Login noetig):", "\n\n".join(tests)]
    if foren_und_sonstiges:
        sections += ["", "-" * 55, "5. FOREN & SONSTIGE MOODLE-LINKS:", "\n\n".join(foren_und_sonstiges)]

    links_file.write_text("\n".join(sections) + "\n", encoding="utf-8")

def expand_all_moodle_sections(page):
    """Klappt alle eingeklappten Moodle-4-Abschnitte auf und wartet auf AJAX-Nachladungen."""
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # 1. Globalen "Alle aufklappen" / "Expand all" Button nutzen, falls vorhanden
    try:
        expand_all = page.locator(
            "#region-main [data-action='expandall'], #region-main a.collapseall, #region-main button.collapseall"
        ).first
        if expand_all.count() > 0 and expand_all.is_visible():
            btn_text = (expand_all.inner_text() or "").lower()
            if any(w in btn_text for w in ("aufklappen", "expand", "alle öffnen")):
                expand_all.click()
                page.wait_for_timeout(1200)
    except Exception:
        pass

    # 2. Alle noch geschlossenen Abschnitte einzeln aufklappen
    for _ in range(3):
        toggles = page.locator(
            "#region-main [data-toggle='collapse'][aria-expanded='false'], "
            "#region-main [data-bs-toggle='collapse'][aria-expanded='false'], "
            "#region-main .icons-collapse-expand.collapsed"
        ).all()
        if not toggles:
            break
        for toggle in toggles:
            try:
                if toggle.is_visible():
                    toggle.click(timeout=2_000)
                    page.wait_for_timeout(250)
            except Exception:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        page.wait_for_timeout(800)

def process_course(
    context,
    page,
    target_folder: Path | None,
    kurs_url: str,
    moodle_domain: str,
    force: bool = False,
    allow_critical_quiz: bool = False,
):
    page.goto(kurs_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    if any(k in page.url.lower() for k in ("/login", "shibboleth", "sso.hm.edu")):
        raise RuntimeError("Nicht eingeloggt (Login-Seite erkannt). Bitte mit --login starten.")

    raw_title = page.locator("h1").first.inner_text() if page.locator("h1").count() > 0 else page.title()
    if "anmelden bei" in raw_title.lower():
        raise RuntimeError("Nicht eingeloggt (Anmeldeseite erkannt). Bitte mit --login starten.")

    if target_folder is None:
        clean_name = sanitize_foldername(raw_title)
        target_folder = DEFAULT_BASE_DIR / clean_name

    target_folder.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Kurs: {target_folder.name} ===")
    print(f"    Ziel: {target_folder}")

    classified_state, known_hashes, url_map = load_state(target_folder)
    existing_map = {
        normalize_name(f.name): f
        for f in target_folder.rglob("*")
        if f.is_file() and not f.name.startswith((".", "_")) and "MoodleTests" not in f.parts
    }
    url_map = {k: v for k, v in url_map.items() if v in existing_map}

    updated_files: list[str] = []
    externe_links = set()
    interne_links = set()
    quiz_links: list[tuple[str, str]] = []
    seen_quiz_urls = set()

    course_id = parse_qs(urlparse(kurs_url).query).get("id", [None])[0]
    pages_to_scan = [kurs_url]
    visited_pages = set()

    while pages_to_scan:
        current_url = pages_to_scan.pop(0)
        if current_url in visited_pages:
            continue
        visited_pages.add(current_url)

        if page.url != current_url:
            page.goto(current_url, wait_until="domcontentloaded")
            page.wait_for_timeout(800)

        is_sub_activity = any(p in current_url for p in ("/mod/assign/", "/mod/page/"))
        if not is_sub_activity:
            expand_all_moodle_sections(page)

        links = page.locator("#region-main a").all()

        for link in links:
            href = link.get_attribute("href")
            text = (link.inner_text() or "").strip().replace("\n", " ")
            if not href or href.startswith("#"):
                continue

            full_href = urljoin(current_url, href)
            parsed_href = urlparse(full_href)

            # 1. Unterabschnitte & Moodle-Verzeichnisse (Ordner) in die Crawl-Queue aufnehmen
            if not is_sub_activity and moodle_domain in parsed_href.netloc:
                is_section = "/course/section.php" in parsed_href.path or (
                    "/course/view.php" in parsed_href.path
                    and parse_qs(parsed_href.query).get("id", [None])[0] == course_id
                    and "section" in parse_qs(parsed_href.query)
                )
                is_folder = "/mod/folder/view.php" in parsed_href.path

                if is_section or is_folder:
                    clean_sub = full_href.split("#")[0]
                    if clean_sub not in visited_pages and clean_sub not in pages_to_scan:
                        pages_to_scan.append(clean_sub)
                    continue

            # 2. Dateien / Ressourcen herunterladen
            if any(p in parsed_href.path for p in ("/mod/resource/view.php", "/pluginfile.php/")) or parsed_href.path.lower().endswith(".pdf"):
                if not try_download_resource(
                    context,
                    full_href,
                    text,
                    target_folder,
                    existing_map,
                    known_hashes,
                    url_map,
                    updated_files,
                    referer=current_url,
                ):
                    interne_links.add((text, full_href))
                continue

            if is_sub_activity:
                continue

            # 3. Externe URL-Aktivität
            if "/mod/url/view.php" in parsed_href.path:
                try:
                    resp = context.request.get(full_href, timeout=10_000)
                    final_url = resp.url
                    if moodle_domain in urlparse(final_url).netloc:
                        m_ext = re.search(r'<div class="urlworkaround">.*?href="([^"]+)"', resp.text(), re.S)
                        if m_ext and moodle_domain not in urlparse(m_ext.group(1)).netloc:
                            externe_links.add(m_ext.group(1))
                        else:
                            interne_links.add((text, final_url))
                    else:
                        externe_links.add(final_url)
                except Exception:
                    interne_links.add((text, full_href))

            # 4. Moodle-Tests
            elif "/mod/quiz/view.php" in parsed_href.path:
                norm_quiz = normalize_quiz_url(full_href)
                if norm_quiz not in seen_quiz_urls:
                    seen_quiz_urls.add(norm_quiz)
                    quiz_links.append((text, norm_quiz))
                interne_links.add((text, norm_quiz))

            # 5. Aufgaben & Textseiten zum Scannen nach eingebetteten Anhängen vormerken + als Link sichern
            elif any(p in parsed_href.path for p in ("/mod/assign/view.php", "/mod/page/view.php")):
                clean_act = full_href.split("#")[0]
                if clean_act not in visited_pages and clean_act not in pages_to_scan:
                    pages_to_scan.append(clean_act)
                interne_links.add((text, clean_act))

            # 6. Sonstige direkte HTTP-Links
            elif full_href.startswith("http"):
                if moodle_domain not in parsed_href.netloc:
                    externe_links.add(full_href)
                elif "/course/view.php" not in parsed_href.path:
                    interne_links.add((text, full_href))

    for fname in updated_files:
        classified_state.pop(fname, None)
    save_state(target_folder, classified_state, known_hashes, url_map)

    if updated_files:
        print(f"  {len(updated_files)} neue/aktualisierte Datei(en) verarbeitet.")

    classify_and_sort_files(target_folder, force=force, file_hashes=known_hashes)
    process_quizzes(
        context,
        target_folder,
        quiz_links,
        allow_critical=allow_critical_quiz,
        force=force,
    )
    write_grouped_links(target_folder, externe_links, interne_links)

# ==========================================
# CLI ENTRYPOINT
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="Moodle Downloader & KI-Sortierer für NotebookLM")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-l", "--link", help="Einzelne Moodle-Kurs-URL verarbeiten")
    group.add_argument("-b", "--batch", help="Pfad zu einer .txt-Datei mit Kurs-URLs")
    group.add_argument("--sort", help="Bestehenden lokalen Kursordner einmalig entschlüsseln & sortieren")
    parser.add_argument("-o", "--output", help="Optionaler Zielordner (nur in Kombination mit -l)")
    parser.add_argument("--login", action="store_true", help="Erzwingt neuen Chrome-Login")
    parser.add_argument("--force", action="store_true", help="Ignoriert .sort_state.json und sortiert alles neu")
    parser.add_argument("--quiz-critical-attempt", action="store_true", help="Erlaubt auch das Starten von Tests mit Zeitlimit oder begrenzten Versuchen")
    args = parser.parse_args()

    if args.sort:
        sort_existing_folder(Path(args.sort), force=args.force)
        return

    tasks: list[tuple[Path | None, str]] = []
    if args.link:
        out_dir = Path(args.output) if args.output else None
        tasks.append((out_dir, args.link))
    elif args.batch:
        tasks = parse_batch_file(Path(args.batch))
    else:
        tasks = [(Path(folder), url) for folder, url in STANDARD_KURSE.items()]

    if not tasks:
        print("Keine Kurse zum Verarbeiten gefunden.")
        return

    first_url = tasks[0][1]
    moodle_domain = urlparse(first_url).netloc

    with sync_playwright() as p:
        context = ensure_logged_in(p, first_url, force_login=args.login)
        page = context.pages[0] if context.pages else context.new_page()

        for target_folder, kurs_url in tasks:
            process_course(
                context,
                page,
                target_folder,
                kurs_url,
                moodle_domain,
                force=args.force,
                allow_critical_quiz=args.quiz_critical_attempt,
            )

        context.close()
        print("\nAlle Aufgaben abgeschlossen!")

if __name__ == "__main__":
    main()