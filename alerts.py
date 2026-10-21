"""
alerts.py
Prüft den aktuellen Stand (positions.py) gegen feste Risikoregeln
und schickt Warnungen an den Telegram-Bot. Keine KI – nur Python.

Regeln nach professionellem Risikomanagement (Risiko pro Trade bis zum Stop,
Tages- und Gesamtverlustgrenzen wie bei Prop-Firmen, Pause nach Verlusten)
ergänzt um Regeln gegen typische Fehler beim Hebel-Trading (zu hoher Hebel,
Gewinne nicht sichern, nach Gewinnen alles neu einsetzen, Nachschießen nach Verlusten).

Aufruf:
  python alerts.py          → einmal prüfen (später jede Minute über n8n)
  python alerts.py --test   → Testnachricht an Telegram

Dateien, die entstehen:
  alert_state.json   Gedächtnis zwischen den Läufen
  trades.csv         Journal aller geschlossenen Trades
  equity.csv         Vermögensverlauf (für den Tagesbericht)
"""

import os
import re
import math
import sys
import csv
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import positions

# ---------------------------------------------------------------
# 1. Regeln (hier anpassen)
# ---------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
START_KAPITAL = float(os.getenv("START_KAPITAL") or 0)   # leer = Stand beim ersten Start

RISIKO_PRO_TRADE = 0.02      # max. Verlust bis zum Stop: 2 % des Vermögens
RISIKO_NACH_VERLUST = 0.01   # nach einem Verlusttrade: nächster Trade nur halbes Risiko
MAX_HEBEL = 10               # Warnung ab diesem Hebel
KRITISCHER_HEBEL = 25        # dringende Warnung
STOP_LOSS_ERINNERUNG = False # bewusst ohne Stop-Loss → keine Erinnerungen (True = wieder an)
SL_PFLICHT_MIN = 2           # so viele Minuten nach dem Öffnen muss ein Stop-Loss stehen
SL_ERINNERUNG_MIN = 60       # danach Erinnerung alle 60 Minuten
# „👉 Regel sagt“ – nur Empfehlung, es wird nichts ausgeführt
GEWINN_TEIL_SICHERN = 10     # ab +10 % des Einsatzes: Hälfte schließen
GEWINN_GANZ_SICHERN = 25     # ab +25 %: Rest schließen / Take-Profit
GEWINN_SCHMILZT = 0.5        # Hälfte des höchsten Gewinns wieder weg → schließen
VERLUST_SCHLIESSEN = -50     # ab −50 %: schließen, nicht bis zur Liquidation warten
LIQ_SCHLIESSEN = 1.0         # Liquidation ≤ 1 % entfernt: schließen, nichts nachlegen
SCHWANKUNG_FENSTER_MIN = 60  # Schwankung der letzten Stunde als Maßstab für den Hebel
PNL_SCHRITT_GEWINN = 5      # Meldung bei jedem +5 % des Einsatzes
PNL_PUFFER_GEWINN = 2        # Gewinnstufe gilt erst als verlassen, wenn 2 Punkte darunter
PNL_PUFFER_VERLUST = 3       # Verluststufe gilt erst als verlassen, wenn 3 Punkte darüber
PNL_SCHRITT_VERLUST = 10     # Meldung bei jedem −10 % des Einsatzes
PREIS_FENSTER_MIN = 10       # Kursbewegung über die letzten 10 Minuten …
PREIS_BEWEGUNG = 0.25        # … Meldung ab 0,25 % Bewegung (beide Richtungen)
PREIS_PAUSE_MIN = 10         # gleiche Kursmeldung frühestens nach 10 Minuten erneut
STOP_MAX_ANTEIL_LIQ = 0.5    # Stop höchstens halb so weit weg wie die Liquidation
LIQ_STUFEN = [5.0, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5, 0.25]  # Warnung bei so viel % Abstand zur Liquidation

TAGESVERLUST_MAX = 0.06      # Tagesstopp bei −6 % (= drei volle Verlusttrades)
MAX_TRADES_TAG = 5           # mehr Trades = Überhandeln
VERLUSTSERIE = 2             # nach zwei Verlusten hintereinander …
PAUSE_NACH_SERIE_MIN = 30    # … 30 Minuten Pause
MAX_DRAWDOWN = 0.20          # −20 % vom Höchststand → drei Tage Pause
GROSSER_TAGESGEWINN = 0.50   # +50 % an einem Tag → sichern und Pause
HOCH_SCHRITT = 0.10          # Meldung „Gewinn sichern“ alle +10 %
MIN_TRANSFER_USD = 1.0       # kleinere Bewegungen gelten nicht als Ein-/Auszahlung

STATE_FILE = "alert_state.json"
TRADES_FILE = "trades.csv"
EQUITY_FILE = "equity.csv"
WARN_FILE = "warnungen.csv"
BERLIN = ZoneInfo("Europe/Berlin")


# ---------------------------------------------------------------
# 2. Hilfsfunktionen
# ---------------------------------------------------------------
def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    r.raise_for_status()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_csv(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def usd(x):
    return f"{x:,.2f} $".replace(",", "X").replace(".", ",").replace("X", ".").replace("-", "−")


def wert(p):
    # Wert wie Jupiter „Value (Inc. Fees)“ (inkl. Eröffnungs-, Schließ- und Leihgebühr)
    return p.get("wert_jupiter_usd", p["nettowert_bei_schliessen_usd"])


def pz(x, d=2, plus=True):
    # Prozentzahl deutsch: Komma, echtes Minuszeichen
    s = f"{x:+.{d}f}" if plus else f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def jupiter_pnl(p):
    # gleiche Größen wie Jupiter: PnL = Value (inkl. Gebühren) − Collateral
    pnl = wert(p) - p["sicherheit_usd"]
    pct = pnl / p["sicherheit_usd"] * 100 if p["sicherheit_usd"] > 0 else 0
    return pnl, pct


def ampel(p):
    _, pct = jupiter_pnl(p)
    liq = p["abstand_liq_prozent"]
    if liq <= 1 or pct <= -50:
        return "🔴"
    if liq <= 3 or pct <= -20:
        return "🟠"
    return "🟢" if pct > 0 else "🟡"


def kursaenderung(st, p, now):
    # Kursbewegung der letzten PREIS_FENSTER_MIN Minuten in % (None, wenn noch keine Daten)
    hist = [(t, x) for t, x in (st or {}).get("preise", {}).get(p["markt"], [])
            if now - t <= PREIS_FENSTER_MIN * 60]
    if not hist:
        return None
    return (p["preis_jetzt"] - hist[0][1]) / hist[0][1] * 100


def kopf(p, aktion=None, bewegung=None):
    # Das Wichtigste zuerst und fett – steht auch in der Handy-Vorschau:
    # 1. Position: Gewinn/Verlust und Abstand zur Liquidation
    # 2. Kurs jetzt und wohin er sich bewegt
    # 3. was die Regel sagt
    pnl, pct = jupiter_pnl(p)
    z = (f"{ampel(p)} <b>{p['markt']}-{p['seite']} {pz(pct, 0)} % "
         f"({'+' if pnl >= 0 else ''}{usd(pnl)}) · Liq {pz(p['abstand_liq_prozent'], 2, False)} %</b>")
    kurs = f"<b>Kurs {usd(p['preis_jetzt'])}"
    if bewegung is not None:
        pfeil = "🔺" if bewegung > 0 else "🔻" if bewegung < 0 else "▪️"
        kurs = f"{pfeil} {kurs} ({pz(bewegung)} % in {PREIS_FENSTER_MIN} Min.)</b>"
    else:
        kurs = f"▪️ {kurs}</b>"
    z += "\n" + kurs
    if aktion:
        z += f"\n👉 <b>REGEL: {aktion}</b>"
    return z


def details(p, now):
    zeit = datetime.fromtimestamp(now, BERLIN).strftime("%H:%M:%S")
    # Dein Geld = Sicherheit + Eröffnungsgebühr (die Jupiter beim Öffnen von deinem Geld abzieht)
    eigen = p["sicherheit_usd"] + p.get("eroeffnungsgebuehr_geschaetzt_usd", 0)
    hebel = str(p["hebel"]).replace(".", ",")
    return (f"Dein Geld {usd(eigen)} → jetzt {usd(wert(p))}\n"
            f"Größe mit Hebel {usd(p['groesse_usd'])} ({hebel}×)\n"
            f"Einstieg {usd(p['einstieg'])} · Liq-Preis {usd(p['liquidation'])}\n"
            f"🕒 {zeit}")


def statuszeile(p, now, st=None):
    # für /status (st = alert_state, für die Kursbewegung)
    return kopf(p, bewegung=kursaenderung(st, p, now)) + "\n" + details(p, now)


def amounts(snap):
    # Token-Mengen im Wallet (nicht Werte – Werte schwanken mit dem Kurs)
    return {h["mint"]: h["menge"] for h in snap["wallet_bestaende"]}


def flow_details(snap, before, st):
    # Wert der Mengenänderung je Token seit dem letzten Lauf.
    # Token, die jetzt auf 0 stehen, fehlen im Snapshot → letzter bekannter Preis aus dem Gedächtnis
    # (sonst zählt z. B. „alle USDC in ETH getauscht“ fälschlich als Einzahlung).
    price = dict(st.get("token_preise", {}))
    price.update({h["mint"]: h["preis_usd"] for h in snap["wallet_bestaende"]})
    now = amounts(snap)
    diffs = {m: (now.get(m, 0) - before.get(m, 0)) * price[m]
             for m in set(now) | set(before) if m in price}
    return {m: v for m, v in diffs.items() if abs(v) >= 0.01}


# Basis-Geld (kein Trade): USDC, USDT und SOL (für Gebühren). Alles andere im Wallet = Spot-Trade.
BASIS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",    # USDC
         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",    # USDT
         "So11111111111111111111111111111111111111112"}    # SOL


KLEINSTBETRAG = 1.0     # Token unter 1 $ ohne bekannten Kauf = meist Airdrop/Spam → nur als Summe zeigen


def kurs(x):
    # Kurs mit genug Nachkommastellen, damit kleine Preise nicht als 0,00 $ erscheinen
    if x <= 0 or x >= 1:
        return usd(x)
    stellen = min(10, 2 - int(math.floor(math.log10(x))) + 1)
    t = f"{x:.{stellen}f}".rstrip("0")
    t += "0" * max(0, 2 - len(t.split(".")[1]))      # mindestens 2 Nachkommastellen
    return t.replace(".", ",") + " $"


def spot_zeilen(snap, st):
    """Wallet getrennt: Basis-Geld und Spot-Trades (mit Ergebnis seit Kauf)."""
    einst = (st or {}).get("spot_einstieg", {})
    basis, trades, klein = [], [], 0.0
    for h in snap["wallet_bestaende"]:
        z = f" · {h['token']} {menge_txt(h['menge']).lstrip('+')} = {usd(h['wert_usd'])}"
        if h["mint"] in BASIS:
            basis.append(z)
            continue
        e = einst.get(h["mint"])
        if not (e and e["kosten"] > 0) and h["wert_usd"] < KLEINSTBETRAG:
            klein += h["wert_usd"]
            continue
        if e and e["kosten"] > 0:
            diff = h["wert_usd"] - e["kosten"]
            z = (f"{'🟢' if diff >= 0 else '🔴'} <b>{h['token']} {pz(diff / e['kosten'] * 100, 1)} % "
                 f"({'+' if diff >= 0 else ''}{usd(diff)})</b>\n   gekauft für {usd(e['kosten'])} → jetzt "
                 f"{usd(h['wert_usd'])} · {menge_txt(h['menge']).lstrip('+')} {h['token']} · Kurs {kurs(h['preis_usd'])}")
        else:
            z = (f"⚪ <b>{h['token']} = {usd(h['wert_usd'])}</b> (Kaufpreis unbekannt)\n"
                 f"   {menge_txt(h['menge']).lstrip('+')} {h['token']} · Kurs {kurs(h['preis_usd'])}")
        trades.append(z)
    zeilen = ["\nWallet (Basis):"] + (basis or [" · leer"])
    if trades:
        zeilen += ["\nSpot-Trades (ohne Hebel):"] + trades
    if klein > 0:
        zeilen.append(f"Kleinstbeträge (unter {usd(KLEINSTBETRAG)}): {usd(klein)}")
    return zeilen


def menge_txt(x):
    # Token-Menge ohne „2e-05“-Schreibweise, deutsch formatiert
    s = f"{x:+,.8f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".").replace("-", "−")


# ---------------------------------------------------------------
# 3. Regeln prüfen
# ---------------------------------------------------------------
def check(snap, st):
    msgs = []
    now = snap["zeit"]
    equity = snap["gesamtvermoegen_usd"]
    today = datetime.fromtimestamp(now, BERLIN).date().isoformat()
    first_run = not st

    st.setdefault("einzahlungen_netto", 0.0)
    st.setdefault("positionen", {})
    st.setdefault("liq_stufe", {})
    st.setdefault("sl_erinnert", {})
    st.setdefault("verlust_serie", 0)
    st.setdefault("pause_bis", 0)
    st.setdefault("drawdown_pause_bis", 0)
    st.setdefault("letzter_trade_verlust", False)
    st.setdefault("mengen", amounts(snap))
    st.setdefault("pnl_stufe", {})
    st.setdefault("preise", {})
    st.setdefault("preis_gemeldet", {})
    st.setdefault("pnl_max", {})
    st.setdefault("regel", {})          # welche Empfehlung pro Position schon gesendet wurde
    st.setdefault("preise_lang", {})

    current = {p["id"]: p for p in snap["offene_positionen"]}
    prev = st["positionen"]

    # --- Ein-/Auszahlungen erkennen: Mengen ändern sich, ohne dass ein Trade passiert ---
    trade_event = (set(current) != set(prev)) or any(
        abs(current[i]["sicherheit_usd"] - prev[i]["sicherheit"]) > 0.01
        for i in current if i in prev)
    diffs = flow_details(snap, st["mengen"], st)
    flow = sum(diffs.values())
    rein = sum(v for v in diffs.values() if v > 0)
    raus = -sum(v for v in diffs.values() if v < 0)
    tausch = (rein >= MIN_TRANSFER_USD and raus >= MIN_TRANSFER_USD
              and abs(flow) < max(MIN_TRANSFER_USD, 0.1 * max(rein, raus)))
    if not first_run and not trade_event and tausch:
        # Ein Token raus, ein anderer rein, Wert fast gleich → Tausch (z. B. USDC → ETH), kein Geld von außen
        namen = st.get("token_namen", {})
        namen.update({h["mint"]: h["token"] for h in snap["wallet_bestaende"]})
        neu_m, alt_m = amounts(snap), st["mengen"]
        def teil(m):
            return f"{menge_txt(neu_m.get(m, 0) - alt_m.get(m, 0))} {namen.get(m, m[:6] + '…')}"
        weg = " + ".join(teil(m) for m, v in diffs.items() if v < 0)
        dazu = " + ".join(teil(m) for m, v in diffs.items() if v > 0)
        text = f"🔄 <b>Tausch im Wallet: {weg} → {dazu}</b>\nWert ca. {usd(raus)} – kein neues Geld, ohne Hebel"
        # Spot-Trades merken: Kaufwert je Token (für „seit Kauf“ in /status und das Ergebnis beim Verkauf)
        einst = st.setdefault("spot_einstieg", {})
        for m, v in diffs.items():
            if m in BASIS:
                continue
            if v > 0:                                    # gekauft → Kaufwert addieren
                e = einst.setdefault(m, {"kosten": 0.0})
                e["kosten"] += v
            elif m in einst:                             # verkauft → Ergebnis anteilig
                anteil = min(1.0, (alt_m.get(m, 0) - neu_m.get(m, 0)) / alt_m[m]) if alt_m.get(m) else 1.0
                kosten = einst[m]["kosten"] * anteil
                ergebnis = -v - kosten
                text += (f"\n{'✅' if ergebnis >= 0 else '❌'} <b>Ergebnis {namen.get(m, '')}: "
                         f"{'+' if ergebnis >= 0 else ''}{usd(ergebnis)} ({pz(ergebnis / kosten * 100, 1)} %)</b>")
                einst[m]["kosten"] -= kosten
                if anteil >= 0.999:
                    einst.pop(m)
        msgs.append(text)
    elif not first_run and not trade_event and abs(flow) >= MIN_TRANSFER_USD:
        st["einzahlungen_netto"] += flow
        if flow > 0:
            text = f"➕ <b>Einzahlung erkannt: {usd(flow)}</b>"
            if st.get("tag") == today and st.get("verluste_heute", 0) > 0:
                text += "\n👉 <b>ACHTUNG: Nachzahlung am Verlusttag</b> – erhöht das Risiko weiterer Verluste."
            msgs.append(text)
        else:
            msgs.append(f"➖ Auszahlung erkannt: {usd(-flow)}. Gut – gesichertes Geld.")

    # Vermögen ohne Einzahlungen = echtes Trading-Ergebnis
    netto = equity - st["einzahlungen_netto"]

    # --- Tageswechsel ---
    if st.get("tag") != today:
        st.update({"tag": today, "tag_start": netto, "verluste_heute": 0, "trades_heute": 0,
                   "tagesstopp": False, "pause_gemeldet": False, "uebertrading_gemeldet": False})
    st.setdefault("start_kapital", START_KAPITAL or netto)
    st.setdefault("hoch", netto)
    st.setdefault("hoch_gemeldet", netto)
    st.setdefault("verdopplung_gemeldet", False)

    gesperrt = []
    if st["tagesstopp"]:
        gesperrt.append("Tagesstopp")
    if now < st["pause_bis"]:
        gesperrt.append("Pause nach Verlustserie")
    if now < st["drawdown_pause_bis"]:
        gesperrt.append("Drawdown-Pause")

    # --- Offene Positionen ---
    # Pro Position EINE Nachricht:
    #   Kopf (fett):  Ampel · Markt · PnL · Liq-Abstand
    #                 👉 REGEL: was jetzt zu tun ist
    #   Gründe:       je eine kurze Zeile, warum gemeldet wird
    #   Details:      Wert, Mark, Liq, Einstieg, Uhrzeit
    pos_nachrichten, protokoll = [], []
    st.setdefault("pnl_gemeldet", {})
    for pid, p in current.items():
        gruende = []
        name = f"{p['markt']}-{p['seite']} ({str(p['hebel']).replace('.', ',')}×)"
        alt = prev.get(pid)
        neu = alt is None
        vergroessert = alt is not None and p["sicherheit_usd"] > alt["sicherheit"] + 0.01
        abstand = p["abstand_liq_prozent"]
        aktionen = []                       # (Dringlichkeit, Text) – die dringendste kommt in den Kopf

        if neu or vergroessert:
            st["trades_heute"] += 1
            if gesperrt:
                gruende.append(f"⛔ {name} geöffnet trotz {', '.join(gesperrt)} – Regel gebrochen")
                aktionen.append((4, "SCHLIESSEN"))
            if st["trades_heute"] > MAX_TRADES_TAG and not st["uebertrading_gemeldet"]:
                st["uebertrading_gemeldet"] = True
                gruende.append(f"🔁 Schon {st['trades_heute']} Trades heute (max. {MAX_TRADES_TAG}) – "
                               f"Überhandeln")
                aktionen.append((3, "PAUSE BIS MORGEN"))
            if p["hebel"] >= KRITISCHER_HEBEL:
                gruende.append(f"🚨 Hebel {str(p['hebel']).replace('.', ',')}× – schon {pz(abstand, 1, False)} % Bewegung "
                               f"= Liquidation (Regel: max. {MAX_HEBEL}×)")
            elif p["hebel"] > MAX_HEBEL:
                gruende.append(f"⚠️ Hebel über {MAX_HEBEL}×")

        # Risiko bis zum Stop im Verhältnis zum Vermögen
        grenze = RISIKO_NACH_VERLUST if st["letzter_trade_verlust"] else RISIKO_PRO_TRADE
        risiko = p["risiko_bis_stop_usd"]
        erlaubt = equity * grenze
        if p.get("stop_loss_vor_liquidation") and risiko > erlaubt and (neu or vergroessert
                                                                       or not alt.get("risiko_gemeldet")):
            st.setdefault("risiko_gemeldet", {})[pid] = True
            # Profi-Formel: Positionsgröße = erlaubtes Risiko / Stop-Abstand
            ziel = erlaubt / (p["stop_abstand_prozent"] / 100)
            gruende.append(f"⚖️ Risiko bis Stop {usd(risiko)} = {pz(risiko / equity * 100, 0, False)} % des Vermögens "
                           f"(Regel: {pz(grenze * 100, 0, False)} % = {usd(erlaubt)}). Passende Größe: ca. {usd(ziel)}")
        stop_neu = neu or vergroessert or not alt.get("mit_stop")
        if p.get("stop_anteil_am_liq_abstand") and p["stop_anteil_am_liq_abstand"] > STOP_MAX_ANTEIL_LIQ \
                and stop_neu:
            gruende.append(f"📏 Stop liegt zu nah an der Liquidation "
                           f"({pz(p['stop_anteil_am_liq_abstand'] * 100, 0, False)} % des Abstands) – Hebel senken")

        # Stop-Loss-Pflicht
        if snap["tpsl_abrufbar"] and STOP_LOSS_ERINNERUNG:
            problem = None
            if not p["stop_loss_gesetzt"]:
                problem = "kein Stop-Loss"
            elif not p["stop_loss_vor_liquidation"]:
                problem = "Stop-Loss liegt hinter der Liquidation – löst nie aus"
            last = st["sl_erinnert"].get(pid)
            faellig = p["offen_seit_min"] >= SL_PFLICHT_MIN and (
                last is None or now - last >= SL_ERINNERUNG_MIN * 60)
            if problem and faellig:
                st["sl_erinnert"][pid] = now
                gruende.append(f"🛑 {problem} – ganze Sicherheit ({usd(risiko)}) im Risiko")
            if not problem:
                st["sl_erinnert"].pop(pid, None)

        # --- Gewinn/Verlust der Position in Schritten (bezogen auf den Einsatz) ---
        einsatz = max(p["sicherheit_usd"], (alt or {}).get("einsatz", 0))
        pnl = wert(p) - einsatz
        pnl_pct = pnl / einsatz * 100 if einsatz > 0 else 0

        def pnl_stufe(x):
            return int(x // PNL_SCHRITT_GEWINN) if x >= 0 else -int(-x // PNL_SCHRITT_VERLUST)

        stufe = pnl_stufe(pnl_pct)
        stufe_alt = st["pnl_stufe"].get(pid, 0)
        # Puffer gegen Pendeln an der Grenze: Zurück Richtung null zählt erst,
        # wenn die Grenze deutlich überschritten ist (Verlust 3 Punkte, Gewinn 2 Punkte)
        if stufe > stufe_alt and stufe_alt < 0 and pnl_stufe(pnl_pct - PNL_PUFFER_VERLUST) <= stufe_alt:
            stufe = stufe_alt
        if stufe < stufe_alt and stufe_alt > 0 and pnl_stufe(pnl_pct + PNL_PUFFER_GEWINN) >= stufe_alt:
            stufe = stufe_alt
        if stufe != stufe_alt and not neu:
            if stufe > stufe_alt:
                art = "📈 Gewinn wächst" if pnl_pct >= 0 else "📈 Verlust wird kleiner"
            else:
                art = "📉 Gewinn schrumpft" if pnl_pct >= 0 else "📉 Verlust wächst"
            zuletzt = st["pnl_gemeldet"].get(pid)
            gruende.append(art + (f" (zuletzt gemeldet {pz(zuletzt, 0)} %)" if zuletzt is not None else ""))
        st["pnl_stufe"][pid] = stufe

        # --- 👉 Regel sagt (Gewinne rechtzeitig sichern, Verluste nicht laufen lassen) ---
        hoch = max(st["pnl_max"].get(pid, pnl_pct), pnl_pct)
        st["pnl_max"][pid] = hoch
        hoch_usd = einsatz * hoch / 100
        gesendet = st["regel"].setdefault(pid, [])

        def regel(key, text):
            # Begründung nur einmal schicken – die Empfehlung steht trotzdem in jedem Kopf
            if key not in gesendet:
                gesendet.append(key)
                gruende.append(text)

        schon_verkleinert = p["sicherheit_usd"] < einsatz - 0.01
        if pnl_pct >= GEWINN_GANZ_SICHERN:
            aktionen.append((3, "REST SCHLIESSEN – Gewinn sichern"))
            regel("ganz", f"💰 Gewinn über +{GEWINN_GANZ_SICHERN} % – Rest schließen oder Take-Profit, "
                          f"Gewinn sichern")
        elif pnl_pct >= GEWINN_TEIL_SICHERN and not schon_verkleinert:
            aktionen.append((2, "HÄLFTE SCHLIESSEN (Close → 50 %)"))
            regel("teil", f"💰 Gewinn über +{GEWINN_TEIL_SICHERN} % – Hälfte sichern")
        if hoch >= GEWINN_TEIL_SICHERN and 0 < pnl_pct <= hoch * GEWINN_SCHMILZT:
            aktionen.append((4, "SCHLIESSEN – Rest-Gewinn mitnehmen"))
            regel("schmilzt", f"⚠️ Gewinn schmilzt – warst bei {pz(hoch, 0)} % (+{usd(hoch_usd)})")
        if hoch >= GEWINN_TEIL_SICHERN and pnl_pct <= 0:
            aktionen.append((4, "SCHLIESSEN – nicht auf Rückkehr warten"))
            regel("weg", f"❗ Gewinn komplett weg – warst bei {pz(hoch, 0)} % (+{usd(hoch_usd)})")
        if pnl_pct <= VERLUST_SCHLIESSEN:
            aktionen.append((4, "SCHLIESSEN"))
            regel("verlust", f"🛑 Verlust über {pz(VERLUST_SCHLIESSEN, 0)} % – "
                             f"weiteres Warten endet oft in der Liquidation")
        if abstand <= LIQ_SCHLIESSEN:
            aktionen.append((5, "SCHLIESSEN – nichts nachlegen"))
            regel("liq", f"🚨 Liquidation höchstens {pz(LIQ_SCHLIESSEN, 0, False)} % entfernt – "
                         f"keine Sicherheit nachlegen")

        # Hebel passt nicht zur aktuellen Schwankung?
        lang = [x for t, x in st["preise_lang"].get(p["markt"], []) if now - t <= SCHWANKUNG_FENSTER_MIN * 60]
        if len(lang) >= 30:
            spanne = (max(lang) - min(lang)) / p["preis_jetzt"] * 100
            if abstand < spanne:
                aktionen.append((2, "POSITION VERKLEINERN"))
                regel("schwankung", f"📏 {p['markt']} schwankte in der letzten Stunde um "
                                    f"{pz(spanne, 2, False)} % – mehr als dein Abstand zur Liquidation")

        # Empfehlungen wieder freigeben, wenn sich die Lage klar geändert hat
        if pnl_pct <= 0:
            for k in ("teil", "ganz", "schmilzt"):
                if k in gesendet:
                    gesendet.remove(k)
            if pnl_pct > VERLUST_SCHLIESSEN / 2 and "verlust" in gesendet:
                gesendet.remove("verlust")
        if pnl_pct > 0 and "weg" in gesendet:
            gesendet.remove("weg")
        if abstand > LIQ_SCHLIESSEN * 2 and "liq" in gesendet:
            gesendet.remove("liq")

        # --- Schnelle Kursbewegung (beide Richtungen) ---
        hist = [(t, x) for t, x in st["preise"].get(p["markt"], []) if now - t <= PREIS_FENSTER_MIN * 60]
        if hist:
            bewegung = (p["preis_jetzt"] - hist[0][1]) / hist[0][1] * 100
            key = f"{pid}:{'hoch' if bewegung > 0 else 'runter'}"   # Pause getrennt je Richtung
            last = st["preis_gemeldet"].get(key, 0)
            if abs(bewegung) >= PREIS_BEWEGUNG and now - last >= PREIS_PAUSE_MIN * 60:
                st["preis_gemeldet"][key] = now
                richtung = "fällt" if bewegung < 0 else "steigt"
                gut = (bewegung > 0) == (p["seite"] == "Long")
                gruende.append(f"{'🟢' if gut else '⚡'} {p['markt']} {richtung} {pz(bewegung)} % in "
                               f"{PREIS_FENSTER_MIN} Min. – {'für' if gut else 'gegen'} dich "
                               f"({usd(hist[0][1])} → {usd(p['preis_jetzt'])})")

        # Abstand zur Liquidation, gestaffelt
        stufe_alt = st["liq_stufe"].get(pid, 99)
        erreicht = [s for s in LIQ_STUFEN if abstand <= s]
        if erreicht and min(erreicht) < stufe_alt:
            st["liq_stufe"][pid] = min(erreicht)
            if not any(g.startswith("🚨 Liquidation höchstens") for g in gruende):   # nicht doppelt
                gruende.append(f"🚨 Abstand zur Liquidation jetzt unter {min(erreicht):g} %".replace(".", ","))
        elif abstand > stufe_alt + 1:
            st["liq_stufe"][pid] = min(erreicht, default=99)

        # Eine Nachricht: Kopf · Gründe · Details
        if gruende:
            aktion = max(aktionen)[1] if aktionen else None
            pos_nachrichten.append(kopf(p, aktion, kursaenderung(st, p, now)) + "\n\n" + "\n".join(gruende)
                                   + "\n──────────\n" + details(p, now))
            protokoll += gruende
            _, st["pnl_gemeldet"][pid] = jupiter_pnl(p)

    # Positionsmeldungen zuerst – sie sind am dringendsten
    msgs[:0] = pos_nachrichten

    # --- Geschlossene / verkleinerte Positionen: Ergebnis ins Journal ---
    for pid, alt in prev.items():
        if pid in current:
            continue
        # Näherung: was ins Wallet zurückkam minus Einsatz
        ergebnis = flow - alt["einsatz"] if not first_run else 0
        append_csv(TRADES_FILE, {
            "geschlossen": datetime.fromtimestamp(now, BERLIN).isoformat(timespec="minutes"),
            "position": pid, "einsatz_usd": round(alt["einsatz"], 2),
            "ergebnis_usd": round(ergebnis, 2), "max_hebel": alt.get("max_hebel"),
            "mit_stop": alt.get("mit_stop"), "liquidiert_vermutet": flow < 0.05 * alt["einsatz"]})
        if ergebnis < 0:
            st["verluste_heute"] += 1
            st["verlust_serie"] += 1
            st["letzter_trade_verlust"] = True
            text = (f"📉 <b>{pid.split('-')[0]}-Position zu: ca. {usd(ergebnis)}</b>\n"
                    f"👉 <b>REGEL: HEUTE KEIN NEUES GELD, KEINE NEUE POSITION</b>")
            if st["verlust_serie"] >= VERLUSTSERIE:
                st["pause_bis"] = now + PAUSE_NACH_SERIE_MIN * 60
                text += (f"\n\n{st['verlust_serie']} Verluste in Folge → {PAUSE_NACH_SERIE_MIN} Min. Pause. "
                         f"Nächster Trade nur mit halbem Risiko.")
            msgs.append(text)
        else:
            st["verlust_serie"] = 0
            st["letzter_trade_verlust"] = False
            msgs.append(f"📈 <b>{pid.split('-')[0]}-Position zu: ca. +{usd(ergebnis)}</b>\n"
                        f"👉 <b>REGEL: HÄLFTE ({usd(ergebnis / 2)}) SICHERN</b>")
        st["liq_stufe"].pop(pid, None)
        st["sl_erinnert"].pop(pid, None)
        st.get("risiko_gemeldet", {}).pop(pid, None)
        st["pnl_gemeldet"].pop(pid, None)

    # --- Tagesregeln ---
    tag_aenderung = (netto - st["tag_start"]) / st["tag_start"] if st["tag_start"] > 0 else 0
    if not st["tagesstopp"] and tag_aenderung <= -TAGESVERLUST_MAX:
        st["tagesstopp"] = True
        msgs.append(f"⛔ <b>TAGESSTOPP: heute {pz(tag_aenderung * 100, 0)} %</b>\n👉 <b>REGEL: KEINE NEUEN TRADES BIS MORGEN</b>")
    if not st["pause_gemeldet"] and tag_aenderung >= GROSSER_TAGESGEWINN:
        st["pause_gemeldet"] = True
        msgs.append(f"✋ <b>Heute {pz(tag_aenderung * 100, 0)} %</b>\n👉 <b>REGEL: GEWINN SICHERN, 24 STUNDEN PAUSE</b>\n\n"
                    f"Nach sehr großen Tagesgewinnen steigt die Gefahr, zu viel zu riskieren.")

    # --- Höchststand, Drawdown, Gewinne sichern ---
    st["hoch"] = max(st["hoch"], netto)
    drawdown = (st["hoch"] - netto) / st["hoch"] if st["hoch"] > 0 else 0
    if drawdown >= MAX_DRAWDOWN and now >= st["drawdown_pause_bis"] and not st.get("drawdown_gemeldet"):
        st["drawdown_gemeldet"] = True
        st["drawdown_pause_bis"] = now + 3 * 86400
        msgs.append(f"🧯 <b>{pz(drawdown * 100, 0, False)} % unter dem Höchststand</b>\n👉 <b>REGEL: DREI TAGE PAUSE</b>, Strategie prüfen")
    if drawdown < MAX_DRAWDOWN / 2:
        st["drawdown_gemeldet"] = False

    if not st["verdopplung_gemeldet"] and netto >= 2 * st["start_kapital"] > 0:
        st["verdopplung_gemeldet"] = True
        msgs.append(f"💰 <b>Verdoppelt: {usd(netto)}</b>\n👉 <b>REGEL: STARTKAPITAL "
                    f"{usd(st['start_kapital'])} SICHERN</b>")
    if netto >= st["hoch_gemeldet"] * (1 + HOCH_SCHRITT):
        zuwachs = netto - st["hoch_gemeldet"]
        st["hoch_gemeldet"] = netto
        msgs.append(f"🏔️ <b>Neuer Höchststand: {usd(netto)}</b>\n👉 <b>REGEL: "
                    f"{usd(zuwachs / 2)} SICHERN</b> (Hälfte des Zuwachses)")

    # --- Stand für den nächsten Lauf merken ---
    st["positionen"] = {pid: {
        "sicherheit": p["sicherheit_usd"],
        "einsatz": max(p["sicherheit_usd"], prev.get(pid, {}).get("einsatz", 0)),
        "max_hebel": max(p["hebel"], prev.get(pid, {}).get("max_hebel", 0)),
        "mit_stop": bool(p.get("stop_loss_vor_liquidation")),
        "risiko_gemeldet": st.get("risiko_gemeldet", {}).get(pid, False),
    } for pid, p in current.items()}
    st["mengen"] = amounts(snap)
    # Preise und Namen merken – für Token, die später auf 0 fallen und dann im Snapshot fehlen
    st.setdefault("token_preise", {}).update({h["mint"]: h["preis_usd"] for h in snap["wallet_bestaende"]})
    st.setdefault("token_namen", {}).update({h["mint"]: h["token"] for h in snap["wallet_bestaende"]})
    for p in snap["offene_positionen"]:
        h = st["preise"].get(p["markt"], []) + [(now, p["preis_jetzt"])]
        st["preise"][p["markt"]] = [(t, x) for t, x in h if now - t <= PREIS_FENSTER_MIN * 60]
    for p in snap["offene_positionen"]:
        h = st["preise_lang"].get(p["markt"], []) + [(now, p["preis_jetzt"])]
        st["preise_lang"][p["markt"]] = [(t, x) for t, x in h if now - t <= SCHWANKUNG_FENSTER_MIN * 60]
    for pid in list(st["pnl_stufe"]):
        if pid not in current:
            st["pnl_stufe"].pop(pid)
            st["pnl_max"].pop(pid, None)
            st["regel"].pop(pid, None)
            for key in [k for k in st["preis_gemeldet"] if k.startswith(pid + ":")]:
                st["preis_gemeldet"].pop(key)

    append_csv(EQUITY_FILE, {"zeit": datetime.fromtimestamp(now, BERLIN).isoformat(timespec="minutes"),
                             "vermoegen_usd": equity, "netto_ohne_einzahlungen_usd": round(netto, 2),
                             "offene_positionen": len(current)})
    # Jede Warnung protokollieren (für den Tagesbericht) – ohne Fett-Markierung,
    # bei Positionen jeder Grund einzeln (erstes Zeichen = Art der Warnung)
    andere = [re.sub(r"</?b>", "", m) for m in msgs if m not in pos_nachrichten]
    for m in protokoll + andere:
        append_csv(WARN_FILE, {"zeit": datetime.fromtimestamp(now, BERLIN).isoformat(timespec="minutes"),
                               "typ": m.split(" ")[0], "text": m.replace("\n", " ")})
    return msgs


# ---------------------------------------------------------------
# 4. Ablauf
# ---------------------------------------------------------------
def main():
    if "--test" in sys.argv:
        send("✅ Wallet Wächter: Testnachricht von alerts.py")
        print("Testnachricht gesendet")
        return

    snap = positions.snapshot()
    state = load_state()
    msgs = check(snap, state)
    for m in msgs:
        send(m)
        time.sleep(0.5)
    save_state(state)
    print(json.dumps({"vermoegen": snap["gesamtvermoegen_usd"], "gesendet": msgs},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
