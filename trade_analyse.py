"""
trade_analyse.py
Analysiert die eigenen Jupiter-Perps-Trades: Was passierte mit dem Kurs
VOR jedem Einstieg (wie „spürbar“ war der Rückgang) und NACH dem Einstieg
(wie viel Gewinn wäre möglich gewesen, was kam am Ende heraus).

Grundlage für Bot-Regeln: Einstieg bei −X %, Verkauf bei +Y %.

Voraussetzungen (im Projektordner):
  transactions_raw.jsonl   (entsteht durch wallet_export.py)
  .env mit WALLET_ADDRESS

Aufruf:
  python trade_analyse.py

Ausgabe:
  trade_analyse.csv        eine Zeile pro Einstieg
  Zusammenfassung im Terminal
"""

import os
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd
from dotenv import load_dotenv
from solders.pubkey import Pubkey

import positions        # für die Positions-Adressen (SOL/ETH/BTC, Long/Short)

# ---------------------------------------------------------------
# 1. Einstellungen
# ---------------------------------------------------------------
load_dotenv()
WALLET = os.getenv("WALLET_ADDRESS")
RAW_FILE = "transactions_raw.jsonl"
OUT_FILE = "trade_analyse.csv"
PERPS = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"

SYMBOL = {"SOL": "SOLUSDT", "ETH": "ETHUSDT", "BTC": "BTCUSDT"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_MARKT = {"So11111111111111111111111111111111111111112": "SOL",
              "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
              "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC"}

VORLAUF_MIN = 60            # so weit vor dem Einstieg wird der Rückgang gemessen
MAX_HALTEN_MIN = 24 * 60    # ohne erkennbares Ende höchstens 24 h beobachten
NEUER_TRADE_NACH_MIN = 30   # Lücke ohne Bewegung → neuer Trade
MIN_BETRAG_USD = 0.5        # kleinere Bewegungen sind nur Gebühren
ANNAHME_HEBEL = 50          # für Trades ohne Rückfluss: Liquidation ≈ bei −(100/Hebel − 0,2) % Kursbewegung


# ---------------------------------------------------------------
# 2. Minutenkurse (Binance, öffentlich, kostenlos) mit Cache
# ---------------------------------------------------------------
def klines(symbol, start, ende):
    cache = f"kurse_{symbol}.csv"
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["zeit"])
        if df["zeit"].min() <= start and df["zeit"].max() >= ende:
            return df.set_index("zeit")["close"]
    rows, t = [], int(start.timestamp() * 1000)
    ende_ms = int(ende.timestamp() * 1000)
    while t < ende_ms:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", timeout=30, params={
            "symbol": symbol, "interval": "1m", "startTime": t, "limit": 1000})
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows += [(k[0], float(k[2]), float(k[3]), float(k[4])) for k in data]
        t = data[-1][0] + 60_000
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ms", "high", "low", "close"])
    df["zeit"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df[["zeit", "high", "low", "close"]].to_csv(cache, index=False)
    return df.set_index("zeit")["close"]


# ---------------------------------------------------------------
# 3. Perps-Transaktionen aus den Rohdaten: Markt, Seite, Geldfluss
# ---------------------------------------------------------------
def perps_events():
    owner = Pubkey.from_string(WALLET)
    pos_map = {str(a): (m, s) for m, s, a in positions.candidates(owner)}
    events = []
    with open(RAW_FILE) as f:
        for line in f:
            d = json.loads(line)
            tx = d["tx"]
            if not tx or tx["meta"].get("err"):
                continue
            keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
            loaded = tx["meta"].get("loadedAddresses") or {}
            keys += loaded.get("writable", []) + loaded.get("readonly", [])
            if PERPS not in keys:
                continue
            treffer = [pos_map[k] for k in keys if k in pos_map]
            if not treffer:
                continue
            markt, seite = treffer[0]
            # Geldfluss des Wallets in dieser Transaktion (Menge je Token)
            meta = tx["meta"]
            flows = {}
            if WALLET in keys:
                i = keys.index(WALLET)
                flows["SOL"] = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9

            def bal(lst):
                out = {}
                for b in lst or []:
                    if b.get("owner") == WALLET:
                        out[b["mint"]] = out.get(b["mint"], 0) + float(b["uiTokenAmount"]["uiAmountString"] or 0)
                return out
            pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
            for mint in set(pre) | set(post):
                flows[mint] = flows.get(mint, 0) + post.get(mint, 0) - pre.get(mint, 0)
            events.append({"zeit": datetime.fromtimestamp(tx["blockTime"], tz=timezone.utc),
                           "markt": markt, "seite": seite, "flows": flows, "signatur": d["signature"]})
    return sorted(events, key=lambda e: e["zeit"])


def usd_wert(flows, zeit, kurse):
    # Wert des Geldflusses in USD zum Zeitpunkt der Transaktion
    w = 0.0
    for asset, menge in flows.items():
        if asset == USDC:
            w += menge
        else:
            m = "SOL" if asset == "SOL" else MINT_MARKT.get(asset)
            if m:
                w += menge * kurse[m].asof(zeit)
    return w


# ---------------------------------------------------------------
# 4. Einstiege zu Trades zusammenfassen und auswerten
# ---------------------------------------------------------------
def analyse():
    ev = perps_events()
    if not ev:
        raise SystemExit("Keine Perps-Transaktionen gefunden – liegt transactions_raw.jsonl im Ordner?")
    start = min(e["zeit"] for e in ev) - pd.Timedelta(minutes=VORLAUF_MIN + 5)
    ende = max(e["zeit"] for e in ev) + pd.Timedelta(minutes=MAX_HALTEN_MIN)
    ende = min(ende, pd.Timestamp.now(tz="UTC"))
    kurse = {m: klines(s, start, ende) for m, s in SYMBOL.items()}

    for e in ev:
        e["usd"] = usd_wert(e["flows"], e["zeit"], kurse)
    ev = [e for e in ev if abs(e["usd"]) >= MIN_BETRAG_USD]

    # Trades je Position bilden: Einzahlungen (usd<0) öffnen/erhöhen, Rückflüsse (usd>0) verkleinern/schließen
    trades = []
    offen = {}
    for e in ev:
        key = (e["markt"], e["seite"])
        t = offen.get(key)
        neu = (t is None or (e["usd"] < 0 and t["rueckfluss"] > 0)
               or (e["zeit"] - t["letzte"]).total_seconds() > NEUER_TRADE_NACH_MIN * 60 and e["usd"] < 0)
        if neu and e["usd"] < 0:
            t = {"markt": e["markt"], "seite": e["seite"], "start": e["zeit"], "letzte": e["zeit"],
                 "einsatz": 0.0, "rueckfluss": 0.0, "ende": None, "erhoehungen": 0}
            trades.append(t)
            offen[key] = t
        if t is None:
            continue
        if e["usd"] < 0:
            t["einsatz"] += -e["usd"]
            t["erhoehungen"] += 1
        else:
            t["rueckfluss"] += e["usd"]
            t["ende"] = e["zeit"]
        t["letzte"] = e["zeit"]

    rows = []
    for t in trades:
        k = kurse[t["markt"]]
        richtung = 1 if t["seite"] == "Long" else -1
        p0 = k.asof(t["start"])
        vor = k[t["start"] - pd.Timedelta(minutes=VORLAUF_MIN):t["start"]]
        vor15 = k[t["start"] - pd.Timedelta(minutes=15):t["start"]]
        ende = t["ende"] or min(t["start"] + pd.Timedelta(minutes=MAX_HALTEN_MIN), k.index[-1])
        nach = k[t["start"]:ende]
        if nach.empty or vor.empty:
            continue                          # keine Kursdaten für diesen Zeitraum
        bewegung = (nach / p0 - 1) * 100 * richtung
        if t["rueckfluss"] == 0:
            # ohne Rückfluss: Beobachtung endet bei der vermuteten Liquidation
            liq = bewegung[bewegung <= -(100 / ANNAHME_HEBEL - 0.2)]
            if not liq.empty:
                ende = liq.index[0]
                nach, bewegung = nach[:ende], bewegung[:ende]
        aus = (k.asof(ende) / p0 - 1) * 100 * richtung
        rows.append({
            "start_utc": t["start"].strftime("%Y-%m-%d %H:%M"),
            "markt": t["markt"], "seite": t["seite"],
            "einsatz_usd": round(t["einsatz"], 2), "rueckfluss_usd": round(t["rueckfluss"], 2),
            "ergebnis_usd": round(t["rueckfluss"] - t["einsatz"], 2),
            "ohne_rueckfluss": t["rueckfluss"] == 0,           # vermutlich liquidiert
            "erhoehungen": t["erhoehungen"],
            "haltedauer_min": round((ende - t["start"]).total_seconds() / 60),
            # VOR dem Einstieg (für Long: Rückgang vom Hoch der letzten 60 Min.)
            "rueckgang_vor_einstieg_60min_pct": round((p0 / vor.max() - 1) * 100, 2) if t["seite"] == "Long"
            else round((p0 / vor.min() - 1) * 100, 2),
            "aenderung_15min_vor_pct": round((p0 / vor15.iloc[0] - 1) * 100, 2) if len(vor15) else None,
            # NACH dem Einstieg (in Richtung der Position, ohne Hebel)
            "bester_kurs_danach_pct": round(bewegung.max(), 2),
            "schlechtester_kurs_danach_pct": round(bewegung.min(), 2),
            "minuten_bis_bester_kurs": int((bewegung.idxmax() - t["start"]).total_seconds() / 60),
            "kurs_bei_ende_pct": round(aus, 2),
            "verschenkt_pct": round(bewegung.max() - aus, 2),
        })
    df = pd.DataFrame(rows)
    df.to_csv(OUT_FILE, index=False)
    return df


# ---------------------------------------------------------------
# 5. Zusammenfassung
# ---------------------------------------------------------------
def zusammenfassung(df):
    longs = df[df["seite"] == "Long"]
    print(f"\nTrades erkannt: {len(df)} (Long {len(longs)}, Short {len(df) - len(longs)})")
    print(f"Märkte: {df['markt'].value_counts().to_dict()}")
    print(f"Ohne Rückfluss (vermutlich liquidiert): {int(df['ohne_rueckfluss'].sum())}")
    print(f"Haltedauer Median: {df['haltedauer_min'].median():.0f} Min.")
    print("\n--- VOR dem Einstieg (Long) ---")
    print(f"Rückgang vom 60-Min.-Hoch, Median: {longs['rueckgang_vor_einstieg_60min_pct'].median():.2f} %")
    print(f"  25 % der Einstiege nach weniger als {longs['rueckgang_vor_einstieg_60min_pct'].quantile(0.75):.2f} %, "
          f"25 % nach mehr als {longs['rueckgang_vor_einstieg_60min_pct'].quantile(0.25):.2f} %")
    print("\n--- NACH dem Einstieg (Kursbewegung ohne Hebel) ---")
    print(f"Bester Kurs danach, Median: +{df['bester_kurs_danach_pct'].median():.2f} % "
          f"(× 50 Hebel ≈ +{df['bester_kurs_danach_pct'].median() * 50:.0f} % auf die Sicherheit)")
    for s in (0.2, 0.3, 0.5, 1.0):
        anteil = (df["bester_kurs_danach_pct"] >= s).mean() * 100
        print(f"  Kurs lief mindestens +{s} % in Trade-Richtung: {anteil:.0f} % der Trades")
    verschenkt = df[(df["bester_kurs_danach_pct"] >= 0.3) & (df["ergebnis_usd"] < 0)]
    print(f"\nTrades, die mindestens +0,3 % im Plus waren und trotzdem mit Verlust endeten: "
          f"{len(verschenkt)} von {len(df)}")
    print(f"Ergebnis aller Trades: {df['ergebnis_usd'].sum():.2f} $")
    print("\n--- Long gegen Short ---")
    for seite, g in df.groupby("seite"):
        print(f"{seite}: {len(g)} Trades, Gewinntrades {int((g['ergebnis_usd'] > 0).sum())}, "
              f"Ergebnis {g['ergebnis_usd'].sum():.2f} $, bester Kurs danach Median +{g['bester_kurs_danach_pct'].median():.2f} %")
    print(f"\nDetails: {OUT_FILE}")


if __name__ == "__main__":
    zusammenfassung(analyse())
