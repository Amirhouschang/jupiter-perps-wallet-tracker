"""
report.py
Tagesbericht für den Vortag: Kennzahlen (Python), zwei Diagramme (matplotlib)
und eine kurze Einordnung durch das lokale Modell (Ollama). Versand per Telegram.

Zahlen kommen immer aus Python – das Modell schreibt nur den erklärenden Text
und bekommt ausschließlich fertige Kennzahlen.

Aufruf:
  python report.py                     → alle fertigen, noch nicht gemeldeten Tage
  python report.py --datum 2026-10-17  → genau diesen Tag (auch erneut)
  python report.py --ohne-ki           → ohne Ollama-Text (schneller Test)
"""

import os
import sys
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from dotenv import load_dotenv

# ---------------------------------------------------------------
# 1. Einstellungen
# ---------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OLLAMA_URL = os.getenv("OLLAMA_URL") or "http://localhost:11434"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "qwen2.5:32b"

EQUITY_FILE, TRADES_FILE, WARN_FILE = "equity.csv", "trades.csv", "warnungen.csv"
REPORT_STATE = "report_state.json"
CHART_DIR = "berichte"
BERLIN = ZoneInfo("Europe/Berlin")
MAX_NACHHOLEN = 3                      # höchstens so viele verpasste Tage nachholen
MAX_FEHLVERSUCHE = 6                   # Datenfehler: nach 6 stündlichen Versuchen den Tag überspringen und melden

WARN_TYPEN = {"🚨": "Hebel/Liquidationsnähe", "⚖️": "Risiko zu groß", "📏": "Hebel zu hoch für Schwankung",
              "⛔": "Sperre/Tagesstopp", "🔁": "Überhandeln", "➕": "Einzahlung", "➖": "Auszahlung",
              "🧯": "Drawdown-Pause", "✋": "Pause nach Gewinn", "🏔️": "Neuer Höchststand",
              "💰": "Gewinn sichern", "⚠️": "Hebel über Grenze / Gewinn schmilzt", "❗": "Gewinn komplett weg",
              "🛑": "Verlust −50 %", "📈": "Gewinn/Verlust steigt", "📉": "Gewinn/Verlust fällt",
              "🟢": "Kursbewegung für dich", "⚡": "Kursbewegung gegen dich",
              "🔄": "Tausch im Wallet"}


# ---------------------------------------------------------------
# 2. Daten laden
# ---------------------------------------------------------------
def load(path):
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, on_bad_lines="skip")
    col = "geschlossen" if "geschlossen" in df.columns else "zeit"
    df["ts"] = pd.to_datetime(df[col], utc=True, errors="coerce", format="ISO8601")
    for c in ("vermoegen_usd", "netto_ohne_einzahlungen_usd", "offene_positionen",
              "ergebnis_usd", "einsatz_usd", "max_hebel"):
        if c in df.columns:                       # Text in einer Zahlenspalte → leer statt Absturz
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "offene_positionen" in df.columns:
        df["offene_positionen"] = df["offene_positionen"].fillna(0)
    pflicht = ["ts"] + [c for c in ("vermoegen_usd", "netto_ohne_einzahlungen_usd", "ergebnis_usd")
                        if c in df.columns]
    kaputt = int(df[pflicht].isna().any(axis=1).sum())
    if kaputt:                                    # z. B. halb geschriebene Zeile → überspringen statt abstürzen
        print(f"Hinweis: {kaputt} unlesbare Zeile(n) in {path} übersprungen")
        df = df.dropna(subset=pflicht)
    df["ts"] = df["ts"].dt.tz_convert(BERLIN)
    df["tag"] = df["ts"].dt.date
    return df


def usd(x):
    return f"{x:,.2f} $".replace(",", "X").replace(".", ",").replace("X", ".").replace("-", "−")


def de(x, d=1, plus=False):
    # Zahl deutsch: Komma statt Punkt, echtes Minuszeichen
    s = f"{x:+.{d}f}" if plus else f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def warn_typ(typ, text):
    # 📈/📉 bedeuten „steigt/fällt“ – geschlossene Trades getrennt zählen
    if typ in ("📈", "📉") and "Position zu" in str(text):
        return "Trade geschlossen"
    return WARN_TYPEN.get(typ, typ)


def tagesstart(eq, tag):
    """Start eines Tages = Stand am Ende des Vortags (gibt es keinen, der erste Wert des Tages)."""
    davor = eq[eq["tag"] < tag]
    if not davor.empty:
        return float(davor.sort_values("ts")["netto_ohne_einzahlungen_usd"].iloc[-1])
    return float(eq[eq["tag"] == tag].sort_values("ts")["netto_ohne_einzahlungen_usd"].iloc[0])


# ---------------------------------------------------------------
# 3. Kennzahlen eines Tages
# ---------------------------------------------------------------
def kennzahlen(tag, eq, tr, wa):
    e = eq[eq["tag"] == tag]
    if e.empty:
        return None
    e = e.sort_values("ts")
    start = tagesstart(eq, tag)
    ende = e["netto_ohne_einzahlungen_usd"].iloc[-1]

    t = tr[tr["tag"] == tag] if not tr.empty else pd.DataFrame()
    w = wa[wa["tag"] == tag] if not wa.empty else pd.DataFrame()
    warn = {}
    if not w.empty:
        for typ, text in zip(w["typ"], w["text"]):
            name = warn_typ(typ, text)
            warn[name] = warn.get(name, 0) + 1
        warn = dict(sorted(warn.items(), key=lambda x: -x[1]))

    k = {
        "datum": tag.isoformat(),
        "vermoegen_start_usd": round(float(start), 2),
        "vermoegen_ende_usd": round(float(ende), 2),
        "ergebnis_usd": round(float(ende - start), 2),
        "ergebnis_prozent": round(float((ende - start) / start * 100), 1) if start > 0 else 0.0,
        "tageshoch_usd": round(float(e["netto_ohne_einzahlungen_usd"].max()), 2),
        "tagestief_usd": round(float(e["netto_ohne_einzahlungen_usd"].min()), 2),
        "vermoegen_inkl_einzahlungen_usd": round(float(e["vermoegen_usd"].iloc[-1]), 2),
        "offene_positionen_am_ende": int(e["offene_positionen"].iloc[-1]),
        "trades_geschlossen": int(len(t)),
        "gewinntrades": int((t["ergebnis_usd"] > 0).sum()) if not t.empty else 0,
        "verlusttrades": int((t["ergebnis_usd"] < 0).sum()) if not t.empty else 0,
        "summe_trades_usd": round(float(t["ergebnis_usd"].sum()), 2) if not t.empty else 0.0,
        "groesster_gewinn_usd": round(float(t["ergebnis_usd"].max()), 2)
        if not t.empty and (t["ergebnis_usd"] > 0).any() else None,
        "groesster_verlust_usd": round(float(t["ergebnis_usd"].min()), 2)
        if not t.empty and (t["ergebnis_usd"] < 0).any() else None,
        "vermutlich_liquidiert": int(t["liquidiert_vermutet"].astype(str).eq("True").sum()) if not t.empty else 0,
        "trades_ohne_stop": int(t["mit_stop"].astype(str).eq("False").sum()) if not t.empty else 0,
        "hoechster_hebel": float(t["max_hebel"].max()) if not t.empty else None,
        "warnungen": warn,
    }
    k["regelverstoesse"] = regelverstoesse(k, w)
    return k


def regelverstoesse(k, w):
    # Python entscheidet, was ein Regelverstoß ist – das Modell formuliert nur
    v = []
    if k["trades_geschlossen"] > 5:
        v.append(f"{k['trades_geschlossen']} Trades geschlossen (Regel: höchstens 5)")
    if k["hoechster_hebel"] and k["hoechster_hebel"] > 10:
        v.append(f"Hebel bis {de(k['hoechster_hebel'])}× (Regel: höchstens 10×)")
    if k["ergebnis_prozent"] <= -6:
        v.append(f"Tagesverlust {de(k['ergebnis_prozent'])} % (Tagesstopp-Grenze: −6 %)")
    if k["vermutlich_liquidiert"]:
        v.append(f"{k['vermutlich_liquidiert']} Position(en) vermutlich liquidiert")
    if k["warnungen"].get("Hebel/Liquidationsnähe"):
        v.append("im Tagesverlauf eine Position mit sehr hohem Hebel bzw. nahe an der Liquidation")
    if not w.empty and w["text"].str.contains("geöffnet trotz").any():
        v.append("Trade geöffnet, obwohl eine Sperre (Tagesstopp/Pause) galt")
    if not w.empty and w["text"].str.contains("Nachzahlung am Verlusttag").any():
        v.append("Geld nachgezahlt an einem Verlusttag")
    return v


# ---------------------------------------------------------------
# 4. Diagramme
# ---------------------------------------------------------------
BLAU, ROT, GRUEN, GRAU = "#2f6fd6", "#c0392b", "#2e8b57", "#8a8f98"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})


def chart_tag(tag, eq, tr, start):
    """Verlauf eines Tages. start = Startwert wie im Bericht (Ende des Vortags)."""
    e = eq[eq["tag"] == tag].sort_values("ts").copy()
    y = e["netto_ohne_einzahlungen_usd"]
    # Lücken (Laptop aus / Dienst gestoppt) nicht mit einer geraden Linie überbrücken
    luecke = e["ts"].diff() > pd.Timedelta(minutes=5)
    y_plot = y.copy()
    y_plot[luecke] = float("nan")

    tag_beginn = pd.Timestamp(datetime.combine(tag, datetime.min.time()), tz=BERLIN)
    tag_ende = tag_beginn + pd.Timedelta(days=1)
    jetzt = pd.Timestamp.now(tz=BERLIN)

    fig, ax = plt.subplots(figsize=(9, 4.2))
    farbe = GRUEN if y.iloc[-1] >= start else ROT
    ax.axhline(start, color=GRAU, lw=1, ls="--", zorder=1)
    ax.fill_between(e["ts"], y_plot, start, color=farbe, alpha=0.12, zorder=1)
    ax.plot(e["ts"], y_plot, color=farbe, lw=2, zorder=2)

    # graue Fläche = keine Daten (Laptop/Dienst aus): Lücken, vor dem ersten und nach dem letzten Messpunkt
    vorher = e["ts"].shift()
    for a, b in zip(vorher[luecke], e["ts"][luecke]):
        ax.axvspan(a, b, color="#f0f0f0", zorder=0)
    if e["ts"].iloc[0] - tag_beginn > pd.Timedelta(minutes=10):
        ax.axvspan(tag_beginn, e["ts"].iloc[0], color="#f0f0f0", zorder=0)
    ende_daten = min(tag_ende, jetzt)
    if ende_daten - e["ts"].iloc[-1] > pd.Timedelta(minutes=10):
        ax.axvspan(e["ts"].iloc[-1], ende_daten, color="#f0f0f0", zorder=0)

    # einzelne Messpunkte zwischen zwei Lücken als Punkt zeigen (eine Linie braucht zwei Punkte)
    allein = luecke & (luecke.shift(-1, fill_value=True))
    if len(e) and (len(e) == 1 or luecke.iloc[1]):
        allein.iloc[0] = True
    ax.scatter(e["ts"][allein], y[allein], s=18, color=farbe, zorder=2)

    t = tr[tr["tag"] == tag] if not tr.empty else pd.DataFrame()
    for _, r in t.iterrows():
        yy = y.iloc[(e["ts"] - r["ts"]).abs().argmin()]
        ax.scatter(r["ts"], yy, s=70, zorder=3, color=GRUEN if r["ergebnis_usd"] > 0 else ROT,
                   edgecolor="white", linewidth=1.5)

    # Start- und Endwert beschriften (Start = gleicher Wert wie im Bericht)
    ax.annotate(f"Start {usd(start)}", (tag_beginn, start), textcoords="offset points", xytext=(4, 6),
                fontsize=9, color=GRAU)
    ax.annotate(usd(y.iloc[-1]), (e["ts"].iloc[-1], y.iloc[-1]), textcoords="offset points", xytext=(-6, 10),
                ha="right", fontsize=10, color=farbe, fontweight="bold",
                bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.8})

    # kleine Schwankungen nicht künstlich aufblasen: mindestens ±3 % Bereich zeigen (Startlinie immer sichtbar)
    oben, unten = max(y.max(), start), min(y.min(), start)
    mitte = (oben + unten) / 2
    spanne = max(oben - unten, mitte * 0.06)
    ax.set_ylim(mitte - spanne * 0.65, mitte + spanne * 0.65)
    ax.set_xlim(tag_beginn, tag_ende)

    titel = f"Vermögen am {tag.strftime('%d.%m.%Y')} (ohne Einzahlungen)"
    if not t.empty:
        titel += "  ·  ● Trade zu (grün Gewinn, rot Verlust)"
    ax.set_title(titel, loc="left", fontsize=10)
    ax.set_ylabel("USD")
    ax.grid(axis="y", color="#eee")
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_locator(matplotlib.dates.HourLocator(byhour=range(0, 24, 3), tz=BERLIN))
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=BERLIN))
    fig.tight_layout()
    path = os.path.join(CHART_DIR, f"{tag}_verlauf.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_30_tage(tag, eq):
    """Ergebnis je Tag (Tagesende minus Tagesstart, gleiche Rechnung wie im Bericht), feste 30-Tage-Achse."""
    tage = sorted(d for d in eq["tag"].unique() if tag - timedelta(days=29) <= d <= tag)
    if not tage:
        return None
    werte = [float(eq[eq["tag"] == d].sort_values("ts")["netto_ohne_einzahlungen_usd"].iloc[-1])
             - tagesstart(eq, d) for d in tage]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    x = [datetime.combine(d, datetime.min.time()) for d in tage]
    ax.bar(x, werte, width=0.7, color=[GRUEN if v >= 0 else ROT for v in werte])
    ax.axhline(0, color=GRAU, lw=1)
    # Achse ab dem ersten Tag mit Daten, mindestens 7 und höchstens 30 Tage breit
    spanne = min(30, max(7, (tag - tage[0]).days + 1))
    erster = tag - timedelta(days=spanne - 1)
    ax.set_xlim(datetime.combine(erster, datetime.min.time()) - timedelta(hours=12),
                datetime.combine(tag, datetime.min.time()) + timedelta(hours=12))
    # Beschriftung rückwärts ab dem Berichtstag (der Berichtstag steht immer da): bis 10 Tage jeden Tag, sonst alle 3
    schritt = 1 if spanne <= 10 else 3
    ax.set_xticks([datetime.combine(tag - timedelta(days=i), datetime.min.time()) for i in range(0, spanne, schritt)])
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m"))
    ax.set_axisbelow(True)                       # Gitterlinien hinter den Balken
    ax.set_title("Ergebnis pro Tag (USD, ohne Einzahlungen)", loc="left", fontsize=10)
    ax.grid(axis="y", color="#eee")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    fig.tight_layout()
    path = os.path.join(CHART_DIR, f"{tag}_30tage.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------
# 5. Einordnung durch das lokale Modell
# ---------------------------------------------------------------
REGELN = ("Hebel höchstens 10×; Risiko pro Trade höchstens 2 % des Vermögens (nach Verlust 1 %); "
          "ab +10 % Gewinn Hälfte schließen, ab +25 % Rest schließen; schmilzt der Gewinn auf die Hälfte "
          "oder ins Minus: schließen; ab −50 % schließen; Liquidation unter 1 %: schließen, nichts nachlegen; "
          "höchstens 5 Trades pro Tag; Tagesstopp bei −6 %; nach Verlusttrade kein neues Geld; "
          "Hälfte jedes Gewinns sichern. Stop-Loss-Hinweise sind per Einstellung abgeschaltet – das nicht kritisieren.")

MUSTER = ("Typische Risiken beim Hebel-Trading: sehr hoher Hebel, nach Gewinnen sofort alles wieder einsetzen, "
          "sehr viele Trades an einem Tag, nach Verlusten Geld nachschießen, Gewinne nie abziehen.")


def ki_text(k):
    prompt = (
        "Du bist ein nüchterner Trading-Coach. Schreibe auf Deutsch eine Einordnung des Handelstages "
        "in 4 bis 6 Sätzen Fließtext, ohne Überschriften und ohne Aufzählungen.\n"
        "Regeln:\n- Nenne keine Zahl, die nicht in den Kennzahlen steht. Rechne nichts selbst aus.\n"
        "- Regelverstöße: Nenne NUR die Punkte aus der Liste 'regelverstoesse'. Erfinde keine weiteren. "
        "Ist die Liste leer, sage klar, dass heute keine Regel verletzt wurde.\n"
        "- 'trades_geschlossen' ist die Anzahl der Trades. Ist sie 0, wurde heute nichts geschlossen.\n"
        "- Fehlender Stop-Loss wird nicht kritisiert (per Einstellung abgeschaltet).\n"
        "- Erwähne die typischen Risiken nur, wenn ein Punkt aus 'regelverstoesse' dazu passt.\n"
        "- Schließe mit genau einer konkreten Sache für morgen.\n"
        "- Keine Kursprognosen, keine Kauf- oder Verkaufsempfehlungen.\n\n"
        f"Handelsregeln: {REGELN}\n{MUSTER}\n\n"
        f"Kennzahlen des Tages (JSON):\n{json.dumps(k, ensure_ascii=False, indent=1)}"
    )
    r = requests.post(f"{OLLAMA_URL}/api/generate", timeout=1800, json={
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "think": False,                       # „Denk-Modus“ aus → deutlich schneller
        "options": {"temperature": 0.2, "num_ctx": 4096}})
    r.raise_for_status()
    return r.json()["response"].strip()


# ---------------------------------------------------------------
# 6. Bericht zusammensetzen und senden
# ---------------------------------------------------------------
def kopf(k):
    pfeil = "📈" if k["ergebnis_usd"] >= 0 else "📉"
    zeilen = [
        f"📊 Tagesbericht {datetime.fromisoformat(k['datum']).strftime('%d.%m.%Y')}",
        f"{pfeil} Ergebnis: {'+' if k['ergebnis_usd'] > 0 else ''}{usd(k['ergebnis_usd'])} "
        f"({de(k['ergebnis_prozent'], plus=True)} %)",
        f"Vermögen: {usd(k['vermoegen_start_usd'])} → {usd(k['vermoegen_ende_usd'])} "
        f"(Hoch {usd(k['tageshoch_usd'])}, Tief {usd(k['tagestief_usd'])})",
        f"Trades: {k['trades_geschlossen']} (✅ {k['gewinntrades']} / ❌ {k['verlusttrades']})"
        + (f", vermutlich liquidiert: {k['vermutlich_liquidiert']}" if k["vermutlich_liquidiert"] else ""),
    ]
    if k["trades_geschlossen"]:
        bester = f"+{usd(k['groesster_gewinn_usd'])}" if k["groesster_gewinn_usd"] is not None else "kein Gewinntrade"
        schlechtester = usd(k["groesster_verlust_usd"]) if k["groesster_verlust_usd"] is not None else "kein Verlusttrade"
        zeilen.append(f"Bester: {bester} · Schlechtester: {schlechtester} · "
                      f"höchster Hebel {de(k['hoechster_hebel'])}×")
    if k["warnungen"]:
        zeilen.append("Warnungen: " + ", ".join(f"{n}× {t}" for t, n in k["warnungen"].items()))
    zeilen.append(f"Offene Positionen am Tagesende: {k['offene_positionen_am_ende']}")
    return "\n".join(zeilen)


def tg(method, data, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                      data={"chat_id": CHAT_ID, **data}, files=files, timeout=60)
    r.raise_for_status()


def bericht(tag, mit_ki=True):
    eq, tr, wa = load(EQUITY_FILE), load(TRADES_FILE), load(WARN_FILE)
    if eq.empty:
        return None
    k = kennzahlen(tag, eq, tr, wa)
    if k is None:
        return None
    os.makedirs(CHART_DIR, exist_ok=True)
    bilder = [chart_tag(tag, eq, tr, k["vermoegen_start_usd"]), chart_30_tage(tag, eq)]

    text = kopf(k)
    ruhig = not k["trades_geschlossen"] and not k["regelverstoesse"] and not k["offene_positionen_am_ende"]
    if ruhig:
        text += "\n\n🧠 Einordnung:\nKein Trade, keine offene Position, keine Regel verletzt."
    elif mit_ki:
        try:
            text += "\n\n🧠 Einordnung:\n" + ki_text(k)
        except Exception as e:                       # Bericht kommt trotzdem
            text += f"\n\n(Einordnung nicht verfügbar: {type(e).__name__})"

    for b in filter(None, bilder):
        with open(b, "rb") as f:
            tg("sendPhoto", {}, files={"photo": f})
    tg("sendMessage", {"text": text})
    with open(os.path.join(CHART_DIR, f"{tag}_bericht.json"), "w") as f:
        json.dump({"kennzahlen": k, "text": text}, f, ensure_ascii=False, indent=2)
    return k


def offene_tage():
    gestern = datetime.now(BERLIN).date() - timedelta(days=1)
    st = _state()
    letzter = date.fromisoformat(st["letzter_bericht"]) if "letzter_bericht" in st else gestern - timedelta(days=1)
    tage = []
    d = letzter + timedelta(days=1)
    while d <= gestern:
        tage.append(d)
        d += timedelta(days=1)
    return tage[-MAX_NACHHOLEN:]


def _state():
    try:
        with open(REPORT_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _state_speichern(st):
    tmp = REPORT_STATE + ".tmp"                 # erst Hilfsdatei, dann ersetzen → nie halb geschrieben
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, REPORT_STATE)


def run_pending(mit_ki=True):
    """Alle fertigen, noch nicht gemeldeten Tage berichten.
    Netzfehler (Telegram/Internet): Tag bleibt offen, nächste Stunde neuer Versuch.
    Andere Fehler (z. B. kaputte Daten): nach MAX_FEHLVERSUCHE Versuchen Tag überspringen und kurz melden,
    damit spätere Tage nicht für immer blockiert sind."""
    gesendet = []
    for tag in offene_tage():
        st = _state()
        try:
            if bericht(tag, mit_ki):
                gesendet.append(tag.isoformat())
        except requests.RequestException:
            raise                                    # Netz weg → später erneut, nichts überspringen
        except Exception as e:
            versuche = st.setdefault("fehlversuche", {}).get(tag.isoformat(), 0) + 1
            st["fehlversuche"][tag.isoformat()] = versuche
            _state_speichern(st)
            if versuche < MAX_FEHLVERSUCHE:
                raise
            try:
                tg("sendMessage", {"text": f"⚠️ Tagesbericht {tag.strftime('%d.%m.%Y')} konnte nicht erstellt "
                                           f"werden ({type(e).__name__}). Tag wird übersprungen."})
            except Exception:
                pass
        st["letzter_bericht"] = tag.isoformat()      # auch Tage ohne Daten als erledigt merken
        st.get("fehlversuche", {}).pop(tag.isoformat(), None)
        _state_speichern(st)
    return gesendet


if __name__ == "__main__":
    mit_ki = "--ohne-ki" not in sys.argv
    if "--datum" in sys.argv:
        tag = date.fromisoformat(sys.argv[sys.argv.index("--datum") + 1])
        print(json.dumps(bericht(tag, mit_ki), ensure_ascii=False, indent=2))
    else:
        print("Gesendet für:", run_pending(mit_ki))
