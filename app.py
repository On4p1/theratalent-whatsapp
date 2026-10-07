import os, io, csv, hmac, hashlib, asyncio, sqlite3, secrets, time, mimetypes
from zoneinfo import ZoneInfo
from contextlib import closing
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------- Konfiguration
APP_PASSWORT    = os.getenv("APP_PASSWORT", "aendern-bitte")
META_TOKEN      = os.getenv("META_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "theratalent-webhook")
DB_PFAD         = os.getenv("DB_PFAD", "/data/setter.db")
GRAPH           = "https://graph.facebook.com/v23.0"
MEDIEN_PFAD     = os.path.join(os.path.dirname(DB_PFAD), "medien")
ZEITZONE        = ZoneInfo(os.getenv("ZEITZONE", "Europe/Berlin"))

TEMPLATES = {
    "interesse":   {"name": "reaktivierung_interesse",   "label": "Hatte Interesse"},
    "abgebrochen": {"name": "reaktivierung_abgebrochen", "label": "Abgesprungen"},
}

FOLLOWUPS = {
    "tt_antwort_verzoegert": {
        "label": "Verspätete Antwort",
        "text": "Hallo,\n\ndanke für deine Nachricht und deine Geduld. Ich komme erst jetzt "
                "dazu, dir zu antworten.\n\nIst dein Anliegen noch aktuell?\n\n"
                "Viele Grüße\nMatthias von TheraTalent"},
    "tt_interesse_termin": {
        "label": "Interesse, Termin vereinbaren",
        "text": "Hallo,\n\ndanke für deine Rückmeldung. Gerne besprechen wir, wie wir euch bei "
                "der Personalsuche unterstützen können.\n\nHier kannst du direkt einen passenden "
                "Termin auswählen:\nhttps://theratalent.de/termin-buchen\n\nFalls nichts Passendes "
                "dabei ist, schick mir gerne zwei Zeitfenster, zu denen du gut erreichbar bist.\n\n"
                "Viele Grüße\nMatthias von TheraTalent"},
    "tt_preis_antwort": {
        "label": "Frage nach dem Preis",
        "text": "Hallo,\n\nwo wir preislich am Ende liegen, hängt insbesondere davon ab, wie viele "
                "Therapeuten ihr sucht und welche fachlichen, zeitlichen und persönlichen "
                "Voraussetzungen diese erfüllen sollen.\n\nDas kann ich dir in einem kurzen, "
                "unverbindlichen Gespräch konkret sagen. Hier kannst du direkt einen passenden "
                "Termin auswählen:\nhttps://theratalent.de/termin-buchen\n\n"
                "Viele Grüße\nMatthias von TheraTalent"},
    "tt_followup": {
        "label": "Allgemeines Nachfassen",
        "text": "Hallo,\n\nich wollte kurz nachfragen: Ist eure Personalsuche noch aktuell oder "
                "hat sich das inzwischen erledigt?\n\nFalls ihr noch Unterstützung sucht, können "
                "wir uns gerne unverbindlich austauschen:\nhttps://theratalent.de/termin-buchen\n\n"
                "Eine kurze Rückmeldung reicht mir.\n\nViele Grüße\nMatthias von TheraTalent"},
    "tt_followup_1": {
        "label": "Nachfassen kurz",
        "text": "Hallo,\n\nist eure Personalsuche noch aktuell? Gib mir gerne kurz Bescheid.\n\n"
                "Viele Grüße\nMatthias"},
    "tt_followup_2": {
        "label": "Nachfassen letztes Mal",
        "text": "Hallo,\n\nich wollte ein letztes Mal nachfragen: Sucht ihr noch Verstärkung oder "
                "hat sich das inzwischen erledigt? Eine kurze Rückmeldung reicht mir.\n\n"
                "Viele Grüße\nMatthias"},
    "tt_kein_bedarf": {
        "label": "Kein Bedarf, Stelle besetzt",
        "text": "Hallo,\n\ndanke für deine Rückmeldung. Dann weiß ich Bescheid und wünsche euch "
                "weiterhin alles Gute mit eurem Team.\n\nFalls ihr später wieder Unterstützung bei "
                "der Personalsuche benötigt, melde dich gerne bei mir.\n\n"
                "Viele Grüße\nMatthias von TheraTalent"},
}

VORLAGE_CSV = (
    "telefon,vorname,nachname,unternehmen,kategorie\r\n"
    "0151 23456789,Anna,Beispiel,Physiopraxis Beispiel,interesse\r\n"
    "+4917612345678,Max,Muster,Ergo Muster,abgebrochen\r\n"
    "0049160 1234567,Sarah,Testfrau,,interesse\r\n"
)

app = FastAPI()
tpl = Jinja2Templates(directory="templates")

def zeit_lokal(iso, muster="%H:%M Uhr · %d.%m."):
    """UTC aus der Datenbank in deutsche Ortszeit."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(ZEITZONE).strftime(muster)
    except ValueError:
        return iso

tpl.env.filters["zeit"] = zeit_lokal

# ---------------------------------------------------------------- Datenbank
def db():
    os.makedirs(os.path.dirname(DB_PFAD), exist_ok=True)
    c = sqlite3.connect(DB_PFAD)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefon TEXT UNIQUE NOT NULL,
            vorname TEXT, nachname TEXT, unternehmen TEXT,
            kategorie TEXT DEFAULT 'interesse',
            status TEXT DEFAULT 'neu',
            auto_antwort INTEGER DEFAULT 0,
            fehler TEXT, angelegt TEXT, gesendet TEXT
        );
        CREATE TABLE IF NOT EXISTS nachrichten (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL, richtung TEXT NOT NULL,
            text TEXT, zeit TEXT, wamid TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_n_lead ON nachrichten(lead_id);
        """)
        vorhanden = {r["name"] for r in c.execute("PRAGMA table_info(leads)")}
        if "zustellung" not in vorhanden:
            c.execute("ALTER TABLE leads ADD COLUMN zustellung TEXT")
        if "zustellung_zeit" not in vorhanden:
            c.execute("ALTER TABLE leads ADD COLUMN zustellung_zeit TEXT")
        if "termin" not in vorhanden:
            c.execute("ALTER TABLE leads ADD COLUMN termin INTEGER DEFAULT 0")
        spalten_n = {r["name"] for r in c.execute("PRAGMA table_info(nachrichten)")}
        if "wamid" not in spalten_n:
            c.execute("ALTER TABLE nachrichten ADD COLUMN wamid TEXT")
        if "medien_datei" not in spalten_n:
            c.execute("ALTER TABLE nachrichten ADD COLUMN medien_datei TEXT")
        if "medien_typ" not in spalten_n:
            c.execute("ALTER TABLE nachrichten ADD COLUMN medien_typ TEXT")
        if "zustellung" not in spalten_n:
            c.execute("ALTER TABLE nachrichten ADD COLUMN zustellung TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS einstellungen (name TEXT PRIMARY KEY, wert TEXT)")
        c.commit()

init_db()
os.makedirs(MEDIEN_PFAD, exist_ok=True)

def geheimnis():
    """Signaturschluessel, liegt in der Datenbank und ueberlebt Neustarts."""
    with closing(db()) as c:
        r = c.execute("SELECT wert FROM einstellungen WHERE name='cookie_secret'").fetchone()
        if r:
            return r["wert"]
        neu = secrets.token_urlsafe(48)
        c.execute("INSERT INTO einstellungen (name,wert) VALUES ('cookie_secret',?)", (neu,))
        c.commit()
        return neu

GEHEIMNIS = geheimnis()

def sitzung_erzeugen():
    nutzlast = str(int(time.time()))
    sig = hmac.new(GEHEIMNIS.encode(), nutzlast.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{nutzlast}.{sig}"

def sitzung_gueltig(wert):
    if not wert or "." not in wert:
        return False
    nutzlast, sig = wert.rsplit(".", 1)
    erwartet = hmac.new(GEHEIMNIS.encode(), nutzlast.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, erwartet):
        return False
    try:
        return int(time.time()) - int(nutzlast) < 60 * 60 * 24 * 30
    except ValueError:
        return False

def jetzt():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def nummer_normalisieren(roh):
    s = "".join(ch for ch in str(roh) if ch.isdigit() or ch == "+")
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("00"):
        s = s[2:]
    elif s.startswith("0"):
        s = "49" + s[1:]
    return s

# ---------------------------------------------------------------- Login
def angemeldet(request: Request):
    return sitzung_gueltig(request.cookies.get("sid"))

def zum_login():
    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, fehler: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"fehler": fehler})

@app.post("/login")
def login(passwort: str = Form(...)):
    if not secrets.compare_digest(passwort, APP_PASSWORT):
        return RedirectResponse("/login?fehler=1", status_code=303)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("sid", sitzung_erzeugen(), httponly=True, samesite="lax",
                 max_age=60 * 60 * 24 * 30)
    return r

@app.get("/logout")
def logout(request: Request):
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("sid")
    return r

# ---------------------------------------------------------------- WhatsApp
async def whatsapp_senden(payload):
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return False, "META_TOKEN oder PHONE_NUMBER_ID fehlt", None
    kopf = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=kopf, json=payload)
    if r.status_code >= 400:
        try:
            d = r.json()["error"]
            return False, f"{d.get('code')}: {d.get('message')}", None
        except Exception:
            return False, r.text[:200], None
    try:
        wamid = r.json()["messages"][0]["id"]
    except Exception:
        wamid = None
    return True, "", wamid

async def template_senden(telefon, template_name, vorname):
    return await whatsapp_senden({
        "messaging_product": "whatsapp", "to": telefon, "type": "template",
        "template": {
            "name": template_name, "language": {"code": "de"},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "parameter_name": "name", "text": vorname or "zusammen"}]}],
        },
    })

async def followup_senden(telefon, template_name):
    """Follow-up-Vorlagen haben keine Platzhalter, also auch keine Parameter."""
    return await whatsapp_senden({
        "messaging_product": "whatsapp", "to": telefon, "type": "template",
        "template": {"name": template_name, "language": {"code": "de"}},
    })

async def text_senden(telefon, text):
    return await whatsapp_senden({
        "messaging_product": "whatsapp", "to": telefon, "type": "text",
        "text": {"preview_url": False, "body": text},
    })

async def medien_holen(media_id, mime):
    """Laedt eine Datei von Meta herunter und legt sie auf dem Server ab.
    Meta loescht Medien nach 30 Tagen, deshalb muss die Datei bei uns bleiben."""
    if not META_TOKEN or not media_id:
        return None
    kopf = {"Authorization": f"Bearer {META_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(f"{GRAPH}/{media_id}", headers=kopf)
            if r.status_code >= 400:
                return None
            adresse = r.json().get("url")
            if not adresse:
                return None
            d = await client.get(adresse, headers=kopf)
            if d.status_code >= 400:
                return None
            inhalt = d.content
    except Exception as e:
        print("Medien-Download fehlgeschlagen:", e, flush=True)
        return None
    endung = mimetypes.guess_extension((mime or "").split(";")[0]) or ".bin"
    if endung == ".oga":
        endung = ".ogg"
    name = f"{media_id}{endung}"
    os.makedirs(MEDIEN_PFAD, exist_ok=True)
    with open(os.path.join(MEDIEN_PFAD, name), "wb") as f:
        f.write(inhalt)
    return name

@app.get("/medien/{dateiname}")
def medien_ausliefern(request: Request, dateiname: str, download: int = 0):
    if not angemeldet(request):
        return zum_login()
    sicher = os.path.basename(dateiname)
    pfad = os.path.join(MEDIEN_PFAD, sicher)
    if not os.path.isfile(pfad):
        return PlainTextResponse("Datei nicht gefunden", status_code=404)
    typ = mimetypes.guess_type(sicher)[0] or "application/octet-stream"
    kopf = {}
    if download:
        kopf["Content-Disposition"] = f'attachment; filename="{sicher}"'
    with open(pfad, "rb") as f:
        return Response(f.read(), media_type=typ, headers=kopf)

# ---------------------------------------------------------------- Leads
@app.get("/", response_class=HTMLResponse)
def leads_seite(request: Request, meldung: str = ""):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        leads = c.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
        z = c.execute("""
            SELECT COUNT(*) gesamt,
                   SUM(status='neu') offen,
                   SUM(status IN ('angeschrieben','geantwortet')) raus,
                   SUM(status='geantwortet') geantwortet,
                   SUM(status='fehler') fehler,
                   SUM(zustellung IN ('zugestellt','gelesen')) zugestellt,
                   SUM(zustellung='gelesen') gelesen,
                   SUM(termin=1) termine
            FROM leads""").fetchone()
    zahlen = {k: (z[k] or 0) for k in
              ["gesamt", "offen", "raus", "geantwortet", "fehler", "zugestellt",
               "gelesen", "termine"]}
    def anteil(oben, unten):
        return round(oben * 100 / unten) if unten else 0
    zahlen["quote"] = anteil(zahlen["geantwortet"], zahlen["raus"])
    zahlen["quote_zugestellt"] = anteil(zahlen["zugestellt"], zahlen["raus"])
    zahlen["quote_termin"] = anteil(zahlen["termine"], zahlen["raus"])
    zahlen["quote_termin_antwort"] = anteil(zahlen["termine"], zahlen["geantwortet"])
    return tpl.TemplateResponse(request, "leads.html", {
        "leads": leads, "meldung": meldung, "templates": TEMPLATES, "zahlen": zahlen,
        "versand": VERSAND_LAEUFT})

@app.get("/vorlage.csv")
def vorlage(request: Request):
    if not angemeldet(request):
        return zum_login()
    return Response(VORLAGE_CSV.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="leads-vorlage.csv"'})

@app.post("/leads/csv")
async def leads_csv(request: Request, datei: UploadFile = File(...)):
    if not angemeldet(request):
        return zum_login()
    roh = (await datei.read()).decode("utf-8-sig", errors="replace")
    trenner = ";" if roh.count(";") > roh.count(",") else ","
    neu = uebersprungen = 0
    with closing(db()) as c:
        for zeile in csv.DictReader(io.StringIO(roh), delimiter=trenner):
            z = {(k or "").strip().lower(): (v or "").strip() for k, v in zeile.items()}
            tel = nummer_normalisieren(z.get("telefon") or z.get("nummer") or z.get("handy") or "")
            if len(tel) < 8:
                uebersprungen += 1
                continue
            kat = (z.get("kategorie") or z.get("status") or "interesse").lower()
            if kat not in TEMPLATES:
                kat = "interesse"
            try:
                c.execute("INSERT INTO leads (telefon,vorname,nachname,unternehmen,kategorie,angelegt)"
                          " VALUES (?,?,?,?,?,?)",
                          (tel, z.get("vorname", ""), z.get("nachname", ""),
                           z.get("unternehmen", ""), kat, jetzt()))
                neu += 1
            except sqlite3.IntegrityError:
                uebersprungen += 1
        c.commit()
    m = f"{neu} Kontakte importiert"
    if uebersprungen:
        m += f", {uebersprungen} übersprungen (doppelt oder ohne Nummer)"
    return RedirectResponse(f"/?meldung={m}", status_code=303)

@app.post("/leads/neu")
async def lead_neu(request: Request, telefon: str = Form(...), vorname: str = Form(""),
                   nachname: str = Form(""), unternehmen: str = Form(""),
                   kategorie: str = Form("interesse")):
    if not angemeldet(request):
        return zum_login()
    tel = nummer_normalisieren(telefon)
    if len(tel) < 8:
        return RedirectResponse("/?meldung=Nummer unvollständig", status_code=303)
    with closing(db()) as c:
        try:
            c.execute("INSERT INTO leads (telefon,vorname,nachname,unternehmen,kategorie,angelegt)"
                      " VALUES (?,?,?,?,?,?)",
                      (tel, vorname, nachname, unternehmen,
                       kategorie if kategorie in TEMPLATES else "interesse", jetzt()))
            c.commit()
        except sqlite3.IntegrityError:
            return RedirectResponse("/?meldung=Nummer schon vorhanden", status_code=303)
    return RedirectResponse("/?meldung=Kontakt angelegt", status_code=303)

@app.post("/leads/{lead_id}/kategorie")
async def kategorie_setzen(request: Request, lead_id: int, kategorie: str = Form(...)):
    if not angemeldet(request):
        return zum_login()
    if kategorie in TEMPLATES:
        with closing(db()) as c:
            c.execute("UPDATE leads SET kategorie=? WHERE id=?", (kategorie, lead_id))
            c.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/leads/{lead_id}/loeschen")
async def lead_loeschen(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        c.execute("DELETE FROM nachrichten WHERE lead_id=?", (lead_id,))
        c.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        c.commit()
    return RedirectResponse("/?meldung=Kontakt entfernt", status_code=303)

@app.post("/leads/{lead_id}/zuruecksetzen")
async def lead_zuruecksetzen(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        c.execute("UPDATE leads SET status='neu', fehler=NULL, zustellung=NULL,"
                  " zustellung_zeit=NULL, gesendet=NULL WHERE id=?", (lead_id,))
        c.commit()
    return RedirectResponse("/?meldung=Kontakt zurückgesetzt, kann erneut angeschrieben werden",
                            status_code=303)

async def einen_anschreiben(lead_id):
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or lead["status"] not in ("neu", "fehler"):
            return False
    t = TEMPLATES[lead["kategorie"]]["name"]
    ok, fehler, wamid = await template_senden(lead["telefon"], t, lead["vorname"])
    with closing(db()) as c:
        if ok:
            c.execute("UPDATE leads SET status='angeschrieben', gesendet=?, fehler=NULL,"
                      " zustellung='angenommen', zustellung_zeit=? WHERE id=?",
                      (jetzt(), jetzt(), lead_id))
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit,wamid)"
                      " VALUES (?,?,?,?,?)",
                      (lead_id, "raus", f"[Erstnachricht: {TEMPLATES[lead['kategorie']]['label']}]",
                       jetzt(), wamid))
        else:
            c.execute("UPDATE leads SET status='fehler', fehler=? WHERE id=?", (fehler, lead_id))
        c.commit()
    return ok

@app.post("/leads/{lead_id}/senden")
async def lead_senden(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    ok = await einen_anschreiben(lead_id)
    return RedirectResponse(
        "/?meldung=" + ("Nachricht an WhatsApp übergeben, Zustellstatus erscheint gleich in der Zeile"
                        if ok else "Versand fehlgeschlagen, Grund steht in der Zeile"),
        status_code=303)

VERSAND_LAEUFT = {"aktiv": False, "offen": 0, "fertig": 0}

async def massenversand(ids):
    """Laeuft im Hintergrund, damit die Seite nicht haengt."""
    try:
        for lid in ids:
            await einen_anschreiben(lid)
            VERSAND_LAEUFT["fertig"] += 1
            await asyncio.sleep(1)
    finally:
        VERSAND_LAEUFT["aktiv"] = False

@app.post("/leads/alle-senden")
async def alle_senden(request: Request):
    if not angemeldet(request):
        return zum_login()
    if VERSAND_LAEUFT["aktiv"]:
        return RedirectResponse("/?meldung=Versand läuft noch, bitte warten", status_code=303)
    with closing(db()) as c:
        offen = [r["id"] for r in
                 c.execute("SELECT id FROM leads WHERE status IN ('neu','fehler')").fetchall()]
    if not offen:
        return RedirectResponse("/?meldung=Keine offenen Kontakte", status_code=303)
    VERSAND_LAEUFT.update({"aktiv": True, "offen": len(offen), "fertig": 0})
    asyncio.create_task(massenversand(offen))
    return RedirectResponse(
        f"/?meldung={len(offen)} Kontakte werden angeschrieben. Läuft im Hintergrund, "
        "Seite neu laden zeigt den Fortschritt.", status_code=303)

# ---------------------------------------------------------------- Chats
@app.get("/chats", response_class=HTMLResponse)
def chats(request: Request, filter: str = "alle"):
    if not angemeldet(request):
        return zum_login()
    bedingung = {"geantwortet": " AND l.status='geantwortet'",
                 "offen": " AND l.status='angeschrieben'",
                 "gelesen": " AND l.status='angeschrieben' AND l.zustellung='gelesen'",
                 "termin": " AND l.termin=1",
                 "fehler": " AND l.status='fehler'"}.get(filter, "")
    with closing(db()) as c:
        zeilen = c.execute(f"""
            SELECT l.*,
              (SELECT text FROM nachrichten n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1) letzte,
              (SELECT zeit FROM nachrichten n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1) letzte_zeit,
              (SELECT COUNT(*) FROM nachrichten n WHERE n.lead_id=l.id AND n.richtung='rein') antworten
            FROM leads l WHERE l.status!='neu'{bedingung}
            ORDER BY letzte_zeit DESC""").fetchall()
        z = c.execute("""SELECT SUM(status!='neu') alle,
                                SUM(status='geantwortet') geantwortet,
                                SUM(status='angeschrieben') offen,
                                SUM(status='angeschrieben' AND zustellung='gelesen') gelesen,
                                SUM(termin=1) termin,
                                SUM(status='fehler') fehler FROM leads""").fetchone()
    zahlen = {k: (z[k] or 0) for k in
              ["alle", "geantwortet", "offen", "gelesen", "termin", "fehler"]}
    return tpl.TemplateResponse(request, "chats.html",
                                {"zeilen": zeilen, "filter": filter, "zahlen": zahlen})

@app.get("/chats/{lead_id}", response_class=HTMLResponse)
def chat(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        verlauf = c.execute("SELECT * FROM nachrichten WHERE lead_id=? ORDER BY id",
                            (lead_id,)).fetchall()
    return tpl.TemplateResponse(request, "chat.html",
                                {"lead": lead, "verlauf": verlauf, "followups": FOLLOWUPS})

@app.post("/chats/{lead_id}/termin")
async def termin_umschalten(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        c.execute("UPDATE leads SET termin = 1 - COALESCE(termin,0) WHERE id=?", (lead_id,))
        c.commit()
    return RedirectResponse(f"/chats/{lead_id}", status_code=303)

@app.post("/chats/{lead_id}/vorlage")
async def chat_vorlage(request: Request, lead_id: int, vorlage: str = Form(...)):
    if not angemeldet(request):
        return zum_login()
    if vorlage not in FOLLOWUPS:
        return RedirectResponse(f"/chats/{lead_id}", status_code=303)
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    ok, fehler, wamid = await followup_senden(lead["telefon"], vorlage)
    with closing(db()) as c:
        if ok:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit,wamid)"
                      " VALUES (?,?,?,?,?)",
                      (lead_id, "raus", FOLLOWUPS[vorlage]["text"], jetzt(), wamid))
        else:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead_id, "system", f"Vorlage nicht gesendet: {fehler}", jetzt()))
        c.commit()
    return RedirectResponse(f"/chats/{lead_id}", status_code=303)

@app.post("/chats/{lead_id}/senden")
async def chat_senden(request: Request, lead_id: int, text: str = Form(...)):
    if not angemeldet(request):
        return zum_login()
    text = text.strip()
    if not text:
        return RedirectResponse(f"/chats/{lead_id}", status_code=303)
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    ok, fehler, wamid = await text_senden(lead["telefon"], text)
    with closing(db()) as c:
        if ok:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit,wamid)"
                      " VALUES (?,?,?,?,?)", (lead_id, "raus", text, jetzt(), wamid))
        else:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead_id, "system", f"Nicht gesendet: {fehler}", jetzt()))
        c.commit()
    return RedirectResponse(f"/chats/{lead_id}", status_code=303)

# ---------------------------------------------------------------- Webhook
@app.get("/webhook")
def webhook_pruefen(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("abgelehnt", status_code=403)

ZUSTELL_TEXT = {"sent": "gesendet", "delivered": "zugestellt",
                "read": "gelesen", "failed": "fehlgeschlagen"}

@app.post("/webhook")
async def webhook(request: Request):
    daten = await request.json()
    try:
        for eintrag in daten.get("entry", []):
            for change in eintrag.get("changes", []):
                wert = change.get("value", {})
                for nachricht in wert.get("messages", []):
                    await eingang_verarbeiten(nachricht)
                for st in wert.get("statuses", []):
                    status_verarbeiten(st)
    except Exception as e:
        print("Webhook-Fehler:", e, flush=True)
    return {"status": "ok"}

def status_verarbeiten(st):
    nummer = st.get("recipient_id", "")
    wamid = st.get("id", "")
    zustand = ZUSTELL_TEXT.get(st.get("status", ""), st.get("status", ""))
    grund = ""
    for f in st.get("errors", []) or []:
        teile = [str(f.get("code", "")), f.get("title", "") or f.get("message", "")]
        detail = (f.get("error_data") or {}).get("details", "")
        if detail:
            teile.append(detail)
        grund = ": ".join(x for x in teile if x)
    with closing(db()) as c:
        if wamid:
            c.execute("UPDATE nachrichten SET zustellung=? WHERE wamid=?", (zustand, wamid))
        lead = c.execute("SELECT * FROM leads WHERE telefon=?", (nummer,)).fetchone()
        if not lead:
            c.commit()
            return
        rang = {"angenommen": 0, "gesendet": 1, "zugestellt": 2, "gelesen": 3}
        alt = rang.get(lead["zustellung"] or "", -1)
        if zustand == "fehlgeschlagen" or rang.get(zustand, 99) >= alt:
            c.execute("UPDATE leads SET zustellung=?, zustellung_zeit=? WHERE id=?",
                      (zustand, jetzt(), lead["id"]))
        if zustand == "fehlgeschlagen":
            c.execute("UPDATE leads SET status='fehler', fehler=? WHERE id=?",
                      (grund or "Von WhatsApp abgelehnt", lead["id"]))
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead["id"], "system",
                       f"Nicht zugestellt. {grund}" if grund else "Nicht zugestellt.", jetzt()))
        c.commit()

async def eingang_verarbeiten(nachricht):
    von = nachricht.get("from", "")
    typ = nachricht.get("type")
    medien_datei = medien_typ = None
    if typ == "text":
        text = nachricht.get("text", {}).get("body", "")
    elif typ == "button":
        text = nachricht.get("button", {}).get("text", "")
    elif typ in ("audio", "voice", "image", "video", "document", "sticker"):
        block = nachricht.get(typ, {}) or {}
        mime = block.get("mime_type", "")
        medien_datei = await medien_holen(block.get("id"), mime)
        medien_typ = typ
        beschriftung = {"audio": "Sprachnachricht", "voice": "Sprachnachricht",
                        "image": "Bild", "video": "Video",
                        "document": "Dokument", "sticker": "Sticker"}[typ]
        zusatz = block.get("caption") or block.get("filename") or ""
        text = f"{beschriftung}{': ' + zusatz if zusatz else ''}"
        if not medien_datei:
            text += " (konnte nicht geladen werden)"
    else:
        text = f"[{typ}]"
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE telefon=?", (von,)).fetchone()
        if not lead:
            c.execute("INSERT INTO leads (telefon,status,angelegt) VALUES (?,?,?)",
                      (von, "angeschrieben", jetzt()))
            c.commit()
            lead = c.execute("SELECT * FROM leads WHERE telefon=?", (von,)).fetchone()
        c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit,wamid,medien_datei,medien_typ)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (lead["id"], "rein", text, jetzt(), nachricht.get("id"),
                   medien_datei, medien_typ))
        c.execute("UPDATE leads SET status='geantwortet' WHERE id=?", (lead["id"],))
        c.commit()

@app.get("/health")
def health():
    return {"ok": True}
