import os, io, csv, json, sqlite3, secrets, hashlib, time
from contextlib import closing
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------- Konfiguration
APP_PASSWORT      = os.getenv("APP_PASSWORT", "aendern-bitte")
META_TOKEN        = os.getenv("META_TOKEN", "")
PHONE_NUMBER_ID   = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "theratalent-webhook")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
DB_PFAD           = os.getenv("DB_PFAD", "/data/setter.db")
GRAPH             = "https://graph.facebook.com/v23.0"

TEMPLATES = {
    "interesse":   {"name": "reaktivierung_interesse",   "label": "Hatte Interesse"},
    "abgebrochen": {"name": "reaktivierung_abgebrochen", "label": "Abgesprungen"},
}

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", """Du schreibst als Matthias Langwald von TheraTalent auf WhatsApp.
TheraTalent vermittelt Personal an Praxen und Einrichtungen im Therapiebereich.
Du schreibst ehemalige Kontakte an, die sich vor einer Weile wegen Personalsuche gemeldet hatten.

Dein Ziel: herausfinden, ob gerade Bedarf besteht, und bei Interesse ein kurzes Telefonat vereinbaren.

So schreibst du:
- kurz, eine bis drei Zeilen, wie eine echte WhatsApp Nachricht
- du, nicht Sie
- eine Frage pro Nachricht, nie mehrere
- keine Aufzaehlungen, keine Emojis, keine Gedankenstriche
- kein Verkaufssprech, keine Floskeln wie "gerne" oder "ich melde mich"
- auf eine Antwort erst eingehen, dann weiterfragen

Ablauf: Bedarf klaeren, dann konkret werden (welche Stelle, seit wann offen),
dann Telefonat vorschlagen. Bei klarem Desinteresse freundlich abschliessen
und nicht nachbohren.

Wenn du unsicher bist oder nach Preisen, Vertraegen oder Konditionen gefragt wirst,
antworte, dass Matthias sich dazu persoenlich meldet.""")

app = FastAPI()
tpl = Jinja2Templates(directory="templates")
SESSIONS = set()

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
            vorname TEXT,
            nachname TEXT,
            unternehmen TEXT,
            kategorie TEXT DEFAULT 'interesse',
            status TEXT DEFAULT 'neu',
            auto_antwort INTEGER DEFAULT 1,
            fehler TEXT,
            angelegt TEXT,
            gesendet TEXT
        );
        CREATE TABLE IF NOT EXISTS nachrichten (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            richtung TEXT NOT NULL,
            text TEXT,
            zeit TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        CREATE INDEX IF NOT EXISTS idx_n_lead ON nachrichten(lead_id);
        """)
        c.commit()

init_db()

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
    return request.cookies.get("sid") in SESSIONS

def zum_login():
    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, fehler: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"fehler": fehler})

@app.post("/login")
def login(response: Response, passwort: str = Form(...)):
    if not secrets.compare_digest(passwort, APP_PASSWORT):
        return RedirectResponse("/login?fehler=1", status_code=303)
    sid = secrets.token_urlsafe(32)
    SESSIONS.add(sid)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return r

@app.get("/logout")
def logout(request: Request):
    SESSIONS.discard(request.cookies.get("sid"))
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("sid")
    return r

# ---------------------------------------------------------------- WhatsApp
async def whatsapp_senden(payload):
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return False, "META_TOKEN oder PHONE_NUMBER_ID fehlt"
    url = f"{GRAPH}/{PHONE_NUMBER_ID}/messages"
    kopf = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=kopf, json=payload)
    if r.status_code >= 400:
        try:
            d = r.json()["error"]
            return False, f"{d.get('code')}: {d.get('message')}"
        except Exception:
            return False, r.text[:200]
    return True, ""

async def template_senden(telefon, template_name, vorname):
    return await whatsapp_senden({
        "messaging_product": "whatsapp",
        "to": telefon,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "de"},
            "components": [{
                "type": "body",
                "parameters": [{
                    "type": "text",
                    "parameter_name": "name",
                    "text": vorname or "zusammen",
                }],
            }],
        },
    })

async def text_senden(telefon, text):
    return await whatsapp_senden({
        "messaging_product": "whatsapp",
        "to": telefon,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    })

# ---------------------------------------------------------------- KI
async def antwort_erzeugen(lead, verlauf):
    if not ANTHROPIC_API_KEY:
        return None
    wer = " ".join(x for x in [lead["vorname"], lead["nachname"]] if x) or "Unbekannt"
    kontext = f"Du schreibst mit: {wer}"
    if lead["unternehmen"]:
        kontext += f" von {lead['unternehmen']}"
    msgs = []
    for n in verlauf:
        rolle = "assistant" if n["richtung"] == "raus" else "user"
        if msgs and msgs[-1]["role"] == rolle:
            msgs[-1]["content"] += "\n" + (n["text"] or "")
        else:
            msgs.append({"role": rolle, "content": n["text"] or ""})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(Gespraech beginnt)"})
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": SYSTEM_PROMPT + "\n\n" + kontext,
                "messages": msgs,
            },
        )
    if r.status_code >= 400:
        return None
    teile = [b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"]
    text = "".join(teile).strip()
    return text or None

# ---------------------------------------------------------------- Leads
@app.get("/", response_class=HTMLResponse)
def leads_seite(request: Request, meldung: str = ""):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        leads = c.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
    return tpl.TemplateResponse(request, "leads.html", {
        "leads": leads, "meldung": meldung, "templates": TEMPLATES,
    })

@app.post("/leads/csv")
async def leads_csv(request: Request, datei: UploadFile = File(...)):
    if not angemeldet(request):
        return zum_login()
    roh = (await datei.read()).decode("utf-8-sig", errors="replace")
    trenner = ";" if roh.count(";") > roh.count(",") else ","
    leser = csv.DictReader(io.StringIO(roh), delimiter=trenner)
    neu = 0
    with closing(db()) as c:
        for zeile in leser:
            z = {(k or "").strip().lower(): (v or "").strip() for k, v in zeile.items()}
            tel = nummer_normalisieren(z.get("telefon") or z.get("nummer") or z.get("handy") or "")
            if len(tel) < 8:
                continue
            kat = (z.get("kategorie") or z.get("status") or "interesse").lower()
            if kat not in TEMPLATES:
                kat = "interesse"
            try:
                c.execute(
                    "INSERT INTO leads (telefon,vorname,nachname,unternehmen,kategorie,angelegt)"
                    " VALUES (?,?,?,?,?,?)",
                    (tel, z.get("vorname", ""), z.get("nachname", ""),
                     z.get("unternehmen", ""), kat, jetzt()),
                )
                neu += 1
            except sqlite3.IntegrityError:
                pass
        c.commit()
    return RedirectResponse(f"/?meldung={neu} Kontakte importiert", status_code=303)

@app.post("/leads/neu")
async def lead_neu(request: Request, telefon: str = Form(...), vorname: str = Form(""),
                   nachname: str = Form(""), unternehmen: str = Form(""),
                   kategorie: str = Form("interesse")):
    if not angemeldet(request):
        return zum_login()
    tel = nummer_normalisieren(telefon)
    if len(tel) < 8:
        return RedirectResponse("/?meldung=Nummer unvollstaendig", status_code=303)
    with closing(db()) as c:
        try:
            c.execute(
                "INSERT INTO leads (telefon,vorname,nachname,unternehmen,kategorie,angelegt)"
                " VALUES (?,?,?,?,?,?)",
                (tel, vorname, nachname, unternehmen,
                 kategorie if kategorie in TEMPLATES else "interesse", jetzt()),
            )
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

async def einen_anschreiben(lead_id):
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or lead["status"] != "neu":
            return False
    t = TEMPLATES[lead["kategorie"]]["name"]
    ok, fehler = await template_senden(lead["telefon"], t, lead["vorname"])
    with closing(db()) as c:
        if ok:
            c.execute("UPDATE leads SET status='angeschrieben', gesendet=?, fehler=NULL WHERE id=?",
                      (jetzt(), lead_id))
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead_id, "raus", f"[Vorlage {t}]", jetzt()))
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
        f"/?meldung={'Nachricht raus' if ok else 'Versand fehlgeschlagen, Grund steht in der Zeile'}",
        status_code=303)

@app.post("/leads/alle-senden")
async def alle_senden(request: Request):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        offen = [r["id"] for r in c.execute("SELECT id FROM leads WHERE status='neu'").fetchall()]
    raus = 0
    for lid in offen:
        if await einen_anschreiben(lid):
            raus += 1
        time.sleep(1)
    return RedirectResponse(f"/?meldung={raus} von {len(offen)} verschickt", status_code=303)

# ---------------------------------------------------------------- Chats
@app.get("/chats", response_class=HTMLResponse)
def chats(request: Request):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        zeilen = c.execute("""
            SELECT l.*, 
                   (SELECT text FROM nachrichten n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1) letzte,
                   (SELECT zeit FROM nachrichten n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1) letzte_zeit,
                   (SELECT COUNT(*) FROM nachrichten n WHERE n.lead_id=l.id AND n.richtung='rein') antworten
            FROM leads l WHERE l.status!='neu'
            ORDER BY letzte_zeit DESC
        """).fetchall()
    return tpl.TemplateResponse(request, "chats.html", {"zeilen": zeilen})

@app.get("/chats/{lead_id}", response_class=HTMLResponse)
def chat(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        verlauf = c.execute("SELECT * FROM nachrichten WHERE lead_id=? ORDER BY id",
                            (lead_id,)).fetchall()
    return tpl.TemplateResponse(request, "chat.html",
                                {"lead": lead, "verlauf": verlauf})

@app.post("/chats/{lead_id}/senden")
async def chat_senden(request: Request, lead_id: int, text: str = Form(...)):
    if not angemeldet(request):
        return zum_login()
    text = text.strip()
    if not text:
        return RedirectResponse(f"/chats/{lead_id}", status_code=303)
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    ok, fehler = await text_senden(lead["telefon"], text)
    with closing(db()) as c:
        if ok:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead_id, "raus", text, jetzt()))
        else:
            c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                      (lead_id, "system", f"Nicht zugestellt: {fehler}", jetzt()))
        c.commit()
    return RedirectResponse(f"/chats/{lead_id}", status_code=303)

@app.post("/chats/{lead_id}/auto")
async def auto_umschalten(request: Request, lead_id: int):
    if not angemeldet(request):
        return zum_login()
    with closing(db()) as c:
        c.execute("UPDATE leads SET auto_antwort = 1 - auto_antwort WHERE id=?", (lead_id,))
        c.commit()
    return RedirectResponse(f"/chats/{lead_id}", status_code=303)

# ---------------------------------------------------------------- Webhook
@app.get("/webhook")
def webhook_pruefen(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("abgelehnt", status_code=403)

@app.post("/webhook")
async def webhook(request: Request):
    daten = await request.json()
    try:
        for eintrag in daten.get("entry", []):
            for change in eintrag.get("changes", []):
                wert = change.get("value", {})
                for nachricht in wert.get("messages", []):
                    await eingang_verarbeiten(nachricht)
    except Exception as e:
        print("Webhook-Fehler:", e, flush=True)
    return {"status": "ok"}

async def eingang_verarbeiten(nachricht):
    von = nachricht.get("from", "")
    if nachricht.get("type") == "text":
        text = nachricht.get("text", {}).get("body", "")
    elif nachricht.get("type") == "button":
        text = nachricht.get("button", {}).get("text", "")
    else:
        text = f"[{nachricht.get('type')}]"
    with closing(db()) as c:
        lead = c.execute("SELECT * FROM leads WHERE telefon=?", (von,)).fetchone()
        if not lead:
            c.execute("INSERT INTO leads (telefon,status,angelegt) VALUES (?,?,?)",
                      (von, "angeschrieben", jetzt()))
            c.commit()
            lead = c.execute("SELECT * FROM leads WHERE telefon=?", (von,)).fetchone()
        c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                  (lead["id"], "rein", text, jetzt()))
        c.execute("UPDATE leads SET status='geantwortet' WHERE id=?", (lead["id"],))
        c.commit()
    if not lead["auto_antwort"]:
        return
    with closing(db()) as c:
        verlauf = c.execute(
            "SELECT * FROM nachrichten WHERE lead_id=? AND richtung IN ('rein','raus') ORDER BY id",
            (lead["id"],)).fetchall()
    antwort = await antwort_erzeugen(lead, verlauf)
    if not antwort:
        return
    ok, fehler = await text_senden(von, antwort)
    with closing(db()) as c:
        c.execute("INSERT INTO nachrichten (lead_id,richtung,text,zeit) VALUES (?,?,?,?)",
                  (lead["id"], "raus" if ok else "system",
                   antwort if ok else f"Antwort nicht zugestellt: {fehler}", jetzt()))
        c.commit()

@app.get("/health")
def health():
    return {"ok": True}
