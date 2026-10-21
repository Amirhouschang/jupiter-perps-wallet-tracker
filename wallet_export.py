"""
wallet_export.py
Exportiert die komplette Transaktionshistorie einer Solana-Adresse als CSV.
Nur Lesen – kein Private Key, keine Seed Phrase nötig.

Ausgabe:
  transactions_raw.jsonl     Rohdaten (Cache, Skript kann abbrechen und weiterlaufen)
  wallet_transactions.csv    eine Zeile pro Transaktion und Asset-Veränderung
"""

import os
import csv
import json
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------
# 1. Einstellungen
# ---------------------------------------------------------------
load_dotenv()
WALLET = os.getenv("WALLET_ADDRESS")
RPC_URL = os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"   # öffentlich, kostenlos
PAUSE = 0.25 if "mainnet-beta" in RPC_URL else 0.05                      # öffentliches RPC ist streng limitiert

RAW_FILE = "transactions_raw.jsonl"
CSV_FILE = "wallet_transactions.csv"

# Programm-IDs zur Einordnung
JUP_PERPS = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"   # Jupiter Perpetuals
JUP_SWAP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"    # Jupiter Aggregator v6
TOKEN_PROGRAMS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",           # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",           # Token-2022
]


# ---------------------------------------------------------------
# 2. RPC-Aufruf mit Wiederholung bei Limit/Fehler
# ---------------------------------------------------------------
def rpc(method, params, retries=8):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    wait = 1
    for _ in range(retries):
        try:
            r = requests.post(RPC_URL, json=payload, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(wait)
                wait = min(wait * 2, 30)
                continue
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except requests.RequestException:
            time.sleep(wait)
            wait = min(wait * 2, 30)
    raise RuntimeError(f"RPC fehlgeschlagen: {method}")


# ---------------------------------------------------------------
# 3. Adressen sammeln: Wallet + eigene Token-Konten
#    (manche Perps-Auszahlungen landen nur im Token-Konto)
# ---------------------------------------------------------------
def get_token_accounts(owner):
    accounts = []
    for program in TOKEN_PROGRAMS:
        res = rpc("getTokenAccountsByOwner",
                  [owner, {"programId": program}, {"encoding": "jsonParsed"}])
        accounts += [a["pubkey"] for a in res["value"]]
    return accounts


# ---------------------------------------------------------------
# 4. Alle Signaturen einer Adresse holen (je 1000, rückwärts)
# ---------------------------------------------------------------
def get_signatures(address):
    sigs, before = [], None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        batch = rpc("getSignaturesForAddress", [address, opts])
        if not batch:
            break
        sigs += [s["signature"] for s in batch]
        before = batch[-1]["signature"]
        time.sleep(PAUSE)
        if len(batch) < 1000:
            break
    return sigs


# ---------------------------------------------------------------
# 5. Transaktionen laden (mit Cache)
# ---------------------------------------------------------------
def load_cache():
    cache = {}
    if os.path.exists(RAW_FILE):
        with open(RAW_FILE) as f:
            for line in f:
                d = json.loads(line)
                cache[d["signature"]] = d["tx"]
    return cache


def fetch_transactions(signatures, cache):
    todo = [s for s in signatures if s not in cache]
    print(f"Zu laden: {len(todo)} (bereits im Cache: {len(cache)})")
    with open(RAW_FILE, "a") as f:
        for i, sig in enumerate(todo, 1):
            tx = rpc("getTransaction",
                     [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            cache[sig] = tx
            f.write(json.dumps({"signature": sig, "tx": tx}) + "\n")
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}")
            time.sleep(PAUSE)
    return cache


# ---------------------------------------------------------------
# 6. Eine Transaktion auswerten: Veränderung pro Asset für das Wallet
# ---------------------------------------------------------------
def parse_tx(sig, tx):
    if tx is None:
        return []
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = [k["pubkey"] for k in msg["accountKeys"]]
    loaded = meta.get("loadedAddresses") or {}
    all_keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])

    # Kategorie
    if JUP_PERPS in all_keys:
        category = "jupiter_perps"
    elif JUP_SWAP in all_keys:
        category = "jupiter_swap"
    else:
        category = "sonstiges"

    time_utc = datetime.fromtimestamp(tx["blockTime"], tz=timezone.utc).isoformat()
    status = "fehlgeschlagen" if meta.get("err") else "ok"
    fee_sol = meta["fee"] / 1e9 if keys and keys[0] == WALLET else 0.0

    # Gegenpartei bei einfachen SOL-Überweisungen (z. B. Hardware-Wallet)
    counterparty = ""
    instructions = list(msg["instructions"])
    for inner in meta.get("innerInstructions") or []:
        instructions += inner["instructions"]
    for ix in instructions:
        p = ix.get("parsed")
        if ix.get("program") == "system" and isinstance(p, dict) and p.get("type") == "transfer":
            info = p["info"]
            if info.get("source") == WALLET:
                counterparty = info.get("destination", "")
            elif info.get("destination") == WALLET:
                counterparty = info.get("source", "")

    rows = []

    # SOL-Veränderung (inkl. Netzwerkgebühr)
    if WALLET in all_keys:
        idx = all_keys.index(WALLET)
        change = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9
        if change != 0:
            rows.append([time_utc, sig, status, category, "SOL", change, fee_sol, counterparty])

    # Token-Veränderungen (alle Token-Konten mit owner = Wallet)
    def balances(lst):
        out = {}
        for b in lst or []:
            if b.get("owner") == WALLET:
                amt = float(b["uiTokenAmount"]["uiAmountString"] or 0)
                out[b["mint"]] = out.get(b["mint"], 0) + amt
        return out

    pre, post = balances(meta.get("preTokenBalances")), balances(meta.get("postTokenBalances"))
    for mint in set(pre) | set(post):
        change = post.get(mint, 0) - pre.get(mint, 0)
        if abs(change) > 0:
            rows.append([time_utc, sig, status, category, mint, change, 0.0, counterparty])

    return rows


# ---------------------------------------------------------------
# 7. Ablauf
# ---------------------------------------------------------------
def main():
    if not WALLET:
        raise SystemExit("WALLET_ADDRESS fehlt in .env")

    addresses = [WALLET] + get_token_accounts(WALLET)
    print(f"Adressen (Wallet + Token-Konten): {len(addresses)}")

    signatures = []
    for addr in addresses:
        signatures += get_signatures(addr)
    signatures = list(dict.fromkeys(signatures))     # Duplikate entfernen
    print(f"Signaturen gesamt: {len(signatures)}")

    cache = fetch_transactions(signatures, load_cache())

    rows = []
    for sig in signatures:
        rows += parse_tx(sig, cache.get(sig))
    rows.sort(key=lambda r: r[0])

    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "signature", "status", "category",
                    "asset", "change", "fee_sol", "counterparty"])
        w.writerows(rows)

    print(f"Fertig: {len(rows)} Zeilen → {CSV_FILE}")


if __name__ == "__main__":
    main()
