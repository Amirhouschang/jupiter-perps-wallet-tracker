"""
positions.py
Liest offene Jupiter-Perps-Positionen einer Wallet direkt von Solana
und berechnet Hebel, Gewinn/Verlust und Abstand zur Liquidation.
Nur Lesen – kein Private Key nötig.
"""

import os
import json
import base64
import struct
import hashlib
import time

import requests
from dotenv import load_dotenv
from solders.pubkey import Pubkey

# ---------------------------------------------------------------
# 1. Einstellungen
# ---------------------------------------------------------------
load_dotenv()
WALLET = os.getenv("WALLET_ADDRESS")
RPC_URL = os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"

PERPS_PROGRAM = Pubkey.from_string("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu")
JLP_POOL = Pubkey.from_string("5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq")

# Märkte: Custody-Konto + Token-Mint (für den aktuellen Preis)
MARKETS = {
    "SOL": ("7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz", "So11111111111111111111111111111111111111112"),
    "ETH": ("AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"),
    "BTC": ("5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"),
}
# Sicherheiten bei Shorts
USDC_CUSTODY = "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa"
USDT_CUSTODY = "4vkNeXiYEUizLdrpdPS1eC2mccyM4NUPRtERrk6ZETkk"

TOKEN_PROGRAMS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",           # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",           # Token-2022
]

LONG, SHORT = 1, 2
BPS_POWER = 10_000
RATE_POWER = 1_000_000_000


# ---------------------------------------------------------------
# 2. Alle möglichen Positions-Adressen berechnen (PDA)
#    Jupiter: pro Markt und Seite genau eine Position je Wallet
# ---------------------------------------------------------------
def position_address(owner, custody, collateral_custody, side):
    seeds = [b"position", bytes(owner), bytes(JLP_POOL),
             bytes(Pubkey.from_string(custody)),
             bytes(Pubkey.from_string(collateral_custody)), bytes([side])]
    return Pubkey.find_program_address(seeds, PERPS_PROGRAM)[0]


def candidates(owner):
    out = []
    for market, (custody, _) in MARKETS.items():
        out.append((market, "Long", position_address(owner, custody, custody, LONG)))
        for coll in (USDC_CUSTODY, USDT_CUSTODY):
            out.append((market, "Short", position_address(owner, custody, coll, SHORT)))
    return out


# ---------------------------------------------------------------
# 3. Konten holen und Position dekodieren
# ---------------------------------------------------------------
def rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def decode_position(raw):
    # Aufbau: 8 Byte Kennung, dann owner, pool, custody, collateralCustody (je 32 Byte)
    owner = Pubkey.from_bytes(raw[8:40])
    open_time, update_time = struct.unpack_from("<qq", raw, 136)
    side = raw[152]
    price, size_usd, collateral_usd = struct.unpack_from("<QQQ", raw, 153)
    realised_pnl = struct.unpack_from("<q", raw, 177)[0]
    lo, hi = struct.unpack_from("<QQ", raw, 185)
    return {
        "owner": str(owner),
        "custody": str(Pubkey.from_bytes(raw[72:104])),
        "collateral_custody": str(Pubkey.from_bytes(raw[104:136])),
        "side": side,
        "price_raw": price,
        "size_raw": size_usd,
        "collateral_raw": collateral_usd,
        "interest_snapshot": lo + (hi << 64),
        "realised_pnl_usd": realised_pnl / 1e6,
        "open_time": open_time,
    }


def decode_custody(raw):
    # Aufbau laut Jupiter-IDL (Byte-Positionen nachgerechnet)
    lo, hi = struct.unpack_from("<QQ", raw, 262)
    return {
        "mint": str(Pubkey.from_bytes(raw[40:72])),
        "ag_oracle": str(Pubkey.from_bytes(raw[384:416])),      # aktuelle Preisquelle von Jupiter Perps
        "trade_impact_fee_scalar": struct.unpack_from("<Q", raw, 151)[0],
        "max_leverage_bps": struct.unpack_from("<Q", raw, 175)[0],
        "cumulative_interest": lo + (hi << 64),
        "increase_bps": struct.unpack_from("<Q", raw, 296)[0],
        "decrease_bps": struct.unpack_from("<Q", raw, 304)[0],
        # für die Leihgebühr
        "owned": struct.unpack_from("<Q", raw, 222)[0],
        "locked": struct.unpack_from("<Q", raw, 230)[0],
        "last_update": struct.unpack_from("<q", raw, 278)[0],
        "hourly_funding_dbps": struct.unpack_from("<Q", raw, 286)[0],
        "jump": struct.unpack_from("<QQQQ", raw, 352),   # min, max, target, targetUtil
        "debt": u128(raw, 1004),
        "interests_accrued": u128(raw, 1020),
    }


def u128(raw, off):
    lo, hi = struct.unpack_from("<QQ", raw, off)
    return lo + (hi << 64)


def div_ceil(a, b):
    return -(-a // b)


def hourly_borrow_rate(c):
    # Nachbau von Jupiters getHourlyBorrowRate (Ergebnis in RATE_POWER)
    debt = div_ceil(max(c["debt"] - c["interests_accrued"], 0), RATE_POWER)
    owned, locked = c["owned"] + debt, c["locked"] + debt
    if c["hourly_funding_dbps"] != 0:                       # lineares Modell
        hourly = c["hourly_funding_dbps"] * RATE_POWER // 100_000
        return div_ceil(locked * hourly, owned) if owned > 0 and locked > 0 else 0
    min_bps, max_bps, target_bps, target_util = c["jump"]  # Sprung-Modell
    util = locked * RATE_POWER // owned if owned > 0 and locked > 0 else 0
    if util <= target_util:
        yearly = (div_ceil((target_bps - min_bps) * util, target_util) + min_bps) * RATE_POWER // BPS_POWER
    else:
        rate_diff = max(0, max_bps - target_bps)
        util_diff = max(0, util - target_util)
        yearly = (div_ceil(rate_diff * util_diff, RATE_POWER - target_util) + target_bps) * RATE_POWER // BPS_POWER
    return yearly // (24 * 365)


def cumulative_interest_now(c):
    # auf Solana gespeicherter Stand + Zinsen seit der letzten Aktualisierung
    if c["owned"] == 0:
        return c["cumulative_interest"]
    seconds = max(0, int(time.time()) - c["last_update"])
    return c["cumulative_interest"] + div_ceil(hourly_borrow_rate(c) * seconds, 3600)


def get_custodies():
    keys = [c for c, _ in MARKETS.values()] + [USDC_CUSTODY, USDT_CUSTODY]
    accs = rpc("getMultipleAccounts", [keys, {"encoding": "base64"}])["value"]
    out = {k: decode_custody(base64.b64decode(a["data"][0])) for k, a in zip(keys, accs)}
    # Selbstkontrolle: Mint muss zum Markt passen, sonst stimmen die Byte-Positionen nicht
    for name, (c, mint) in MARKETS.items():
        if out[c]["mint"] != mint:
            raise SystemExit(f"Custody-Daten für {name} passen nicht – Aufbau prüfen")
    return out


# ---------------------------------------------------------------
# 3b. Stop-Loss / Take-Profit (offene Trigger-Aufträge der Wallet)
# ---------------------------------------------------------------
REQUEST_DISC = hashlib.sha256(b"account:PositionRequest").digest()[:8]


def read_option(raw, off, fmt, size):
    # Anchor-Option: 1 Byte (0 = leer, 1 = vorhanden) + Wert
    if raw[off] == 0:
        return None, off + 1
    return struct.unpack_from(fmt, raw, off + 1)[0], off + 1 + size


def decode_request(raw):
    position = str(Pubkey.from_bytes(raw[104:136]))
    request_change, request_type = raw[200], raw[201]
    off = 203
    _, off = read_option(raw, off, "<Q", 8)            # priceSlippage
    _, off = read_option(raw, off, "<Q", 8)            # jupiterMinimumOut
    _, off = read_option(raw, off, "<Q", 8)            # preSwapAmount
    trigger_price, off = read_option(raw, off, "<Q", 8)
    above, off = read_option(raw, off, "<?", 1)
    _, off = read_option(raw, off, "<?", 1)            # entirePosition
    executed = raw[off] == 1
    return {"position": position, "change": request_change, "type": request_type,
            "trigger_price": trigger_price / 1e6 if trigger_price else None,
            "above": above, "executed": executed}


def get_trigger_orders(owner):
    # Alle offenen Aufträge der Wallet: Filter auf Kontotyp + Besitzer
    res = rpc("getProgramAccounts", [str(PERPS_PROGRAM), {
        "encoding": "base64",
        "filters": [
            {"memcmp": {"offset": 0, "bytes": base64.b64encode(REQUEST_DISC).decode(), "encoding": "base64"}},
            {"memcmp": {"offset": 8, "bytes": str(owner)}},
        ]}])
    orders = {}
    for acc in res:
        r = decode_request(base64.b64decode(acc["account"]["data"][0]))
        # Trigger-Auftrag (1), der die Position verkleinert (2), noch nicht ausgeführt
        if r["type"] == 1 and r["change"] == 2 and not r["executed"]:
            orders.setdefault(r["position"], []).append(r)
    return orders


def tpsl_info(orders, is_long, pos):
    liq, mark, size = pos["liquidation"], pos["preis_jetzt"], pos["groesse_usd"]
    tp = [o["trigger_price"] for o in orders if o["above"] == is_long]
    sl = [o["trigger_price"] for o in orders if o["above"] != is_long]
    # Stop-Loss bringt nur etwas, wenn er vor der Liquidation auslöst
    valid = [p for p in sl if (p > liq if is_long else p < liq)]
    info = {"take_profit": tp, "stop_loss": sl,
            "stop_loss_gesetzt": bool(sl), "stop_loss_vor_liquidation": bool(valid)}
    if valid:
        # nächstgelegener wirksamer Stop: so viel geht verloren, wenn er auslöst (ab jetzt)
        stop = max(valid) if is_long else min(valid)
        abstand = abs(mark - stop) / mark
        info["stop_abstand_prozent"] = round(abstand * 100, 2)
        info["risiko_bis_stop_usd"] = round(size * abstand + pos["schliessgebuehr_usd"], 2)
        # Anteil des Stop-Abstands am Liquidationsabstand (Profi-Regel: höchstens die Hälfte)
        liq_abstand = pos["abstand_liq_prozent"] / 100
        info["stop_anteil_am_liq_abstand"] = round(abstand / liq_abstand, 2) if liq_abstand > 0 else None
    else:
        # ohne wirksamen Stop ist die ganze Sicherheit im Risiko
        info["risiko_bis_stop_usd"] = pos["nettowert_bei_schliessen_usd"]
    return info


# ---------------------------------------------------------------
# 4. Aktuelle Preise
#    Hauptquelle: Orakel, das Jupiter Perps selbst nutzt (= „Mark Price“ in der App)
#    Ersatz: Jupiter Price API (Handelspreis des Tokens, kann abweichen)
# ---------------------------------------------------------------
def oracle_prices(custodies):
    names = list(MARKETS)
    keys = [custodies[MARKETS[n][0]]["ag_oracle"] for n in names]
    accs = rpc("getMultipleAccounts", [keys, {"encoding": "base64"}])["value"]
    out = {}
    for name, acc in zip(names, accs):
        if acc is None:
            continue
        raw = base64.b64decode(acc["data"][0])
        # Aufbau (Doves agPriceFeed): 8 Kennung, mint, edge, chainlink, pyth, pythId (je 32),
        # dann Preis (u64), Exponent (i8), Zeit (i64)
        if str(Pubkey.from_bytes(raw[8:40])) != MARKETS[name][1]:
            continue                        # Selbstkontrolle: falsches Konto
        price, expo, ts = struct.unpack_from("<Qbq", raw, 168)
        alter = int(time.time() - ts)
        ORACLE_AGE[name] = alter
        if alter < 120:                     # nur frische Preise verwenden
            out[name] = price * 10 ** expo
    return out


ORACLE_AGE = {}                             # Alter der Orakelpreise in Sekunden (zur Kontrolle)


# ---------------------------------------------------------------
# 4a. Ersatzquelle: Jupiter Price API (kostenlos)
# ---------------------------------------------------------------
def prices():
    ids = ",".join(m for _, m in MARKETS.values())
    data = requests.get(f"https://lite-api.jup.ag/price/v3?ids={ids}", timeout=15).json()
    return {name: float(data[mint]["usdPrice"]) for name, (_, mint) in MARKETS.items()}


SOL_MINT = "So11111111111111111111111111111111111111112"
SYMBOLS = {
    SOL_MINT: "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
}


def token_prices(mints):
    # Jupiter Price API: höchstens 50 Token pro Abfrage
    out = {}
    for i in range(0, len(mints), 50):
        ids = ",".join(mints[i:i + 50])
        data = requests.get(f"https://lite-api.jup.ag/price/v3?ids={ids}", timeout=15).json()
        out.update({m: float(v["usdPrice"]) for m, v in data.items() if v and "usdPrice" in v})
    return out


# ---------------------------------------------------------------
# 4b. Spot-Bestände im Wallet (SOL + alle Token)
# ---------------------------------------------------------------
def wallet_holdings(owner):
    amounts = {SOL_MINT: rpc("getBalance", [str(owner)])["value"] / 1e9}
    for program in TOKEN_PROGRAMS:
        res = rpc("getTokenAccountsByOwner",
                  [str(owner), {"programId": program}, {"encoding": "jsonParsed"}])["value"]
        for acc in res:
            info = acc["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"]["uiAmountString"] or 0)
            if amt > 0:
                amounts[info["mint"]] = amounts.get(info["mint"], 0) + amt

    px = token_prices(list(amounts))
    rows, unpriced = [], 0
    for mint, amt in amounts.items():
        if mint not in px:
            unpriced += 1                 # kein Preis = meist Spam/Airdrop-Token
            continue
        value = amt * px[mint]
        if value >= 0.01:                 # Staub ausblenden
            rows.append({"token": SYMBOLS.get(mint, mint[:6] + "…"), "mint": mint,
                         "menge": round(amt, 6), "preis_usd": px[mint],
                         "wert_usd": round(value, 2)})
    rows.sort(key=lambda r: -r["wert_usd"])
    return rows, unpriced


# ---------------------------------------------------------------
# 5. Kennzahlen pro Position
# ---------------------------------------------------------------
def metrics(p, mark, custody, coll_custody):
    # Rechnung wie in Jupiters eigenem Beispielcode (Rohwerte, 6 Dezimalstellen)
    size, coll, entry = p["size_raw"], p["collateral_raw"], p["price_raw"]
    is_long = p["side"] == LONG

    impact_bps = -(-size * BPS_POWER // custody["trade_impact_fee_scalar"])   # aufrunden
    close_fee = size * (custody["decrease_bps"] + impact_bps) // BPS_POWER
    open_fee_est = size * (custody["increase_bps"] + impact_bps) // BPS_POWER
    borrow_fee = (cumulative_interest_now(coll_custody) - p["interest_snapshot"]) * size // RATE_POWER
    borrow_per_day = hourly_borrow_rate(coll_custody) * 24 * size / RATE_POWER / 1e6

    max_loss = size * BPS_POWER // custody["max_leverage_bps"] + close_fee + borrow_fee
    diff = abs(max_loss - coll) * entry // size
    if is_long:
        liq = entry + diff if max_loss > coll else entry - diff
    else:
        liq = entry - diff if max_loss > coll else entry + diff

    entry_f, liq_f, size_f = entry / 1e6, liq / 1e6, size / 1e6
    pnl = size_f * (mark - entry_f) / entry_f * (1 if is_long else -1)
    net = coll / 1e6 + pnl - close_fee / 1e6 - borrow_fee / 1e6
    dist = (mark - liq_f) / mark if is_long else (liq_f - mark) / mark
    return {
        "einstieg": round(entry_f, 2),
        "groesse_usd": round(size_f, 2),
        "sicherheit_usd": round(coll / 1e6, 2),
        "hebel": round(size / coll, 1),
        "pnl_usd": round(pnl, 2),
        "eroeffnungsgebuehr_geschaetzt_usd": round(open_fee_est / 1e6, 2),
        "schliessgebuehr_usd": round(close_fee / 1e6, 2),
        "leihgebuehr_bisher_usd": round(borrow_fee / 1e6, 4),
        "leihgebuehr_pro_tag_usd": round(borrow_per_day, 3),
        "leihgebuehr_pro_tag_prozent_sicherheit": round(borrow_per_day / (coll / 1e6) * 100, 2),
        "nettowert_bei_schliessen_usd": round(net, 2),
        # wie Jupiter „Value (Inc. Fees)“: zusätzlich die Eröffnungsgebühr abgezogen
        "wert_jupiter_usd": round(net - open_fee_est / 1e6, 2),
        "liquidation": round(liq_f, 2),
        "abstand_liq_prozent": round(dist * 100, 2),
    }


# ---------------------------------------------------------------
# 6. Ablauf
# ---------------------------------------------------------------
def snapshot():
    """Liefert den kompletten aktuellen Stand als Dictionary (für alerts.py und den Bericht)."""
    owner = Pubkey.from_string(WALLET)
    cands = candidates(owner)
    accounts = rpc("getMultipleAccounts",
                   [[str(a) for _, _, a in cands], {"encoding": "base64"}])["value"]

    custodies = get_custodies()
    mark = oracle_prices(custodies)
    preisquelle = "orakel"
    ersatz = [m for m in MARKETS if m not in mark]
    if ersatz:                              # Orakel nicht lesbar/zu alt → Ersatzquelle
        mark = {**prices(), **mark}
        preisquelle = f"Ersatz-API für {', '.join(ersatz)} (Orakelalter s: {ORACLE_AGE})"
    try:
        orders = get_trigger_orders(owner)
    except Exception as e:                # öffentliches RPC lehnt diese Abfrage manchmal ab
        print(f"HINWEIS: Stop-Loss/Take-Profit nicht abrufbar ({e})")
        orders = None
    found, open_positions = 0, []
    for (market, side_name, addr), acc in zip(cands, accounts):
        if acc is None:
            continue
        found += 1
        raw = base64.b64decode(acc["data"][0])
        p = decode_position(raw)
        if acc["owner"] != str(PERPS_PROGRAM) or p["owner"] != WALLET:
            print(f"WARNUNG: {market} {side_name} – Konto passt nicht zur Wallet")
            continue
        if p["size_raw"] == 0:
            continue                      # Konto existiert, Position ist geschlossen
        pos = {"id": f"{market}-{side_name}-{str(addr)[:6]}", "adresse": str(addr),
               "markt": market, "seite": side_name, "preis_jetzt": round(mark[market], 2),
               **metrics(p, mark[market], custodies[p["custody"]],
                         custodies[p["collateral_custody"]])}
        if orders is not None:
            pos.update(tpsl_info(orders.get(str(addr), []), side_name == "Long", pos))
        else:
            pos["risiko_bis_stop_usd"] = pos["nettowert_bei_schliessen_usd"]
        pos["offen_seit_min"] = round((time.time() - p["open_time"]) / 60, 1)
        open_positions.append(pos)

    holdings, unpriced = wallet_holdings(owner)
    spot_total = sum(h["wert_usd"] for h in holdings)
    perps_total = sum(p["nettowert_bei_schliessen_usd"] for p in open_positions)

    return {
        "zeit": int(time.time()),
        "positionskonten_gefunden": found,
        "tpsl_abrufbar": orders is not None,
        "preisquelle": preisquelle,
        "offene_positionen": open_positions,
        "wallet_bestaende": holdings,
        "token_ohne_preis": unpriced,
        "summe_wallet_usd": round(spot_total, 2),
        "summe_positionen_usd": round(perps_total, 2),
        "gesamtvermoegen_usd": round(spot_total + perps_total, 2),
    }


def main():
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
