"""
service.py
Kleiner lokaler Dienst, den n8n (im Docker-Container) aufruft.
Rechnet mit positions.py + alerts.py und gibt die Warnungen zurück –
den Versand an Telegram übernimmt n8n.

Start:
  uvicorn service:app --host 172.17.0.1 --port 8765

Endpunkte:
  GET  /health   → läuft der Dienst?
  POST /check    → Regeln prüfen, Warnungen als Liste zurück (aus der Warteschlange)
  POST /ack      → n8n bestätigt: diese Nachricht ist bei Telegram angekommen

Warteschlange (postausgang.json): Jede Nachricht bleibt gespeichert, bis n8n sie mit /ack
bestätigt. Ohne Bestätigung wird sie bei einer späteren Prüfung erneut mitgeschickt
(frühestens nach NOCHMAL_NACH_S Sekunden). Was älter als VERFALL_MIN Minuten ist, wird
verworfen und als eine Sammelmeldung „… nicht zugestellt“ gemeldet.
Solange n8n noch nie bestätigt hat (alter Workflow ohne /ack-Knoten), gilt jede
ausgelieferte Nachricht sofort als erledigt – so wie früher.
  GET  /snapshot → aktueller Stand
  POST /report   → einmal täglich: wallet_transactions.csv ergänzen + Tagesbericht
                   für alle fertigen, noch nicht gemeldeten Tage
                   (läuft im Hintergrund, weil das große Modell Minuten braucht)
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Body

import sys  # noqa: E402
sys.stdout.reconfigure(line_buffering=True)   # Meldungen sofort im Journal (systemd puffert sonst)

# Arbeitsordner = Projektordner (dort liegen .env, alert_state.json, CSV-Dateien)
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

import positions  # noqa: E402
import alerts     # noqa: E402
import report     # noqa: E402
import wallet_export  # noqa: E402
import papertrade     # noqa: E402
import status         # noqa: E402

API_KEY = os.getenv("SERVICE_API_KEY")
app = FastAPI(title="Wallet Wächter")
lock = threading.Lock()                # nie zwei Prüfungen gleichzeitig
report_lock = threading.Lock()         # nie zwei Berichte gleichzeitig


# ---------------------------------------------------------------
# Warteschlange mit Versandbestätigung
# ---------------------------------------------------------------
POSTAUSGANG = "postausgang.json"
NOCHMAL_NACH_S = 90        # unbestätigte Nachricht frühestens nach 90 Sekunden erneut ausliefern
VERFALL_MIN = 30           # danach nicht mehr einzeln senden (wäre veraltet), nur Sammelmeldung
MAX_NACHRICHTEN = 200
MAX_ZEICHEN = 4000         # Telegram nimmt höchstens 4096 Zeichen pro Nachricht
BERLIN = ZoneInfo("Europe/Berlin")
pa_lock = threading.Lock() # nur für postausgang.json – /ack wartet nie auf eine laufende Prüfung


def sicher(e):
    """Fehlertext ohne Geheimnisse (Telegram-Token, RPC-Adresse mit Schlüssel) fürs Journal."""
    text = f"{type(e).__name__}: {e}"
    for geheim in (os.getenv("TELEGRAM_TOKEN"), os.getenv("RPC_URL"), API_KEY):
        if geheim:
            text = text.replace(geheim, "***")
    return text


def _laden():
    if os.path.exists(POSTAUSGANG):
        try:
            with open(POSTAUSGANG) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"ack_aktiv": False, "nachrichten": [], "ausgeliefert": []}


def _speichern(pa):
    tmp = POSTAUSGANG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pa, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POSTAUSGANG)           # nie halb geschriebene Datei


def _sammel_text(anzahl, von, bis):
    v = datetime.fromtimestamp(von, BERLIN).strftime("%d.%m. %H:%M")
    b = datetime.fromtimestamp(bis, BERLIN).strftime("%H:%M")
    return (f"⚠️ <b>{anzahl} Warnung(en) zwischen {v} und {b} nicht zugestellt</b> "
            f"(Internet/Telegram/n8n gestört).\nAktuellen Stand mit /status prüfen.")


def ausliefern(neue):
    """Neue Nachrichten einreihen und alles zurückgeben, was jetzt (wieder) gesendet werden soll."""
    with pa_lock:
        pa = _laden()
        pa.setdefault("ausgeliefert", [])
        jetzt = time.time()
        for text in neue:
            if len(text) > MAX_ZEICHEN:
                text = text[:MAX_ZEICHEN - 20] + "\n… (gekürzt)"
            pa["nachrichten"].append({"id": uuid.uuid4().hex[:12], "text": text, "zeit": jetzt, "gesendet": 0})

        # Veraltete, nie bestätigte Nachrichten → eine Sammelmeldung (bestehende Sammelmeldung wird erweitert)
        alt = [n for n in pa["nachrichten"] if jetzt - n["zeit"] > VERFALL_MIN * 60]
        if alt:
            pa["nachrichten"] = [n for n in pa["nachrichten"] if n not in alt]
            anzahl = sum(n.get("sammel", {}).get("anzahl", 1) for n in alt)
            von = min(n.get("sammel", {}).get("von", n["zeit"]) for n in alt)
            bis = max(n.get("sammel", {}).get("bis", n["zeit"]) for n in alt)
            alte_sammel = [n for n in pa["nachrichten"] if "sammel" in n]
            for n in alte_sammel:                        # noch offene Sammelmeldung mit einrechnen
                anzahl += n["sammel"]["anzahl"]
                von, bis = min(von, n["sammel"]["von"]), max(bis, n["sammel"]["bis"])
                pa["nachrichten"].remove(n)
            pa["nachrichten"].insert(0, {"id": uuid.uuid4().hex[:12], "zeit": jetzt, "gesendet": 0,
                                         "sammel": {"anzahl": anzahl, "von": von, "bis": bis},
                                         "text": _sammel_text(anzahl, von, bis)})
        pa["nachrichten"] = pa["nachrichten"][-MAX_NACHRICHTEN:]

        faellig = [n for n in pa["nachrichten"] if jetzt - n["gesendet"] >= NOCHMAL_NACH_S]
        for n in faellig:
            n["gesendet"] = jetzt
        if not pa["ack_aktiv"]:            # alter Workflow ohne Bestätigung: wie früher sofort erledigt
            pa["nachrichten"] = [n for n in pa["nachrichten"] if n not in faellig]
            pa["ausgeliefert"] = (pa["ausgeliefert"] + [n["id"] for n in faellig])[-MAX_NACHRICHTEN:]
        _speichern(pa)
    # „nachrichten“ im Objekt, damit der Telegram-Knoten mit {{ $json.nachrichten }} weiter funktioniert
    return [{"id": n["id"], "nachrichten": n["text"]} for n in faellig]


def bestaetigen(nid):
    with pa_lock:
        pa = _laden()
        pa.setdefault("ausgeliefert", [])
        offen = any(n["id"] == nid for n in pa["nachrichten"])
        if not (offen or nid in pa["ausgeliefert"]):
            return False                   # unbekannte ID → nichts ändern (auch Bestätigungsmodus nicht)
        pa["ack_aktiv"] = True             # n8n bestätigt wirklich → ab jetzt bis zur Bestätigung aufheben
        pa["nachrichten"] = [n for n in pa["nachrichten"] if n["id"] != nid]
        pa["ausgeliefert"] = []
        _speichern(pa)
        return offen


def auth(key):
    if not API_KEY or key != API_KEY:
        raise HTTPException(status_code=401, detail="falscher oder fehlender Schlüssel")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/check")
def check(x_api_key: str = Header(None)):
    auth(x_api_key)
    with lock:
        try:
            snap = positions.snapshot()
        except Exception as e:             # z. B. Solana-RPC nicht erreichbar → n8n versucht es in 30 s erneut
            print(f"Snapshot fehlgeschlagen: {sicher(e)}")
            raise HTTPException(status_code=503, detail="Solana-Daten nicht abrufbar")
        state = alerts.load_state()
        msgs = alerts.check(snap, state)
        alerts.save_state(state)
        # Papierhandel (kein echtes Geld) – Fehler dort dürfen die Warnungen nie stören
        try:
            msgs += papertrade.check()
        except Exception as e:
            print(f"Papierhandel fehlgeschlagen: {sicher(e)}")
        # /status-Befehl beantworten bzw. Lebenszeichen nach längerer Ruhe
        try:
            msgs = status.ergaenzen(msgs, snap)
        except Exception as e:
            print(f"Status fehlgeschlagen: {sicher(e)}")
        senden = ausliefern(msgs)
    return {"vermoegen_usd": snap["gesamtvermoegen_usd"],
            "preisquelle": snap["preisquelle"],
            "nachrichten": senden}


@app.post("/ack")
def ack(x_api_key: str = Header(None), daten: dict = Body(...)):
    auth(x_api_key)
    gefunden = bestaetigen(str(daten.get("id", "")))
    return {"bestaetigt": gefunden}


@app.get("/snapshot")
def snapshot(x_api_key: str = Header(None)):
    auth(x_api_key)
    return positions.snapshot()


@app.post("/report")
def make_report(x_api_key: str = Header(None)):
    auth(x_api_key)
    if report_lock.locked():
        return {"status": "läuft bereits"}

    def job():
        with report_lock:
            # 1. Blockchain-Historie ergänzen (lädt nur neue Transaktionen, Rest kommt aus dem Cache)
            if report.offene_tage():
                try:
                    wallet_export.main()
                except Exception as e:
                    print(f"wallet_export fehlgeschlagen: {sicher(e)}")
            # 2. Tagesbericht (Fehler landen im Journal; der nächste stündliche Aufruf versucht es erneut)
            try:
                gesendet = report.run_pending()
                print(f"Tagesbericht gesendet für: {gesendet}")
            except Exception as e:
                print(f"Tagesbericht fehlgeschlagen: {sicher(e)}")

    threading.Thread(target=job, daemon=True).start()
    return {"status": "gestartet", "offene_tage": [t.isoformat() for t in report.offene_tage()]}
