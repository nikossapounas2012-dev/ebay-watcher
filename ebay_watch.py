"""
eBay UK card price watcher (official eBay Browse API, no browser needed)
- Reads card names from ebay_cards.txt (one per line, # for comments, [Section] headers)
  and Magic: The Gathering booster boxes from mtg_boxes.txt (Telegram numbers B1, B2...)
  and Sorcery: Contested Realm Unique cards from sorcery_uniques.txt (Telegram numbers S1, S2...)
  and Frank Frazetta artwork in MTG and Sorcery from frazetta.txt (Telegram numbers F1, F2...)
- Searches eBay UK for Buy It Now Yu-Gi-Oh! singles located in the United Kingdom
- Only counts listings whose title contains the exact card name
- Records the cheapest total price (item + postage), keeps a history of every change,
  and sends a numbered summary to Telegram. Reply to the bot with a number to get that card's link.

Setup:
    1. Create a free account at https://developer.ebay.com
    2. Create a *Production* keyset and copy the App ID (Client ID) and Cert ID (Client Secret)
    3. Set them as environment variables:
         export EBAY_CLIENT_ID="your-app-id"
         export EBAY_CLIENT_SECRET="your-cert-id"

Telegram (set as environment variables):
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    - a message is sent only for alerts: under your target price ("| £90" on a line), 20%+ under
      market, or a new lowest ever (set ALERTS_ONLY = False for every price change)
    - reply 'deals' for everything at/under target or under market right now
    - reply with a number (e.g. "12" or "3 7 15") to get the eBay link(s)

Run:
    python ebay_watch.py              # loop forever
    python ebay_watch.py --once       # single pass (also answers replies first)
    python ebay_watch.py --replies    # only answer Telegram replies, no eBay check
"""

import argparse
import base64
import unicodedata
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# ---------------- settings ----------------
BASE_DIR = Path(__file__).resolve().parent   # files live next to this script, wherever it is run from
CARDS_FILE = BASE_DIR / "ebay_cards.txt"
BOXES_FILE = BASE_DIR / "mtg_boxes.txt"
SORCERY_FILE = BASE_DIR / "sorcery_uniques.txt"
FRAZETTA_FILE = BASE_DIR / "frazetta.txt"
# saved prices + history go in DATA_DIR (GitHub Actions points this at the "data" branch folder)
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
STATE_FILE = DATA_DIR / "ebay_prices.json"
HISTORY_FILE = DATA_DIR / "ebay_history.csv"

MARKETPLACE = "EBAY_GB"
ITEM_COUNTRY = "GB"              # only listings located in the UK
CATEGORY_ID = "183454"           # eBay "CCG Individual Cards" (Yu-Gi-Oh! singles live here)
BUY_IT_NOW_ONLY = True           # skip auctions (their current bid is not a real price)
RESULTS_PER_SEARCH = 200         # max allowed; many cheap results are other cards with similar names

# listings whose title contains any of these words are ignored (whole words only)
EXCLUDE_WORDS = {"japanese", "korean", "chinese", "asian", "ocg", "proxy", "orica", "custom",
                 "replica", "fake", "psa", "bgs", "cgc", "graded", "empty", "bundle", "lot", "x3", "3x",
                 "sleeve", "sleeves", "playmat", "binder", "digital", "code", "poster"}

# ---- MTG booster boxes (mtg_boxes.txt) ----
# only sealed boxes: eBay condition "New", and titles with these words are skipped
BOX_EXCLUDE_WORDS = {
    "case", "cases", "opened", "unsealed", "resealed", "used", "omega", "jumpstart", "prerelease",
    "draft", "commander", "deck", "decks", "starter", "sample", "topper", "x2", "2x", "x4", "4x",
    "damage", "damaged", "dented", "crushed", "torn",
    # English only
    "german", "deutsch", "french", "francais", "italian", "italiano", "spanish", "espanol",
    "portuguese", "russian", "japanese", "japan", "jp", "jpn", "chinese", "korean",
}
BOX_EXCLUDE_PHRASES = ["no packs", "set booster", "collector booster pack", "play booster pack"]
BOX_TYPE_WORDS = {"play": {"play"}, "collector": {"collector", "collectors"}}
# a "box" cheaper than this is a single pack or a scam - also passed to eBay as a price filter
BOX_MIN_PRICE = {"play": 50, "collector": 120}

# ---- Sorcery: Contested Realm Uniques (sorcery_uniques.txt) ----
# Sorcery has few UK listings, so instead of one search per card, these broad searches are read
# page by page (~20 eBay calls per check) and every listing is matched to the card lines.
SORCERY_QUERIES = ["sorcery contested realm", "sorcery tcg", "sorcery gothic", "sorcery arthurian",
                   "sorcery dragonlord"]
SORCERY_MAX_RESULTS = 3000           # per query (15 pages of 200)
SORCERY_SET_WORDS = {"Alpha": {"alpha"}, "Beta": {"beta"}, "Arthurian Legends": {"arthurian"},
                     "Gothic": {"gothic"}, "Dragonlord": {"dragonlord"}}
SORCERY_EXCLUDE_WORDS = {"playset", "x2", "2x", "x3", "3x", "x4", "4x", "deck", "decks", "precon",
                         "booster", "box", "pack", "packs", "bulk", "collection", "joblot",
                         "book", "paperback", "hardcover", "novel", "grimoire"}
# a Sorcery card title says one of these (books and games also use the word "sorcery")
SORCERY_GAME_WORDS = {"contested", "realm", "tcg", "gothic", "arthurian", "dragonlord"}
SORCERY_CARDS_URL = "https://api.sorcerytcg.com/api/cards"
SORCERY_NAMES_FILE = DATA_DIR / "sorcery_names.json"   # every card name, to skip longer names

# ---- Frank Frazetta artwork (frazetta.txt) ----
# Sorcery lines are matched from the shared Sorcery listings above; each MTG card gets one search.
FRAZETTA_EXCLUDE_WORDS = {"proxy", "custom", "altered", "signed", "print", "prints", "poster", "canvas",
                          "framed", "book", "lot", "bundle", "complete", "sealed", "playset", "deck",
                          "x2", "2x", "x3", "3x", "x4", "4x"}

# "25th Anniversary Edition" reprints reuse the original set code (e.g. MRD-EN138, PSV-EN000).
# A 4th column in ebay_cards.txt keeps them apart: "original" skips titles with these words,
# "25th" only counts titles that have one of them.
ANNIVERSARY_WORDS = {"anniversary", "20th", "25th"}

# the same set under a different code in Europe and North America
PREFIX_ALIASES = {"SRL": ["MRL"], "MRL": ["SRL"]}

# how each rarity is written in titles, and words that mean it is a *different* rarity
RARITY_WORDS = {
    "secret rare":   ({"secret", "scr"}, {"starlight", "platinum", "prismatic", "quarter", "collectors", "ultimate"}),
    "ultra rare":    ({"ultra", "ur"}, {"ultimate", "secret", "ghost", "scr", "utr"}),
    "ultimate rare": ({"ultimate", "utr"}, set()),
    "ghost rare":    ({"ghost"}, set()),
    "ghost/gold rare": ({"ghost"}, set()),
}
RARITY_SHORT = {"secret rare": "ScR", "ultra rare": "UR", "ultimate rare": "UtR",
                "ghost rare": "GR", "ghost/gold rare": "GGR"}

# other official card names that contain a watched name (e.g. "Spirit of the Pot of Greed",
# "Mirror Force Launcher") are looked up once from YGOPRODeck and cached here
NAMES_FILE = DATA_DIR / "card_names.json"
YGOPRODECK_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"

# Cardmarket + TCGplayer reference prices are saved once a day (see references.py).
# Cardmarket versions that can't be matched automatically are set by hand in this file.
REFERENCES = True
CARDMARKET_IDS_FILE = BASE_DIR / "cardmarket_ids.txt"

# Telegram only messages when something is worth buying (an "alert"), when a listing gets cheaper and:
#   - it is at or under the target price on its line ("| £90" anywhere after the name), or
#   - it is DEAL or less of the market price (Cardmarket trend; boxes, Sorcery and Frazetta also
#     TCGplayer, which is matched to the exact product there - Yu-Gi-Oh! TCGplayer prices are often
#     for another edition), or
#   - it is at least LOWEST_MIN_DROP under the lowest price ever recorded for it
#   The last two only for things worth ALERT_MIN_PRICE or more, so cheap cards don't flood the chat.
ALERTS_ONLY = True               # False = also send every price change after every pass (the old summary)
DEAL = 0.8                       # 0.8 = 20% or more under the market price
LOWEST_MIN_DROP = 0.10           # 10% under the lowest ever (so a few pence cheaper doesn't alert)
ALERT_MIN_PRICE = 10.0           # £
CHECK_INTERVAL_MIN = 30          # minutes between full passes (only when looping locally)
PARALLEL_SEARCHES = 5            # eBay searches at the same time (GitHub bills every started minute)
DELAY_BETWEEN_CARDS = (0.2, 0.6) # seconds, random pause before each search
# ------------------------------------------

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_token = {"value": None, "expires": 0}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_cards():
    """
    Returns a list of entries. Numbers in Telegram are the position in this list (from 1).
    A line is either "Card name" (any printing) or "Card name | SET-CODE | Rarity" (one printing).
    """
    if not CARDS_FILE.exists():
        sys.exit(f"{CARDS_FILE} not found")
    cards, section = [], ""
    for line in CARDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        line, target = split_target(line)   # "| £90" = alert at or under £90
        parts = [p.strip() for p in line.split("|")] + ["", "", ""]
        name, code, rarity, edition = parts[0], parts[1].upper(), parts[2].lower(), parts[3].lower()
        if rarity and rarity not in RARITY_WORDS:
            sys.exit(f"Unknown rarity '{parts[2]}' in: {line}  (use one of: {', '.join(RARITY_WORDS)})")
        if edition not in ("", "original", "25th"):
            sys.exit(f"Unknown 4th column '{parts[3]}' in: {line}  (use original or 25th)")
        label = name
        if code:
            label += f" · {code}" + (f" {RARITY_SHORT[rarity]}" if rarity else "")
        if edition == "25th":
            label += " (25th)"
        cards.append({"kind": "card", "num": str(len(cards) + 1), "section": section, "name": name,
                      "code": code, "rarity": rarity, "edition": edition, "key": line, "target": target, "label": label})
    return cards


def load_boxes():
    """MTG booster boxes: "Set name | SET | Play or Collector | eBay name / other eBay name"."""
    if not BOXES_FILE.exists():
        return []
    boxes, section = [], ""
    for line in BOXES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        line, target = split_target(line)   # "| £90" = alert at or under £90
        parts = [p.strip() for p in line.split("|")] + ["", "", ""]
        name, set_code, box = parts[0], parts[1].upper(), parts[2].lower()
        if box not in BOX_TYPE_WORDS:
            sys.exit(f"Unknown box type '{parts[2]}' in: {line}  (use Play or Collector)")
        ebay_names = [n.strip() for n in (parts[3] or name).split("/") if n.strip()]
        boxes.append({"kind": "box", "num": f"B{len(boxes) + 1}", "section": section, "name": name,
                      "set": set_code, "box": box, "ebay_names": ebay_names,
                      "code": "", "rarity": "", "edition": "", "key": line, "target": target,
                      "label": f"{name} {box.title()} box"})
    return boxes


def load_sorcery():
    """Sorcery Uniques: "Card name | Set | Standard or Foil"."""
    if not SORCERY_FILE.exists():
        return []
    entries, section = [], ""
    for line in SORCERY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        line, target = split_target(line)   # "| £90" = alert at or under £90
        parts = [p.strip() for p in line.split("|")] + ["", ""]
        name, set_name, finish = parts[0], parts[1], parts[2].lower()
        if set_name not in SORCERY_SET_WORDS or finish not in ("standard", "foil"):
            sys.exit(f"Check this line in {SORCERY_FILE.name}: {line}  (set: {', '.join(SORCERY_SET_WORDS)}; Standard or Foil)")
        entries.append({"kind": "sorcery", "num": f"S{len(entries) + 1}", "section": section, "name": name,
                        "set": set_name, "foil": finish == "foil", "code": "", "rarity": "", "edition": "",
                        "key": line, "target": target, "label": f"{name} ({set_name}{', foil' if finish == 'foil' else ''})"})
    return entries


def load_frazetta():
    """Frazetta art: "Game | Card | Printing | Standard or Foil | eBay words | TCGplayer | Cardmarket"."""
    if not FRAZETTA_FILE.exists():
        return []
    entries, section = [], ""
    for line in FRAZETTA_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        line, target = split_target(line)   # "| £90" = alert at or under £90
        parts = [p.strip() for p in line.split("|")] + ["", "", "", ""]
        game, name, printing, finish, words = parts[0], parts[1], parts[2], parts[3].lower(), parts[4]
        if game not in ("MTG", "Sorcery") or finish not in ("standard", "foil"):
            sys.exit(f"Check this line in {FRAZETTA_FILE.name}: {line}  (game MTG or Sorcery; Standard or Foil)")
        tokens = words.split()
        entries.append({"kind": "frazetta", "num": f"F{len(entries) + 1}", "section": section, "game": game,
                        "name": name, "printing": printing, "foil": finish == "foil",
                        "want": [w.lower() for t in tokens if not t.startswith("-") for w in t.split("/") if w],
                        "avoid": [t[1:].lower() for t in tokens if t.startswith("-")],
                        "tcgplayer": parts[5], "cardmarket": parts[6],
                        "code": "", "rarity": "", "edition": "", "key": line, "target": target,
                        "label": f"{name} ({game} {printing}{', foil' if finish == 'foil' else ''})"})
    return entries


def load_all():
    return load_cards() + load_boxes() + load_sorcery() + load_frazetta()


TARGET_RE = re.compile(r"\s*\|\s*(?:max\s*)?£\s*(\d+(?:\.\d+)?)\s*(?=\||$)", re.I)


def split_target(line):
    """'Bloomburrow | BLB | Play | £200' -> ('Bloomburrow | BLB | Play', 200.0).
    The target is left out of the line's key, so adding or changing one keeps the item's history."""
    m = TARGET_RE.search(line)
    if not m:
        return line, None
    return " | ".join(p.strip() for p in (line[:m.start()] + line[m.end():]).split("|")), float(m.group(1))


def lowest_ever():
    """Lowest eBay price recorded for each line, from the history file."""
    lows = {}
    if HISTORY_FILE.exists():
        with HISTORY_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["price"]:
                    price = float(row["price"])
                    lows[row["card"]] = min(price, lows.get(row["card"], price))
    return lows


def market_prices(entries):
    """Latest market price per line: Cardmarket trend, else TCGplayer (not for Yu-Gi-Oh! cards).
    Returns {key: (price in £, "Cardmarket" or "TCGplayer")}."""
    kinds = {e["key"]: e["kind"] for e in entries}
    latest = {}
    path = DATA_DIR / "reference_history.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):      # oldest first, so later rows win
                latest.setdefault(row["card"], {})[row["source"]] = float(row["price"]) if row["price"] else None
    out = {}
    for key, sources in latest.items():
        for source, label in (("cardmarket", "Cardmarket"), ("tcgplayer", "TCGplayer")):
            if source == "tcgplayer" and kinds.get(key, "card") == "card":
                continue
            if sources.get(source):
                out[key] = (sources[source], label)
                break
    return out


def alert_reasons(entry, offer, prev, lows, markets):
    """Why this listing is worth a message (empty list = no alert). Only when it got cheaper or is new,
    so the same listing at the same price never alerts twice."""
    price = offer["price"]
    if prev is not None and price >= prev["price"]:
        return []
    reasons = []
    if entry.get("target") and price <= entry["target"]:
        reasons.append(f"🎯 at/under your £{entry['target']:g}")
    market = markets.get(entry["key"])
    if market and market[0] >= ALERT_MIN_PRICE and price <= DEAL * market[0]:
        reasons.append(f"💰 {1 - price / market[0]:.0%} under {market[1]} £{market[0]:.2f}")
    low = lows.get(entry["key"])
    if low is not None and low >= ALERT_MIN_PRICE and price <= low * (1 - LOWEST_MIN_DROP):
        reasons.append(f"📉 lowest ever (before: £{low:.2f})")
    return reasons


# ---------------- set codes ----------------
def split_code(code):
    """'MRD-EN138' -> ('MRD', '138'); 'CT05-ENS03' -> ('CT05', 'S03'); 'IOC-SE3' -> ('IOC', 'SE3')."""
    prefix, _, suffix = code.partition("-")
    if suffix.startswith("EN"):
        suffix = suffix[2:]
    elif suffix.startswith("E") and suffix[1:2].isdigit():
        suffix = suffix[1:]
    return prefix, suffix


def code_prefixes(code):
    prefix, _ = split_code(code)
    return [prefix] + PREFIX_ALIASES.get(prefix, [])


def code_query(code):
    """Old cards are listed as MRD-138, MRD-E138 or MRD-EN138 - search all of them at once."""
    _, number = split_code(code)
    variants = [f"{p}-{region}{number}" for p in code_prefixes(code) for region in ("", "EN", "E")]
    return "(" + ", ".join(variants) + ")"


def title_has_code(title, code):
    _, number = split_code(code)
    prefixes = "|".join(re.escape(p.lower()) for p in code_prefixes(code))
    pattern = rf"(?<![a-z0-9])(?:{prefixes})\s*-?\s*(?:en|e)?\s*{re.escape(number.lower())}(?![0-9])"
    return re.search(pattern, title.lower()) is not None


def title_has_rarity(title, rarity):
    wanted, other = RARITY_WORDS[rarity]
    words = set(words_of(title))
    return bool(words & wanted) and not (words & other)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


HISTORY_COLUMNS = ["time", "card", "price", "item", "postage", "previous", "seller", "title", "url"]


def add_history(name, offer, prev):
    """Append one row to the history CSV whenever a card's cheapest price changes (or is first seen).
    offer=None writes a row with an empty price: the card is no longer listed (a gap in the charts)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(HISTORY_COLUMNS)
        if offer is None:
            writer.writerow([datetime.now().isoformat(timespec="seconds"), name, "", "", "",
                             "" if prev is None else f"{prev['price']:.2f}", "", "", ""])
            return
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"), name, f"{offer['price']:.2f}",
            f"{offer['item']:.2f}", "" if offer["postage"] is None else f"{offer['postage']:.2f}",
            "" if prev is None else f"{prev['price']:.2f}",
            offer["seller"], offer["title"], offer["url"],
        ])


# ---------------- title matching ----------------
def words_of(text):
    """Lower-case words, with '.' and apostrophes removed so 'D.D.' -> 'dd' and "Gravekeeper's" -> 'gravekeepers'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()   # "á" -> "a"
    return re.findall(r"[a-z0-9]+", re.sub(r"[.'’]", "", text.lower()))


def contains_phrase(words, phrase):
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


_names_cache = None


def longer_names(name):
    """Official card names that contain this name, e.g. 'Cyber Dragon' -> ['Cyber Dragon Infinity', ...]."""
    global _names_cache
    if _names_cache is None:
        _names_cache = json.loads(NAMES_FILE.read_text(encoding="utf-8")) if NAMES_FILE.exists() else {}
    if name not in _names_cache:
        try:
            req = urllib.request.Request(f"{YGOPRODECK_URL}?{urllib.parse.urlencode({'fname': name})}",
                                         headers={"User-Agent": "ebay-card-watcher/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                found = [card["name"] for card in json.load(r).get("data", [])]
        except urllib.error.HTTPError as e:
            if e.code != 400:     # 400 = no card with this name
                raise
            found = []
        own = words_of(name)
        if not any(words_of(n) == own for n in found):
            log(f"warning: '{name}' is not an exact card name on YGOPRODeck, check the spelling")
        _names_cache[name] = [n for n in found if words_of(n) != own and contains_phrase(words_of(n), own)]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        NAMES_FILE.write_text(json.dumps(_names_cache, indent=1, ensure_ascii=False), encoding="utf-8")
    return _names_cache[name]


def title_matches(title, name):
    """True if the title has the exact card name and none of the longer card names that contain it."""
    words = words_of(title)
    if not contains_phrase(words, words_of(name)):
        return False
    return not any(contains_phrase(words, words_of(other)) for other in longer_names(name))


# ---------------- eBay API ----------------
def get_token():
    """Application token (client credentials). Valid ~2 hours, reused until it expires."""
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]

    client_id = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET first (see the setup notes at the top)")

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    _token["value"] = body["access_token"]
    _token["expires"] = time.time() + body.get("expires_in", 7200)
    return _token["value"]


def search(query, category=CATEGORY_ID, min_price=None, new_only=False, offset=0, with_total=False):
    filters = [f"itemLocationCountry:{ITEM_COUNTRY}", "priceCurrency:GBP"]
    if BUY_IT_NOW_ONLY:
        filters.append("buyingOptions:{FIXED_PRICE}")
    if min_price:
        filters.append(f"price:[{min_price}..]")
    if new_only:
        filters.append("conditions:{NEW}")
    params = {
        "q": query,
        "filter": ",".join(filters),
        "sort": "price",          # cheapest first
        "limit": RESULTS_PER_SEARCH,
        "offset": offset,
    }
    if category:
        params["category_ids"] = category
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    items = body.get("itemSummaries", [])
    return (items, body.get("total", 0)) if with_total else items


def shipping_cost(item):
    options = item.get("shippingOptions") or []
    if not options:
        return None   # unknown (often "collection only" or calculated postage)
    cost = options[0].get("shippingCost")
    return float(cost["value"]) if cost else 0.0


def listing_matches(title, entry):
    words = set(words_of(title))
    if EXCLUDE_WORDS & words:
        return False
    if entry["edition"] == "original" and ANNIVERSARY_WORDS & words:
        return False
    if entry["edition"] == "25th" and not ANNIVERSARY_WORDS & words:
        return False
    if entry["code"]:
        # set code + rarity identify the printing; names in titles are often wrong or shortened
        if not title_has_code(title, entry["code"]):
            return False
        return not entry["rarity"] or title_has_rarity(title, entry["rarity"])
    return title_matches(title, entry["name"])


def box_listing_matches(title, entry):
    """Title has one of the box's eBay names, its type (play/collector) and box/display - nothing else."""
    words = set(words_of(title))
    text = " ".join(words_of(title))
    if (EXCLUDE_WORDS | BOX_EXCLUDE_WORDS) & words or any(p in text for p in BOX_EXCLUDE_PHRASES):
        return False
    if not any(contains_phrase(words_of(title), words_of(n)) for n in entry["ebay_names"]):
        return False
    if not {"box", "display"} & words:
        return False
    wanted = BOX_TYPE_WORDS[entry["box"]]
    others = set().union(*(w for t, w in BOX_TYPE_WORDS.items() if t != entry["box"]))
    return bool(wanted & words) and not others & words


def why_not_box(title, entry):
    """Dry-run help: the reason a title is skipped, or '' if it counts."""
    words, text = set(words_of(title)), " ".join(words_of(title))
    bad = (EXCLUDE_WORDS | BOX_EXCLUDE_WORDS) & words
    if bad:
        return "word: " + ", ".join(sorted(bad))
    phrase = next((p for p in BOX_EXCLUDE_PHRASES if p in text), None)
    if phrase:
        return "phrase: " + phrase
    if not any(contains_phrase(words_of(title), words_of(n)) for n in entry["ebay_names"]):
        return "no set name"
    if not {"box", "display"} & words:
        return "no box/display"
    return "" if box_listing_matches(title, entry) else "wrong box type"


def cheapest_box(entry):
    """MTG boxes: one search per eBay name, any category, sealed (New) only, above the pack price."""
    type_query = "play" if entry["box"] == "play" else "(collector, collectors)"
    items, seen = [], set()
    for name in entry["ebay_names"]:
        for item in search(f"{name} {type_query} booster", category=None,
                           min_price=BOX_MIN_PRICE[entry["box"]], new_only=True):
            if item.get("itemId") not in seen:
                seen.add(item.get("itemId"))
                items.append(item)
    return cheapest_of(items, lambda title: box_listing_matches(title, entry))


def sorcery_names():
    """Every Sorcery card name (any rarity), cached for a week - used to skip longer names."""
    if SORCERY_NAMES_FILE.exists() and time.time() - SORCERY_NAMES_FILE.stat().st_mtime < 7 * 86400:
        return json.loads(SORCERY_NAMES_FILE.read_text(encoding="utf-8"))
    try:
        req = urllib.request.Request(SORCERY_CARDS_URL, headers={"User-Agent": "ebay-card-watcher/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            names = sorted({card["name"] for card in json.load(r)})
    except Exception as e:
        if SORCERY_NAMES_FILE.exists():
            return json.loads(SORCERY_NAMES_FILE.read_text(encoding="utf-8"))
        log(f"Sorcery card names not loaded ({e}) - longer names are not skipped")
        return []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SORCERY_NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    return names


def title_is_foil(words):
    """'Foil' -> True; 'Non-Foil', 'Nonfoil', 'Non Foil', 'not foil', 'Regular' -> False."""
    text = " ".join(words)
    if "nonfoil" in words or re.search(r"\b(non|no|not) foil", text):
        return False
    return "foil" in words or "foils" in words


def sorcery_matches(title, entry, longer):
    words = words_of(title)
    found = set(words)
    extra = found - set(words_of(entry["name"]))      # a card may be called "... Box"
    if (EXCLUDE_WORDS | SORCERY_EXCLUDE_WORDS) & extra or "sorcery" not in found:
        return False
    if not SORCERY_GAME_WORDS & found:
        return False
    if not contains_phrase(words, words_of(entry["name"])):
        return False
    if any(contains_phrase(words, words_of(other)) for other in longer):
        return False
    other_sets = set().union(*(w for s, w in SORCERY_SET_WORDS.items() if s != entry["set"]))
    if other_sets & found and not SORCERY_SET_WORDS[entry["set"]] & found:
        return False   # names a different set (Alpha, Beta...)
    return title_is_foil(words) == entry["foil"]


def sorcery_listings():
    """Every UK Buy It Now Sorcery listing from the shared searches: (items, None) or (None, error text)."""
    try:
        items, seen = [], set()
        for query in SORCERY_QUERIES:
            offset = 0
            while offset < SORCERY_MAX_RESULTS:
                page, total = search(query, category=None, min_price=1, offset=offset, with_total=True)
                for item in page:
                    if item.get("itemId") not in seen:
                        seen.add(item.get("itemId"))
                        items.append(item)
                offset += RESULTS_PER_SEARCH
                if not page or offset >= total:
                    break
        log(f"Sorcery: {len(items)} UK listings read")
        return items, None
    except urllib.error.HTTPError as e:
        log(f"Sorcery: eBay error {e.code} — {e.read().decode(errors='ignore')[:300]}")
        return None, f"eBay error {e.code}"
    except Exception as e:
        log(f"Sorcery: error — {e}")
        return None, "error"


def longer_sorcery_names(name, names):
    own = words_of(name)
    return [n for n in names if words_of(n) != own and contains_phrase(words_of(n), own)]


def sorcery_results(entries, pool):
    """(offer, error) for each Sorcery line, from the shared listings."""
    items, error = pool
    if error:
        return [(None, error)] * len(entries)
    names = sorcery_names() if entries else []
    results = []
    for e in entries:
        longer = longer_sorcery_names(e["name"], names)    # once per line, not once per listing
        results.append((cheapest_of(items, lambda title: sorcery_matches(title, e, longer)), None))
    return results


def frazetta_matches(title, entry, longer=()):
    words = words_of(title)
    found = set(words)
    extra = found - set(words_of(entry["name"]))
    if (EXCLUDE_WORDS | FRAZETTA_EXCLUDE_WORDS) & extra:
        return False
    if not contains_phrase(words, words_of(entry["name"])):
        return False
    if any(contains_phrase(words, words_of(other)) for other in longer):
        return False
    if entry["want"] and not set(entry["want"]) & found:
        return False
    if set(entry["avoid"]) & found:
        return False
    if entry["game"] == "Sorcery" and not ("sorcery" in found and (SORCERY_GAME_WORDS | {"alpha", "promo"}) & found):
        return False
    return title_is_foil(words) == entry["foil"]


def frazetta_results(entries, pool):
    """Sorcery lines from the shared Sorcery listings; one eBay search per MTG card name."""
    results = {}
    sorcery = [e for e in entries if e["game"] == "Sorcery"]
    if sorcery:
        items, error = pool
        names = sorcery_names() if not error else []
        for e in sorcery:
            if error:
                results[e["key"]] = (None, error)
                continue
            longer = longer_sorcery_names(e["name"], names)  # once per line, not once per listing
            results[e["key"]] = (cheapest_of(items, lambda t: frazetta_matches(t, e, longer)), None)
    mtg_names = sorted({e["name"] for e in entries if e["game"] == "MTG"})
    found = {}
    for name in mtg_names:
        try:
            found[name] = (search(f"frazetta {name}", category=None, min_price=1), None)
        except urllib.error.HTTPError as e:
            found[name] = (None, f"eBay error {e.code}")
        except Exception as e:
            found[name] = (None, "error")
    for e in entries:
        if e["game"] == "MTG":
            items, error = found[e["name"]]
            results[e["key"]] = (None, error) if error else (
                cheapest_of(items, lambda t: frazetta_matches(t, e)), None)
    return [results[e["key"]] for e in entries]


def cheapest_listing(entry):
    if entry["kind"] == "box":
        return cheapest_box(entry)
    query = code_query(entry["code"]) if entry["code"] else entry["name"]
    return cheapest_of(search(query), lambda title: listing_matches(title, entry))


def cheapest_of(items, matches):
    best = None
    for item in items:
        title = item.get("title", "")
        if not matches(title):
            continue
        price = item.get("price") or {}
        if price.get("currency") != "GBP":
            continue
        postage = shipping_cost(item)
        total = float(price["value"]) + (postage or 0.0)
        if best is None or total < best["price"]:
            best = {
                "price": round(total, 2),
                "item": float(price["value"]),
                "postage": postage,
                "title": title,
                "condition": item.get("condition", ""),
                "seller": (item.get("seller") or {}).get("username", ""),
                "url": item.get("itemWebUrl", "").split("?")[0],   # drop eBay's tracking parameters
                "image": ((item.get("thumbnailImages") or [{}])[0].get("imageUrl")
                          or (item.get("image") or {}).get("imageUrl", "")),
                "first_edition": bool({"1st", "first"} & set(words_of(title))),
            }
    return best


# ---------------- Telegram ----------------
telegram_failed = False   # set when a message could not be sent, so the run can be marked as failed


def telegram_config():
    return os.getenv("TELEGRAM_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_call(method, params):
    token, _ = telegram_config()
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=20) as r:
        return json.load(r)


def send_telegram(text, preview=False):
    global telegram_failed
    token, chat = telegram_config()
    if not (token and chat):
        missing = [name for name, value in (("TELEGRAM_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)) if not value]
        log(f"Telegram not sent: {', '.join(missing)} not set")
        return
    try:
        telegram_call("sendMessage", {"chat_id": chat, "text": text,
                                      "disable_web_page_preview": "false" if preview else "true"})
        log("Telegram sent")
    except urllib.error.HTTPError as e:
        telegram_failed = True
        log(f"Telegram failed: {e.code} — {e.read().decode(errors='ignore')[:200]}")
    except Exception as e:
        telegram_failed = True
        log(f"Telegram failed: {e}")


def send_long(header, lines):
    """Telegram allows 4096 characters per message, so long lists are split."""
    message = header
    for line in lines:
        if len(message) + len(line) > 3900:
            send_telegram(message)
            message = header
        message += line + "\n"
    send_telegram(message)


def answer_replies(state):
    """Reply to messages like '12' or '3 7 15' with the eBay link of those card numbers."""
    token, chat = telegram_config()
    if not (token and chat):
        return
    try:
        updates = telegram_call("getUpdates", {"timeout": 0}).get("result", [])
    except Exception as e:
        log(f"Telegram replies not read: {e}")
        return
    if not updates:
        return

    cards = load_all()
    by_num = {entry["num"]: entry for entry in cards}
    for update in updates:
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != chat:
            continue   # only answer you, not anyone else who finds the bot
        text = message.get("text", "").strip()
        if text.lower().strip() in ("deals", "deal", "alerts"):
            send_deals(cards, state)
            log(f"Answered reply: {text}")
            continue
        if text.lower().startswith("list"):
            send_list(cards, state, text[4:].strip())
            log(f"Answered reply: {text}")
            continue
        numbers = [n.upper() for n in re.findall(r"\b[bBsSfF]?\d+\b", text)]
        if not numbers:
            send_telegram("Reply with a number (e.g. 12, or 3 7 15, B4 for a booster box, S20 for a Sorcery Unique, "
                          "F3 for Frazetta art) "
                          "to get the eBay link.\nReply 'list' for everything, 'list cards', 'list boxes', "
                          "'list sorcery', 'list frazetta', or e.g. 'list goat' for one section.\n"
                          "Reply 'deals' for everything at/under its target price or 20%+ under market.")
            continue
        lines = []
        for n in numbers[:10]:
            entry = by_num.get(n)
            if not entry:
                lines.append(f"{n}: nothing with that number")
                continue
            offer = state.get(entry["key"])
            if not offer:
                lines.append(f"{n}. {entry['label']}: no listing found at the last check")
                continue
            checked = offer.get("checked", "")[11:16]
            lines.append(f"{n}. {entry['label']} — £{offer['price']:.2f} (at {checked})\n"
                         f"{offer['title']}\n{offer['url']}")
        send_telegram("\n\n".join(lines), preview=len(lines) == 1)
        log(f"Answered reply: {' '.join(numbers[:10])}")

    # tell Telegram these messages are handled, so they are not answered again next time
    try:
        telegram_call("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
    except Exception as e:
        log(f"Telegram confirm failed: {e}")


def send_list(cards, state, section_filter=""):
    """Full numbered list with the latest prices, optionally only sections containing the filter word."""
    lines, section_shown = [], None
    word = section_filter.lower()
    kinds = {"cards": "card", "card": "card", "yugioh": "card", "boxes": "box", "box": "box", "mtg": "box",
             "sorcery": "sorcery", "uniques": "sorcery", "frazetta": "frazetta"}
    for entry in cards:
        if word in kinds:
            if entry["kind"] != kinds[word]:
                continue
        elif word and word not in entry["section"].lower():
            continue
        if entry["section"] != section_shown:
            lines.append(f"\n{entry['section']}")
            section_shown = entry["section"]
        offer = state.get(entry["key"])
        price = f"£{offer['price']:.2f}" if offer else "none found"
        lines.append(f"{entry['num']}. {entry['label']}: {price}")
    if not lines:
        send_telegram(f"No section matches '{section_filter}'.")
        return
    send_long("Latest prices\n", lines + ["\nReply with a number for the eBay link."])


def send_deals(cards, state):
    """Everything listed now at/under its target or DEAL or less of the market price, best deal first."""
    markets, rows = market_prices(cards), []
    for entry in cards:
        offer = state.get(entry["key"])
        if not offer:
            continue
        reasons, price = [], offer["price"]
        if entry.get("target") and price <= entry["target"]:
            reasons.append(f"🎯 £{entry['target']:g}")
        market = markets.get(entry["key"])
        ratio = price / market[0] if market else 1
        if market and market[0] >= ALERT_MIN_PRICE and ratio <= DEAL:
            reasons.append(f"💰 {1 - ratio:.0%} under {market[1]} £{market[0]:.2f}")
        if reasons:
            rows.append((ratio, f"{entry['num']}. {entry['label']}: £{price:.2f} · {' · '.join(reasons)}"))
    if not rows:
        send_telegram("Nothing is at/under its target or 20%+ under market right now.")
        return
    send_long(f"{len(rows)} deals now\n", [r for _, r in sorted(rows)] + ["\nReply a number for the link."])


def describe(offer):
    postage = "postage unknown" if offer["postage"] is None else f"incl. £{offer['postage']:.2f} postage"
    return f"£{offer['price']:.2f} ({postage}) — {offer['title']} [{offer['seller']}]"


# ---------------- main loop ----------------
def fetch_offer(entry):
    """Returns (offer, error text). Runs in parallel threads."""
    time.sleep(random.uniform(*DELAY_BETWEEN_CARDS))
    try:
        return cheapest_listing(entry), None
    except urllib.error.HTTPError as e:
        log(f"{entry['label']}: eBay error {e.code} — {e.read().decode(errors='ignore')[:300]}")
        return None, f"eBay error {e.code}"
    except Exception as e:
        log(f"{entry['label']}: error — {e}")
        return None, "error"


def run_pass(state):
    cards = load_all()
    get_token()
    for entry in cards:         # fill the card-name cache first (one file, so not in parallel)
        if entry["kind"] == "card" and not entry["code"]:
            longer_names(entry["name"])
    single = [c for c in cards if c["kind"] in ("card", "box")]
    with ThreadPoolExecutor(max_workers=PARALLEL_SEARCHES) as pool:
        by_key = dict(zip((c["key"] for c in single), pool.map(fetch_offer, single)))
    sorcery = [c for c in cards if c["kind"] == "sorcery"]
    frazetta = [c for c in cards if c["kind"] == "frazetta"]
    pool = sorcery_listings() if sorcery or any(c["game"] == "Sorcery" for c in frazetta) else ([], None)
    by_key.update(zip((c["key"] for c in sorcery), sorcery_results(sorcery, pool)))
    by_key.update(zip((c["key"] for c in frazetta), frazetta_results(frazetta, pool)))
    results = [by_key[c["key"]] for c in cards]

    drops, rises, new, gone, errors, alerts = [], [], [], [], [], []
    found = 0
    lows, markets = lowest_ever(), market_prices(cards)
    for entry, (offer, error) in zip(cards, results):
        label, key, number = entry["label"], entry["key"], entry["num"]
        if error:
            errors.append(f"{number}. {label}: {error}")
            continue
        prev = state.get(key)
        if offer is None:
            log(f"{label}: no UK Buy It Now listings")
            if prev:
                gone.append(f"{number}. {label} (was £{prev['price']:.2f})")
                add_history(key, None, prev)
                state.pop(key)
            continue

        found += 1
        edition = " 1st" if entry["kind"] == "card" and offer["first_edition"] else ""
        if prev is None:
            new.append(f"{number}. {label}: £{offer['price']:.2f}{edition}")
        elif offer["price"] < prev["price"]:
            drops.append(f"{number}. {label}: £{offer['price']:.2f}{edition} (was £{prev['price']:.2f})")
        elif offer["price"] > prev["price"]:
            rises.append(f"{number}. {label}: £{offer['price']:.2f}{edition} (was £{prev['price']:.2f})")
        log(f"{label}: {describe(offer)}")
        reasons = alert_reasons(entry, offer, prev, lows, markets)
        if reasons:
            was = f" (was £{prev['price']:.2f})" if prev else ""
            alerts.append(f"{number}. {label}: £{offer['price']:.2f}{edition}{was}\n"
                          f"   {' · '.join(reasons)}\n   {offer['url']}")
        if prev is None or offer["price"] != prev["price"]:
            add_history(key, offer, prev)
        offer["checked"] = datetime.now().isoformat(timespec="seconds")
        state[key] = offer
    save_state(state)

    when = f"{datetime.now():%d %b %H:%M}"
    if ALERTS_ONLY:
        # only alerts (and errors) are sent; the website and 'list' have everything else
        if alerts or errors:
            lines = ([f"\n{a}" for a in alerts]
                     + ([f"\n⚠️ Errors: {len(errors)}"] + errors[:10] if errors else [])
                     + ["\nReply a number for details, 'deals' for everything under market or target."])
            send_long(f"🔔 eBay {when} — {len(alerts)} alert{'s' * (len(alerts) != 1)}\n", lines)
        else:
            log(f"No alerts ({len(drops)} drops, {len(new)} new) - nothing sent to Telegram")
    else:
        lines = ([f"\n🔔 Alerts"] + alerts if alerts else [])
        for title, items in (("🔻 Price drops", drops), ("🔺 Price rises", rises),
                             ("🆕 Now listed", new), ("❌ No longer listed", gone), ("⚠️ Errors", errors)):
            if not items:
                continue
            if len(items) > 25:
                lines.append(f"\n{title}: {len(items)} (reply 'list' to see them)")
            else:
                lines.append(f"\n{title}")
                lines.extend(items)
        if not lines:
            lines.append("\nNo price changes.")
        lines.append("\nReply a number for the link, or 'list' for all prices.")
        send_long(f"eBay check {when} — {found} of {len(cards)} listed\n", lines)

    if REFERENCES:
        try:
            import references
            references.update([c for c in cards if c["kind"] == "card"], DATA_DIR, CARDMARKET_IDS_FILE, log,
                              boxes=[c for c in cards if c["kind"] == "box"],
                              sorcery=[c for c in cards if c["kind"] == "sorcery"],
                              frazetta=[c for c in cards if c["kind"] == "frazetta"])
        except Exception as e:     # reference prices are extra - never stop the eBay check
            log(f"references: failed - {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--replies", action="store_true", help="only answer Telegram replies and exit")
    ap.add_argument("--dry-run", choices=["cards", "boxes", "sorcery", "frazetta", "all"],
                    help="search eBay and print the cheapest match for each line; saves and sends nothing")
    args = ap.parse_args()

    if args.dry_run:
        entries = {"cards": load_cards, "boxes": load_boxes, "sorcery": load_sorcery, "frazetta": load_frazetta,
                   "all": load_all}[args.dry_run]()
        get_token()
        single = [e for e in entries if e["kind"] in ("card", "box")]
        with ThreadPoolExecutor(max_workers=PARALLEL_SEARCHES) as threads:
            found = dict(zip((e["key"] for e in single), threads.map(fetch_offer, single)))
        sorcery = [e for e in entries if e["kind"] == "sorcery"]
        frazetta = [e for e in entries if e["kind"] == "frazetta"]
        pool = sorcery_listings() if sorcery or any(e["game"] == "Sorcery" for e in frazetta) else ([], None)
        found.update(zip((e["key"] for e in sorcery), sorcery_results(sorcery, pool)))
        found.update(zip((e["key"] for e in frazetta), frazetta_results(frazetta, pool)))
        for entry, (offer, error) in ((e, found[e["key"]]) for e in entries):
            result = error or (describe(offer) + f"\n      {offer['url']}" if offer else "no listing")
            print(f"{entry['num']:>4}. {entry['label']}: {result}", flush=True)
            if entry["kind"] == "box" and not offer and not error:
                # show what eBay returned without the condition filter, and why each was skipped
                type_query = "play" if entry["box"] == "play" else "(collector, collectors)"
                for new_only in (True, False):
                    items = search(f"{entry['ebay_names'][-1]} {type_query} booster", category=None,
                                   min_price=BOX_MIN_PRICE[entry["box"]], new_only=new_only)
                    print(f"      {len(items)} results{' (New only)' if new_only else ' (any condition)'}")
                    for item in items[:8]:
                        print(f"        £{item.get('price', {}).get('value')} [{item.get('condition')}] "
                              f"{why_not_box(item.get('title', ''), entry) or 'OK'} | {item.get('title')}")
        return

    state = load_state()
    answer_replies(state)
    if args.replies:
        sys.exit(1 if telegram_failed else 0)

    while True:
        run_pass(state)
        if args.once:
            if telegram_failed:
                sys.exit("Telegram message could not be sent (see the 'Telegram failed' line above)")
            break
        wait = CHECK_INTERVAL_MIN * 60 + random.uniform(0, 120)
        log(f"next pass in {wait/60:.0f} min")
        time.sleep(wait)
        answer_replies(state)


if __name__ == "__main__":
    main()
