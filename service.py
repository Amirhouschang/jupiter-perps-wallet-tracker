"""
service.py
Kleiner lokaler Dienst, den n8n (im Docker-Container) aufruft.
Rechnet mit positions.py + alerts.py und gibt die Warnungen zurück –
den Versand an Telegram übernimmt n8n.

Start:
  uvicorn service:app --host 172.17.0.1 --port 8765

Endpunkte:
  GET  /health   → läuft der Dienst?
  POST /check    → Regeln prüfen, Warnungen als Liste zurück
  GET  /snapshot → aktueller Stand
  POST /report   → einmal täglich: wallet_transactions.csv ergänzen + Tagesbericht
                   für alle fertigen, noch nicht gemeldeten Tage
                   (läuft im Hintergrund, weil das große Modell Minuten braucht)
"""

import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException

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
        snap = positions.snapshot()
        state = alerts.load_state()
        msgs = alerts.check(snap, state)
        alerts.save_state(state)
        # Papierhandel (kein echtes Geld) – Fehler dort dürfen die Warnungen nie stören
        try:
            msgs += papertrade.check()
        except Exception as e:
            print(f"Papierhandel fehlgeschlagen: {e}")
        # /status-Befehl beantworten bzw. Lebenszeichen nach längerer Ruhe
        try:
            msgs = status.ergaenzen(msgs, snap)
        except Exception as e:
            print(f"Status fehlgeschlagen: {e}")
    return {"vermoegen_usd": snap["gesamtvermoegen_usd"],
            "preisquelle": snap["preisquelle"],
            "nachrichten": msgs}


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
                    print(f"wallet_export fehlgeschlagen: {e}")
            # 2. Tagesbericht
            report.run_pending()

    threading.Thread(target=job, daemon=True).start()
    return {"status": "gestartet", "offene_tage": [t.isoformat() for t in report.offene_tage()]}
