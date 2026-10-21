"""
backtest.py
Prüft die eigene Methode („nach spürbarem Rückgang Long, kurz halten“) auf ALLEN
Rückgängen der letzten Monate – nicht nur auf den eigenen Trades.

Regel, die getestet wird:
  Einstieg:  Kurs fällt um mindestens D % unter das Hoch der letzten 60 Minuten
             (optional nur, wenn der Kurs über dem 24-Stunden-Durchschnitt liegt = Aufwärtstrend)
  Ausstieg:  Ziel +T %  |  Notausstieg −S %  |  spätestens nach H Stunden
  Gebühren:  Öffnen + Schließen (Standard 0,12 % der Positionsgröße) + Schlupf

Ergebnisse OHNE Hebel (Kursbewegung in %). Mit Hebel × Hebel rechnen –
Notausstieg muss dann vor der Liquidation liegen (bei 50× ca. −1,8 %).

Schutz gegen Selbstbetrug: Daten werden geteilt in
  Lernzeit (erste 2/3) → beste Einstellungen suchen
  Prüfzeit (letztes 1/3) → nur prüfen, ob sie dort auch funktionieren

Aufruf:
  python backtest.py              (Standard: SOL, ETH, BTC, letzte 180 Tage)
  python backtest.py --tage 90
  python backtest.py --gebuehr 0.05   (andere Gebühren testen, Kurse kommen aus dem Cache)

Ausgabe:
  backtest_ergebnisse.csv   alle Kombinationen
  Zusammenfassung im Terminal
"""

import os
import sys
import time
import itertools
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------
# 1. Einstellungen
# ---------------------------------------------------------------
MAERKTE = {"SOL": "SOLUSDT", "ETH": "ETHUSDT", "BTC": "BTCUSDT"}
TAGE = int(sys.argv[sys.argv.index("--tage") + 1]) if "--tage" in sys.argv else 180

RUECKGANG = [0.5, 0.75, 1.0, 1.5, 2.0]        # D: Einstieg ab so viel % unter dem 60-Min.-Hoch
ZIEL = [0.3, 0.5, 0.75, 1.0, 1.5]              # T: Gewinnziel in %
NOTAUSSTIEG = [1.0, 1.5, 2.0, 3.0]             # S: Notausstieg in %
TREND_FILTER = [False, True]                    # nur kaufen, wenn Kurs über 24-h-Durchschnitt
MAX_HALTEN_STD = 24                             # H
GEBUEHR = float(sys.argv[sys.argv.index("--gebuehr") + 1]) if "--gebuehr" in sys.argv else 0.12
                                                # % pro Trade (Öffnen + Schließen); Jupiter ≈ 0,12
SCHLUPF = 0.02                                  # % zusätzlicher Preisnachteil
PAUSE_NACH_TRADE_MIN = 15                       # kein sofortiger Wiedereinstieg
HOCH_FENSTER_MIN = 60
TREND_FENSTER_MIN = 24 * 60


# ---------------------------------------------------------------
# 2. Minutenkurse (Binance, öffentlich) mit Cache
# ---------------------------------------------------------------
def lade_kurse(symbol, start, ende):
    cache = f"bt_kurse_{symbol}_{TAGE}t.csv"
    if os.path.exists(cache):
        return pd.read_csv(cache, parse_dates=["zeit"]).set_index("zeit")
    rows, t, ende_ms = [], int(start.timestamp() * 1000), int(ende.timestamp() * 1000)
    print(f"  lade {symbol} …", flush=True)
    while t < ende_ms:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", timeout=30, params={
            "symbol": symbol, "interval": "1m", "startTime": t, "limit": 1000})
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows += [(k[0], float(k[2]), float(k[3]), float(k[4])) for k in data]
        t = data[-1][0] + 60_000
        time.sleep(0.05)
    df = pd.DataFrame(rows, columns=["ms", "high", "low", "close"])
    df["zeit"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df = df.drop(columns="ms").set_index("zeit")
    df.to_csv(cache)
    return df


# ---------------------------------------------------------------
# 3. Ein Durchlauf für eine Einstellung
# ---------------------------------------------------------------
def simuliere(high, low, close, hoch60, trend_ok, d, ziel, stop, mit_trend, von, bis):
    """Gibt Liste der Trade-Ergebnisse (in % ohne Hebel, nach Gebühren) zurück."""
    signal = close <= hoch60 * (1 - d / 100)
    if mit_trend:
        signal &= trend_ok
    kandidaten = np.flatnonzero(signal[von:bis]) + von
    max_h = MAX_HALTEN_STD * 60
    kosten = GEBUEHR + SCHLUPF
    ergebnisse, frei_ab = [], von
    for i in kandidaten:
        if i < frei_ab or i + 1 >= bis:
            continue
        einstieg = close[i]
        j_ende = min(i + 1 + max_h, bis)
        h = high[i + 1:j_ende]
        l = low[i + 1:j_ende]
        tp = np.flatnonzero(h >= einstieg * (1 + ziel / 100))
        sl = np.flatnonzero(l <= einstieg * (1 - stop / 100))
        t_tp = tp[0] if tp.size else np.inf
        t_sl = sl[0] if sl.size else np.inf
        if t_tp == np.inf and t_sl == np.inf:           # Zeit abgelaufen → zum Schlusskurs raus
            k = j_ende - 1
            ergebnis = (close[k] / einstieg - 1) * 100
        elif t_sl <= t_tp:                              # gleiche Minute → vorsichtig: Notausstieg zuerst
            k = i + 1 + int(t_sl)
            ergebnis = -stop
        else:
            k = i + 1 + int(t_tp)
            ergebnis = ziel
        ergebnisse.append(ergebnis - kosten)
        frei_ab = k + PAUSE_NACH_TRADE_MIN
    return ergebnisse


def kennzahlen(e):
    if not e:
        return {"trades": 0, "trefferquote": np.nan, "schnitt": np.nan, "summe": 0.0, "max_rueckgang": 0.0}
    e = np.array(e)
    kurve = np.cumsum(e)
    dd = (np.maximum.accumulate(kurve) - kurve).max()
    return {"trades": len(e), "trefferquote": round((e > 0).mean() * 100, 1),
            "schnitt": round(e.mean(), 3), "summe": round(e.sum(), 1), "max_rueckgang": round(dd, 1)}


# ---------------------------------------------------------------
# 4. Ablauf
# ---------------------------------------------------------------
def main():
    ende = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = ende - timedelta(days=TAGE)
    print(f"Backtest {start:%d.%m.%Y} – {ende:%d.%m.%Y} ({TAGE} Tage), Gebühren {GEBUEHR} %")
    zeilen = []
    for markt, sym in MAERKTE.items():
        df = lade_kurse(sym, start, ende)
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        hoch60 = df["high"].rolling(HOCH_FENSTER_MIN, min_periods=HOCH_FENSTER_MIN).max().shift(1).values
        trend_ok = (df["close"] > df["close"].rolling(TREND_FENSTER_MIN, min_periods=60).mean()).values
        n = len(df)
        grenze = int(n * 2 / 3)
        for d, z, s, tr in itertools.product(RUECKGANG, ZIEL, NOTAUSSTIEG, TREND_FILTER):
            lern = kennzahlen(simuliere(high, low, close, hoch60, trend_ok, d, z, s, tr, HOCH_FENSTER_MIN, grenze))
            pruef = kennzahlen(simuliere(high, low, close, hoch60, trend_ok, d, z, s, tr, grenze, n))
            zeilen.append({"markt": markt, "rueckgang": d, "ziel": z, "notausstieg": s, "trendfilter": tr,
                           **{f"lern_{k}": v for k, v in lern.items()},
                           **{f"pruef_{k}": v for k, v in pruef.items()}})
        print(f"  {markt}: {len(df):,} Minuten getestet".replace(",", "."))

    res = pd.DataFrame(zeilen)
    datei = "backtest_ergebnisse.csv" if GEBUEHR == 0.12 else f"backtest_ergebnisse_gebuehr_{GEBUEHR}.csv"
    res.to_csv(datei, index=False)

    # --- Zusammenfassung ---
    print("\n=== Beste Einstellungen in der LERNZEIT (mind. 30 Trades) – und wie sie in der PRÜFZEIT liefen ===")
    gut = res[res["lern_trades"] >= 30].sort_values("lern_schnitt", ascending=False)
    spalten = ["markt", "rueckgang", "ziel", "notausstieg", "trendfilter",
               "lern_trades", "lern_trefferquote", "lern_schnitt", "pruef_trades", "pruef_trefferquote", "pruef_schnitt"]
    print(gut[spalten].head(10).to_string(index=False))

    print("\n=== Vergleich: kleiner Rückgang (0,5 %), Ziel 0,5 %, Notausstieg 1,5 %, ohne Trendfilter ===")
    vergleich = res[(res.rueckgang == 0.5) & (res.ziel == 0.5) & (res.notausstieg == 1.5) & (~res.trendfilter)]
    print(vergleich[spalten].to_string(index=False))

    beide = res[(res.lern_trades >= 30) & (res.pruef_trades >= 15)]
    stabil = beide[(beide.lern_schnitt > 0) & (beide.pruef_schnitt > 0)]
    print(f"\nEinstellungen, die in Lern- UND Prüfzeit nach Gebühren im Plus waren: {len(stabil)} von {len(beide)}")
    if len(stabil):
        print(stabil.sort_values("pruef_schnitt", ascending=False)[spalten].head(5).to_string(index=False))
    print("\nWerte = Ø Kursbewegung pro Trade in % OHNE Hebel, nach Gebühren. Mit Hebel × Hebel.")
    print(f"Gebühren in diesem Lauf: {GEBUEHR} % + {SCHLUPF} % Schlupf pro Trade")
    print(f"Details: {datei}")


if __name__ == "__main__":
    main()
