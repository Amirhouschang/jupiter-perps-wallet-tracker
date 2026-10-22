"""
status.py
Damit du weißt, dass der Wallet Wächter läuft – auch wenn gerade nichts passiert:

1. Befehl /status im Telegram-Chat → Antwort mit aktuellem Stand (innerhalb von ~30 Sekunden)
2. Lebenszeichen: Kam LEBENSZEICHEN_STUNDEN lang keine Nachricht, schickt der Bot
   von sich aus eine kurze Statusmeldung.

Wird bei jeder Prüfung von service.py aufgerufen. Liest Telegram per getUpdates
(kein Webhook nötig, funktioniert auch ohne Internet-Freigabe von n8n).
"""

import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import alerts
import papertrade

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID"))
LEBENSZEICHEN_STUNDEN = 6
STATE_FILE = "status_state.json"
BERLIN = ZoneInfo("Europe/Berlin")


def _load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"offset": 0, "letzte_nachricht": time.time()}


def _save(st):
    tmp = STATE_FILE + ".tmp"                  # erst Hilfsdatei, dann ersetzen → nie halb geschrieben
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def text(snap, titel="📊 Status"):
    jetzt = datetime.now(BERLIN).strftime("%H:%M:%S")
    zeilen = [f"{titel} – Wallet Wächter läuft ✅ ({jetzt})",
              f"Vermögen: {alerts.usd(snap['gesamtvermoegen_usd'])} "
              f"(Wallet {alerts.usd(snap['summe_wallet_usd'])} + Positionen {alerts.usd(snap['summe_positionen_usd'])})"]
    ast = alerts.load_state()
    zeilen += alerts.spot_zeilen(snap, ast)
    if snap["offene_positionen"]:
        for p in snap["offene_positionen"]:
            zeilen.append("\n" + alerts.statuszeile(p, snap["zeit"], ast))
    else:
        zeilen.append("\nHebel: keine Position offen.")
    # Papierhandel
    try:
        pst = papertrade.load()
        pos = pst.get("position")
        zeilen.append("\n🧪 Papierhandel: " + (f"offen seit {papertrade.zeit(pos['ms'])}, Einstieg {alerts.usd(pos['preis'])}"
                                              if pos else "keine offene Papier-Position"))
        b = papertrade.bilanz()
        if b:
            zeilen.append(b)
    except Exception:
        pass
    return "\n".join(zeilen)


def befehle():
    """Liest neue Telegram-Nachrichten; True, wenn /status angefragt wurde."""
    st = _load()
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": st["offset"], "timeout": 0}, timeout=10).json()
    except Exception:
        return False
    angefragt = False
    for u in r.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == CHAT_ID and \
                (msg.get("text") or "").strip().lower() in ("/status", "status"):
            angefragt = True
    _save(st)
    return angefragt


def ergaenzen(msgs, snap):
    """Hängt Status-Antwort bzw. Lebenszeichen an die Nachrichtenliste an."""
    st = _load()
    if befehle():
        msgs.append(text(snap))
    elif not msgs and time.time() - st["letzte_nachricht"] >= LEBENSZEICHEN_STUNDEN * 3600:
        msgs.append(text(snap, titel="💓 Lebenszeichen"))
    if msgs:
        st = _load()                       # offset kann sich in befehle() geändert haben
        st["letzte_nachricht"] = time.time()
        _save(st)
    return msgs
