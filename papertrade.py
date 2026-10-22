"""
papertrade.py
Papierhandel (ohne echtes Geld) für den besten Kandidaten aus dem Backtest:

  SOL · Einstieg, wenn der Kurs mindestens 1,5 % unter dem Hoch der letzten 60 Minuten liegt
        UND über dem 24-Stunden-Durchschnitt (Aufwärtstrend)
      · Ziel +0,5 %  · Notausstieg −2 %  · spätestens nach 24 Stunden raus
      · Gebühren wie im Backtest abgezogen

Rechnet genau wie backtest.py (abgeschlossene 1-Minuten-Kerzen von Binance),
damit Papierhandel und Backtest vergleichbar sind.
Läuft automatisch bei jeder Prüfung des Wallet Wächters mit (service.py).

Dateien:
  paper_state.json   aktueller Stand (offene Papier-Position, letzte Kerze)
  paper_trades.csv   Journal aller Papier-Trades
"""

import os
import csv
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# ---------------------------------------------------------------
# 1. Regel (aus dem Backtest)
# ---------------------------------------------------------------
SYMBOL, MARKT = "SOLUSDT", "SOL"
RUECKGANG = 1.5          # % unter dem 60-Min.-Hoch
ZIEL = 0.5               # % Gewinnziel
NOTAUSSTIEG = 2.0        # % Notausstieg
MAX_HALTEN_MIN = 24 * 60
PAUSE_NACH_TRADE_MIN = 15
GEBUEHR, SCHLUPF = 0.12, 0.02
HEBEL_BEISPIEL = 10      # nur zur Anzeige: was wäre es mit 10× gewesen

STATE_FILE = "paper_state.json"
TRADES_FILE = "paper_trades.csv"
BERLIN = ZoneInfo("Europe/Berlin")


# ---------------------------------------------------------------
# 2. Kerzen holen (letzte ~25 Stunden, nur abgeschlossene)
# ---------------------------------------------------------------
def kerzen():
    url = "https://data-api.binance.vision/api/v3/klines"
    neu = requests.get(url, params={"symbol": SYMBOL, "interval": "1m", "limit": 1000}, timeout=20).json()
    alt = requests.get(url, params={"symbol": SYMBOL, "interval": "1m", "limit": 600,
                                    "endTime": neu[0][0] - 1}, timeout=20).json()
    jetzt_ms = int(time.time() * 1000)
    rows = [(k[0], float(k[2]), float(k[3]), float(k[4])) for k in alt + neu if k[6] < jetzt_ms]
    df = pd.DataFrame(rows, columns=["ms", "high", "low", "close"]).drop_duplicates("ms").sort_values("ms")
    df["hoch60"] = df["high"].rolling(60, min_periods=60).max().shift(1)
    df["trend_ok"] = df["close"] > df["close"].rolling(1440, min_periods=60).mean()
    return df.reset_index(drop=True)


# ---------------------------------------------------------------
# 3. Hilfsfunktionen
# ---------------------------------------------------------------
def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save(st):
    tmp = STATE_FILE + ".tmp"                  # erst Hilfsdatei, dann ersetzen → nie halb geschrieben
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)


def zeit(ms):
    return datetime.fromtimestamp(ms / 1000, BERLIN).strftime("%d.%m. %H:%M")


def de(x, d=2, plus=False):
    # Zahl deutsch: Komma statt Punkt, echtes Minuszeichen
    s = f"{x:+.{d}f}" if plus else f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def usd(x):
    return f"{x:,.2f} $".replace(",", "X").replace(".", ",").replace("X", ".")


def journal(row):
    neu = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if neu:
            w.writeheader()
        w.writerow(row)


def bilanz():
    if not os.path.exists(TRADES_FILE):
        return ""
    t = pd.read_csv(TRADES_FILE)
    return (f"Bilanz Papierhandel: {len(t)} Trades, Treffer {de((t['ergebnis_pct'] > 0).mean() * 100, 0)} %, "
            f"Summe {de(t['ergebnis_pct'].sum(), 2, True)} % (≈ {de(t['ergebnis_pct'].sum() * HEBEL_BEISPIEL, 1, True)} % bei {HEBEL_BEISPIEL}×)")


# ---------------------------------------------------------------
# 4. Prüfen: alle neuen abgeschlossenen Kerzen der Reihe nach abarbeiten
# ---------------------------------------------------------------
def check():
    msgs = []
    st = load()
    df = kerzen()
    if df.empty:
        return msgs
    if "letzte_ms" not in st:                     # erster Start: ab jetzt beobachten, nichts nachholen
        st.update({"letzte_ms": int(df["ms"].iloc[-1]), "position": None, "frei_ab_ms": 0})
        save(st)
        return [f"🧪 Papierhandel gestartet ({MARKT}): Einstieg ab −{de(RUECKGANG, 1)} % unter Stundenhoch "
                f"im Aufwärtstrend, Ziel +{de(ZIEL, 1)} %, Notausstieg −{de(NOTAUSSTIEG, 1)} %. Kein echtes Geld."]

    for _, k in df[df["ms"] > st["letzte_ms"]].iterrows():
        ms = int(k["ms"])
        pos = st["position"]
        if pos:
            einstieg = pos["preis"]
            grund = None
            if k["low"] <= einstieg * (1 - NOTAUSSTIEG / 100):          # gleiche Minute → Notausstieg zuerst
                grund, ergebnis, preis = "Notausstieg", -NOTAUSSTIEG, einstieg * (1 - NOTAUSSTIEG / 100)
            elif k["high"] >= einstieg * (1 + ZIEL / 100):
                grund, ergebnis, preis = "Ziel erreicht", ZIEL, einstieg * (1 + ZIEL / 100)
            elif ms - pos["ms"] >= MAX_HALTEN_MIN * 60_000:
                grund, preis = "Zeit abgelaufen", k["close"]
                ergebnis = (preis / einstieg - 1) * 100
            if grund:
                netto = ergebnis - GEBUEHR - SCHLUPF
                journal({"einstieg": zeit(pos["ms"]), "ausstieg": zeit(ms), "markt": MARKT,
                         "einstieg_preis": round(einstieg, 4), "ausstieg_preis": round(preis, 4),
                         "grund": grund, "ergebnis_pct": round(netto, 3),
                         "minuten": int((ms - pos["ms"]) / 60_000)})
                symbol = "✅" if netto > 0 else "❌"
                msgs.append(f"🧪 {symbol} <b>Papierhandel {MARKT}: würde jetzt VERKAUFEN – {de(netto, 2, True)} %</b>\n"
                            f"{grund} bei {usd(preis)}.\n"
                            f"Ergebnis {de(netto, 2, True)} % nach Gebühren (≈ {de(netto * HEBEL_BEISPIEL, 1, True)} % bei "
                            f"{HEBEL_BEISPIEL}×), gehalten {int((ms - pos['ms']) / 60_000)} Min.\n{bilanz()}")
                st["position"] = None
                st["frei_ab_ms"] = ms + PAUSE_NACH_TRADE_MIN * 60_000
        elif ms >= st["frei_ab_ms"] and pd.notna(k["hoch60"]) and k["trend_ok"] \
                and k["close"] <= k["hoch60"] * (1 - RUECKGANG / 100):
            st["position"] = {"ms": ms, "preis": float(k["close"])}
            abstand = (k["close"] / k["hoch60"] - 1) * 100
            msgs.append(f"🧪 <b>Papierhandel {MARKT}: würde jetzt KAUFEN</b>\nbei {usd(k['close'])} "
                        f"({de(abstand)} % unter dem Stundenhoch, Aufwärtstrend).\n"
                        f"Ziel {usd(k['close'] * (1 + ZIEL / 100))} (+{de(ZIEL, 1)} %) · "
                        f"Notausstieg {usd(k['close'] * (1 - NOTAUSSTIEG / 100))} (−{de(NOTAUSSTIEG, 1)} %)")
        st["letzte_ms"] = ms
    save(st)
    return msgs


if __name__ == "__main__":
    for m in check():
        print(m, "\n")
    print(json.dumps(load(), indent=2))
