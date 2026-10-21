# Jupiter Perps Wallet Tracker

🇬🇧 [English version](README.en.md)

Ein Überwachungssystem für eine Solana-Wallet mit Hebel-Trading auf Jupiter Perps (ETH, SOL, BTC).

Die Idee: Warnungen **bevor** ein Verlust passiert, nicht erst danach. Dazu kommt ein täglicher Bericht mit Diagrammen, ein Backtest einer einfachen Trading-Methode und ein Papierhandel ohne echtes Geld.

Das System liest nur. Es hat keinen privaten Schlüssel und kann keine Trades ausführen.

> **Alle Regeln und Zahlen in diesem Projekt sind Beispiele.** Wann gewarnt wird, ab welchem Gewinn oder Verlust, was die Regel empfiehlt und wie oft Nachrichten kommen, lässt sich frei ändern. Wie das geht, steht unter [Regeln anpassen](#regeln-anpassen).

## Was es macht

**1. Warnungen in Telegram (alle 30 Sekunden geprüft)**

Alle Zahlen hier sind Beispielwerte.

- Gewinn oder Verlust einer Position in Stufen (alle +5 % Gewinn, alle −10 % Verlust)
- Abstand zur Liquidation, gestaffelt bis 0,25 %
- schnelle Kursbewegungen in beide Richtungen
- feste Regeln gegen typische Fehler beim Hebel-Trading („👉 REGEL: …“), z. B. Hälfte schließen ab +10 %, schließen ab −50 %, Gewinn schmilzt
- Tagesstopp, Pause nach Verlustserie, Überhandeln, Drawdown
- Ein- und Auszahlungen sowie Tausch im Wallet
- `/status` im Chat zeigt den aktuellen Stand, nach 6 Stunden Ruhe kommt ein Lebenszeichen

Das Wichtigste steht fett ganz oben, damit man es schon in der Handy-Vorschau sieht.

**2. Tagesbericht (nachts)**
- Vermögensverlauf und 30-Tage-Übersicht als Diagramme
- Kennzahlen und Regelverstöße werden in Python berechnet
- ein lokales Sprachmodell (Ollama) schreibt daraus nur den Text

**3. Trade-Analyse und Backtest**
- Rekonstruktion der bisherigen Perps-Trades der Wallet direkt aus der Blockchain
- Backtest der Methode „nach spürbarem Rückgang Long, kurz halten“ auf Minutenkursen, mit Lern- und Prüfzeit und Gebühren
- Ergebnis: kleine Rückgänge mit kleinem Ziel und ohne Trendfilter waren nach Gebühren auf allen Märkten im Minus. Nur wenige Einstellungen waren in beiden Zeiträumen positiv.

**4. Papierhandel**
- testet die beste Einstellung aus dem Backtest live, ohne echtes Geld, und meldet jeden Kauf und Verkauf

## Aufbau

```
Solana RPC ──► positions.py ──► alerts.py ──┐
Binance-Kurse ──► papertrade.py ────────────┼──► service.py (FastAPI) ◄── n8n (alle 30 Sek.) ──► Telegram
Telegram /status ──► status.py ─────────────┘
wallet_export.py ──► report.py ──► Ollama (Text) ──► Telegram (nachts)
```

- **Python** rechnet alles: Positionen, Gebühren, Liquidation, Regeln
- **n8n** (Docker) ruft den Dienst regelmäßig auf und schickt die Nachrichten an Telegram
- **Ollama** (lokal, gemma4:26b) schreibt nur den Text des Tagesberichts

## Dateien

| Datei | Aufgabe |
|---|---|
| `positions.py` | liest offene Positionen von Solana, berechnet Wert wie bei Jupiter („Inc. Fees“), Leihgebühr, Liquidation |
| `alerts.py` | prüft die Regeln und baut die Warnungen |
| `status.py` | `/status`-Befehl und Lebenszeichen |
| `report.py` | Tagesbericht mit Diagrammen und lokalem Sprachmodell |
| `wallet_export.py` | ganze Transaktionshistorie der Wallet als CSV |
| `trade_analyse.py` | rekonstruiert die bisherigen Perps-Trades |
| `backtest.py` | Backtest der Methode |
| `papertrade.py` | Papierhandel ohne echtes Geld |
| `service.py` | kleiner lokaler Dienst, den n8n aufruft |
| `wallet_waechter_workflow.json` | n8n-Workflow für die Warnungen |
| `wallet_waechter_bericht_workflow.json` | n8n-Workflow für den Tagesbericht |
| `deploy/wallet-waechter.service` | systemd-Dienst, damit alles nach dem Start von selbst läuft |

## Regeln anpassen

Alle Werte stehen als Einstellungen ganz oben in den Dateien (Abschnitt „1. Regeln“ bzw. „1. Einstellungen“). Die Zahlen unten sind nur Beispielwerte. Nach einer Änderung den Dienst neu starten.

**Wann informiert wird** (`alerts.py`)

| Einstellung | Beispiel | Bedeutung |
|---|---|---|
| `PNL_SCHRITT_GEWINN` | 5 | Meldung bei jedem +5 % Gewinn des Einsatzes |
| `PNL_SCHRITT_VERLUST` | 10 | Meldung bei jedem −10 % Verlust |
| `PNL_PUFFER_GEWINN` / `PNL_PUFFER_VERLUST` | 2 / 3 | Puffer, damit beim Pendeln an einer Grenze keine doppelten Meldungen kommen |
| `LIQ_STUFEN` | 5, 3, 2 … 0,25 | Warnung bei so viel % Abstand zur Liquidation |
| `PREIS_FENSTER_MIN` / `PREIS_BEWEGUNG` | 10 / 0,25 | Meldung, wenn sich der Kurs in 10 Minuten um 0,25 % bewegt |
| `PREIS_PAUSE_MIN` | 10 | gleiche Kursmeldung frühestens nach 10 Minuten wieder |
| `STOP_LOSS_ERINNERUNG` | False | Erinnerung, wenn kein Stop-Loss gesetzt ist (True = an) |

**Was die Regel empfiehlt** („👉 REGEL: …“, nur Empfehlung, es wird nichts ausgeführt)

| Einstellung | Beispiel | Empfehlung |
|---|---|---|
| `GEWINN_TEIL_SICHERN` | 10 | ab +10 %: Hälfte schließen |
| `GEWINN_GANZ_SICHERN` | 25 | ab +25 %: Rest schließen |
| `GEWINN_SCHMILZT` | 0,5 | Gewinn auf die Hälfte geschrumpft: schließen |
| `VERLUST_SCHLIESSEN` | −50 | ab −50 %: schließen |
| `LIQ_SCHLIESSEN` | 1,0 | Liquidation 1 % entfernt: schließen, nichts nachlegen |
| `MAX_HEBEL` / `KRITISCHER_HEBEL` | 10 / 25 | Warnung ab diesem Hebel |
| `RISIKO_PRO_TRADE` | 0,02 | höchstens 2 % des Vermögens Risiko pro Trade |

**Tagesregeln** (`alerts.py`)

| Einstellung | Beispiel | Bedeutung |
|---|---|---|
| `TAGESVERLUST_MAX` | 0,06 | Tagesstopp bei −6 % |
| `MAX_TRADES_TAG` | 5 | mehr Trades gelten als Überhandeln |
| `VERLUSTSERIE` / `PAUSE_NACH_SERIE_MIN` | 2 / 30 | nach zwei Verlusten in Folge 30 Minuten Pause |
| `MAX_DRAWDOWN` | 0,20 | 20 % unter dem Höchststand: Pause |
| `GROSSER_TAGESGEWINN` | 0,50 | +50 % an einem Tag: Gewinn sichern, Pause |

**Weitere Stellen**

- Prüfintervall: im n8n-Workflow „Wallet Wächter – Warnungen“ im Schedule-Knoten (Beispiel: alle 30 Sekunden)
- Lebenszeichen: `LEBENSZEICHEN_STUNDEN` in `status.py` (Beispiel: 6)
- Regel des Papierhandels: `RUECKGANG`, `ZIEL`, `NOTAUSSTIEG` in `papertrade.py`
- Backtest: getestete Werte in `RUECKGANG`, `ZIEL`, `NOTAUSSTIEG`, Gebühren mit `--gebuehr` in `backtest.py`
- Tagesbericht: Sprachmodell über `OLLAMA_MODEL` in `.env`; der Text `REGELN` in `report.py` sollte zu den eigenen Regeln passen
- Wortlaut der Nachrichten: direkt in `alerts.py` änderbar

## Einrichtung (kurz)

1. `pip install -r requirements.txt`
2. `.env.example` als `.env` kopieren und ausfüllen
3. Dienst starten: `uvicorn service:app --host 172.17.0.1 --port 8765` (oder als systemd-Dienst)
4. Beide Workflows in n8n importieren, Header-Auth (`x-api-key`) und Telegram-Zugang anlegen, im Telegram-Knoten *Parse Mode* auf **HTML** stellen
5. In Telegram `/status` schicken

## Grenzen

- Werte sind aus öffentlichen Kontodaten berechnet. Die Eröffnungsgebühr ist geschätzt, kleine Abweichungen zu Jupiter sind möglich.
- Das öffentliche Solana-RPC ist manchmal langsam oder lehnt Abfragen ab.
- Der Laptop muss laufen, damit Warnungen kommen. Das System warnt, es schützt nicht.
- Die Regeln sind Beispiel-Faustregeln und keine Anlageberatung. Jeder sollte sie an sich anpassen.
- Backtest-Ergebnisse gelten nur für den getesteten Zeitraum und sagen nichts sicher über die Zukunft.

## Datenquellen

- [Solana JSON-RPC](https://solana.com/docs/rpc)
- [Jupiter Perps Dokumentation](https://developers.jup.ag/docs/perps)
- [Jupiter Perps IDL und Beispiele](https://github.com/julianfssen/jupiter-perps-anchor-idl-parsing)
- [Binance öffentliche Kursdaten](https://data-api.binance.vision)
- [Telegram Bot API](https://core.telegram.org/bots/api)

## KI-Nutzung

Der Code ist mit Unterstützung von KI entstanden. Wie ich mit KI arbeite, steht in [local-ai-workflow](https://github.com/Amirhouschang/local-ai-workflow).

---

© 2026 Amirhoushang Rahmannejad. Alle Rechte vorbehalten.
