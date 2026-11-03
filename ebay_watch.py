"""
eBay UK card price watcher (official eBay Browse API, no browser needed)
- Reads card names from ebay_cards.txt (one per line, # for comments, [Section] headers)
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
    - a numbered summary is sent after every pass (turn off with TELEGRAM_EVERY_RUN = False)
    - price drops are listed at the top of the summary
    - reply with a number (e.g. "12" or "3 7 15") to get the eBay link(s)

Run:
    python ebay_watch.py              # loop forever
    python ebay_watch.py --once       # single pass (also answers replies first)
    python ebay_watch.py --replies    # only answer Telegram replies, no eBay check
"""

import argparse
import base64
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

TELEGRAM_EVERY_RUN = True        # send a summary to Telegram after every pass
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
        cards.append({"section": section, "name": name, "code": code, "rarity": rarity,
                      "edition": edition, "key": line, "label": label})
    return cards


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


def search(query):
    filters = [f"itemLocationCountry:{ITEM_COUNTRY}", "priceCurrency:GBP"]
    if BUY_IT_NOW_ONLY:
        filters.append("buyingOptions:{FIXED_PRICE}")
    params = {
        "q": query,
        "category_ids": CATEGORY_ID,
        "filter": ",".join(filters),
        "sort": "price",          # cheapest first
        "limit": RESULTS_PER_SEARCH,
    }
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("itemSummaries", [])


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


def cheapest_listing(entry):
    best = None
    query = code_query(entry["code"]) if entry["code"] else entry["name"]
    for item in search(query):
        title = item.get("title", "")
        if not listing_matches(title, entry):
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

    cards = load_cards()
    for update in updates:
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != chat:
            continue   # only answer you, not anyone else who finds the bot
        text = message.get("text", "").strip()
        if text.lower().startswith("list"):
            send_list(cards, state, text[4:].strip())
            log(f"Answered reply: {text}")
            continue
        numbers = [int(n) for n in re.findall(r"\d+", text)]
        if not numbers:
            send_telegram("Reply with a number (e.g. 12, or 3 7 15) to get the eBay link.\n"
                          "Reply 'list' for every card with its price, or e.g. 'list goat' for one section.")
            continue
        lines = []
        for n in numbers[:10]:
            if not 1 <= n <= len(cards):
                lines.append(f"{n}: no card with that number (1–{len(cards)})")
                continue
            entry = cards[n - 1]
            offer = state.get(entry["key"])
            if not offer:
                lines.append(f"{n}. {entry['label']}: no listing found at the last check")
                continue
            checked = offer.get("checked", "")[11:16]
            lines.append(f"{n}. {entry['label']} — £{offer['price']:.2f} (at {checked})\n"
                         f"{offer['title']}\n{offer['url']}")
        send_telegram("\n\n".join(lines), preview=len(lines) == 1)
        log(f"Answered reply: {numbers[:10]}")

    # tell Telegram these messages are handled, so they are not answered again next time
    try:
        telegram_call("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
    except Exception as e:
        log(f"Telegram confirm failed: {e}")


def send_list(cards, state, section_filter=""):
    """Full numbered list with the latest prices, optionally only sections containing the filter word."""
    lines, section_shown = [], None
    for number, entry in enumerate(cards, start=1):
        if section_filter and section_filter.lower() not in entry["section"].lower():
            continue
        if entry["section"] != section_shown:
            lines.append(f"\n{entry['section']}")
            section_shown = entry["section"]
        offer = state.get(entry["key"])
        price = f"£{offer['price']:.2f}" if offer else "none found"
        lines.append(f"{number}. {entry['label']}: {price}")
    if not lines:
        send_telegram(f"No section matches '{section_filter}'.")
        return
    send_long("Latest prices\n", lines + ["\nReply with a number for the eBay link."])


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
    cards = load_cards()
    get_token()
    for entry in cards:         # fill the card-name cache first (one file, so not in parallel)
        if not entry["code"]:
            longer_names(entry["name"])
    with ThreadPoolExecutor(max_workers=PARALLEL_SEARCHES) as pool:
        results = list(pool.map(fetch_offer, cards))

    drops, rises, new, gone, errors = [], [], [], [], []
    found = 0
    for number, (entry, (offer, error)) in enumerate(zip(cards, results), start=1):
        label, key = entry["label"], entry["key"]
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
        edition = " 1st" if offer["first_edition"] else ""
        if prev is None:
            new.append(f"{number}. {label}: £{offer['price']:.2f}{edition}")
        elif offer["price"] < prev["price"]:
            drops.append(f"{number}. {label}: £{offer['price']:.2f}{edition} (was £{prev['price']:.2f})")
        elif offer["price"] > prev["price"]:
            rises.append(f"{number}. {label}: £{offer['price']:.2f}{edition} (was £{prev['price']:.2f})")
        log(f"{label}: {describe(offer)}")
        if prev is None or offer["price"] != prev["price"]:
            add_history(key, offer, prev)
        offer["checked"] = datetime.now().isoformat(timespec="seconds")
        state[key] = offer
    save_state(state)

    # only changes are sent - reply "list" for everything
    lines = []
    for title, items in (("🔻 Price drops", drops), ("🔺 Price rises", rises),
                         ("🆕 Now listed", new), ("❌ No longer listed", gone), ("⚠️ Errors", errors)):
        if not items:
            continue
        if len(items) > 25:
            lines.append(f"\n{title}: {len(items)} printings (reply 'list' to see them)")
        else:
            lines.append(f"\n{title}")
            lines.extend(items)
    header = f"eBay check {datetime.now():%d %b %H:%M} — {found} of {len(cards)} printings listed\n"
    if not lines:
        lines.append("\nNo price changes.")
    lines.append("\nReply a number for the link, or 'list' for all prices.")
    if TELEGRAM_EVERY_RUN or drops:
        send_long(header, lines)

    if REFERENCES:
        try:
            import references
            references.update(cards, DATA_DIR, CARDMARKET_IDS_FILE, log)
        except Exception as e:     # reference prices are extra - never stop the eBay check
            log(f"references: failed - {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--replies", action="store_true", help="only answer Telegram replies and exit")
    args = ap.parse_args()

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
