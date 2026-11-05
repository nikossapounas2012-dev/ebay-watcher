"""
Reference prices for each watched printing, saved once a day next to the eBay history.

- Cardmarket (EU, euros): the public daily price guide (trend, lowest listing, 30-day average).
  Cardmarket's files have no set codes or rarities, so a printing is matched through YGOPRODeck's
  set name -> Cardmarket expansion. When a card has several versions in one expansion, it can't be
  told apart automatically: paste that version's Cardmarket link into cardmarket_ids.txt.
- TCGplayer (US, dollars) via tcgcsv.com: market price (from recent sales) and lowest listing.
  Products there have exact set codes and rarities, so these match automatically.

Frank Frazetta artwork (frazetta.txt) lists its exact TCGplayer and Cardmarket products, so it is not matched.
Sorcery Uniques (sorcery_uniques.txt) are matched by set and card name on both sites.
MTG booster boxes (mtg_boxes.txt) are matched by set code on TCGplayer and by product name
("<set> Play Booster Box") on Cardmarket. Cardmarket's lowest price covers every language.

All prices are converted to pounds with the day's ECB rate (frankfurter.dev).
Saved in DATA_DIR:
    reference_history.csv   one row per printing per source per day
    reference_matches.json  which product each printing was matched to (or why not)
    reference_state.json    dates of the last saved files, so each day is saved once
"""

import collections
import csv
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

UA = {"User-Agent": "ebay-card-watcher/1.0"}
CM_FILES = "https://downloads.s3.cardmarket.com/productCatalog"
CM_GUIDE = f"{CM_FILES}/priceGuide/price_guide_3.json"          # 3 = Yu-Gi-Oh!
CM_SINGLES = f"{CM_FILES}/productList/products_singles_3.json"
CM_SEALED = f"{CM_FILES}/productList/products_nonsingles_3.json"
TCG = "https://tcgcsv.com/tcgplayer/2"                          # 2 = Yu-Gi-Oh!
TCG_MTG = "https://tcgcsv.com/tcgplayer/1"                      # 1 = Magic: The Gathering
CM_MTG_GUIDE = f"{CM_FILES}/priceGuide/price_guide_1.json"      # 1 = Magic: The Gathering
CM_MTG_SEALED = f"{CM_FILES}/productList/products_nonsingles_1.json"
TCG_SORCERY = "https://tcgcsv.com/tcgplayer/77"                 # 77 = Sorcery: Contested Realm
CM_SORCERY_GUIDE = f"{CM_FILES}/priceGuide/price_guide_25.json" # 25 = Sorcery: Contested Realm
CM_SORCERY_SINGLES = f"{CM_FILES}/productList/products_singles_25.json"
CM_SORCERY_SEALED = f"{CM_FILES}/productList/products_nonsingles_25.json"
YGOPRODECK_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
ECB_RATES = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
RATES_URL = "https://api.frankfurter.dev/v1/latest?from=EUR&symbols=GBP,USD"   # backup (same ECB rates)

COLUMNS = ["date", "card", "source", "product", "price", "low", "avg30"]


# ---------------- helpers ----------------
def get(url, method="GET", timeout=60, tries=3):
    req = urllib.request.Request(url, headers=UA, method=method)
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 or attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def get_json(url, timeout=60):
    with get(url, timeout=timeout) as r:
        return json.load(r)


def norm(text):
    """Lower case, no accents or punctuation: "Yugi's World: Mega-Pack" -> "yugi s world mega pack"."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", "and")
    return " ".join(re.sub(r"[\"'’.:;!?,\-–()/]", " ", text).split())


def slug(text):
    """Cardmarket URL style: "Legendary Collection 3: Yugi's World" -> "legendary-collection-3-yugis-world"."""
    text = re.sub(r"['’]", "", text.lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def code_parts(code):
    """'MRD-EN138', 'MRD-E138' and 'MRD-138' -> ('MRD', '138'), so all regions match."""
    prefix, _, suffix = code.upper().partition("-")
    suffix = re.sub(r"^(EN|E)(?=[A-Z]?\d)", "", suffix)
    return prefix, suffix


def same_set(a, b):
    """Set names written differently by each site: every word of one appears in the other
    ('legendary collection 3 mega pack' vs 'legendary collection 3 yugi s world mega pack')."""
    a, b = set(a.split()), set(b.split())
    return bool(a) and bool(b) and (a <= b or b <= a)


# TCGplayer writes the Rarity Collection "Ultimate Rare" as "Prismatic Ultimate Rare"
TCG_RARITY = {"ultimate rare": {"ultimate rare", "prismatic ultimate rare"}}


def edition_ok(name, edition):
    """'original' skips the 25th Anniversary reprints, '25th' only keeps them."""
    is_25th = "25th" in name.lower()
    return not ((edition == "25th" and not is_25th) or (edition == "original" and is_25th))


def round2(value):
    """Prices are never 0 - Cardmarket writes 0 when it has no sales, so 0 means 'no price'."""
    return round(value, 2) if value else None


# ---------------- shared lookups ----------------
def euro_rates():
    """Pounds and dollars per 1 euro (European Central Bank daily reference rates)."""
    try:
        with get(ECB_RATES, timeout=30) as r:
            xml = r.read().decode()
        found = dict(re.findall(r"currency='([A-Z]{3})' rate='([0-9.]+)'", xml))
        return {"EUR": float(found["GBP"]), "USD": float(found["USD"])}
    except Exception:
        data = get_json(RATES_URL, timeout=30)
        return {"EUR": data["rates"]["GBP"], "USD": data["rates"]["USD"]}



def ygoprodeck_sets(names, log):
    """Card name -> YGOPRODeck card_sets (set_name, set_code, set_rarity) for every printing."""
    out, names = {}, sorted(set(names))
    for i in range(0, len(names), 20):
        query = urllib.parse.urlencode({"name": "|".join(names[i:i + 20])})
        try:
            data = get_json(f"{YGOPRODECK_URL}?{query}").get("data", [])
        except urllib.error.HTTPError as e:
            log(f"references: YGOPRODeck error {e.code}")
            continue
        for card in data:
            out[norm(card["name"])] = card.get("card_sets", [])
    return out


def printing_sets(entry, sets):
    """YGOPRODeck set names this printing belongs to (same code number and rarity)."""
    return {norm(s["set_name"]) for s in sets.get(norm(entry["name"]), [])
            if code_parts(s["set_code"]) == code_parts(entry["code"])
            and s["set_rarity"].lower() == entry["rarity"]}


# ---------------- Cardmarket ----------------
def load_overrides(path):
    """cardmarket_ids.txt: '<line from ebay_cards.txt> = <Cardmarket link or product ID>'."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.rpartition("=")
        key = " | ".join(p.strip() for p in key.split("|")).strip()
        if value.strip():
            out[key] = value.strip()
    return out


def cardmarket_match(entry, sets, catalog, override):
    """Returns (idProduct, note). note explains a missing match."""
    by_name, exp_names = catalog
    products = by_name.get(norm(entry["name"]), [])
    if override:
        return resolve_link(override, products, exp_names)

    wanted = printing_sets(entry, sets)
    if not wanted:
        return None, "printing not found on YGOPRODeck"

    def expansion_fits(exp_id):
        names = exp_names.get(exp_id, set())
        if not any(edition_ok(n, entry["edition"]) for n in names):
            return False
        return any(same_set(a, b) for a in names for b in wanted)

    candidates = [p for p in products if expansion_fits(p["idExpansion"])]
    if len(candidates) == 1:
        return candidates[0]["idProduct"], ""
    if not candidates:
        return None, "set not found on Cardmarket - add its link to cardmarket_ids.txt"
    return None, f"{len(candidates)} versions on Cardmarket - add the right link to cardmarket_ids.txt"


def resolve_link(value, products, exp_names):
    """A product ID, or a link like .../Singles/Metal-Raiders/Mirror-Force-V2-Ultra-Rare.
    Cardmarket numbers versions V1, V2... in the order they were added, which is idProduct order."""
    if value.isdigit():
        return int(value), ""
    path = urllib.parse.urlparse(value).path.strip("/").split("/")
    if "Singles" not in path or len(path) < path.index("Singles") + 3:
        return None, "cardmarket_ids.txt: not a Cardmarket card link"
    exp_slug, card_slug = path[path.index("Singles") + 1].lower(), path[path.index("Singles") + 2]
    by_exp = collections.defaultdict(list)
    for p in products:
        by_exp[p["idExpansion"]].append(p)
    named = [e for e in by_exp if any(slug(n) == exp_slug for n in exp_names.get(e, ()))]
    if not named:   # expansions Cardmarket sells no sealed product for have no known name
        unnamed = [e for e in by_exp if e not in exp_names]
        named = unnamed if len(unnamed) == 1 else []
    if len(named) != 1:
        return None, "cardmarket_ids.txt: could not find that set - try the product ID instead"
    versions = sorted(by_exp[named[0]], key=lambda p: p["idProduct"])
    v = re.search(r"-V(\d+)(?:-|$)", card_slug)
    index = int(v.group(1)) - 1 if v else 0
    if not v and len(versions) > 1:
        return None, "cardmarket_ids.txt: that link has no version (V1, V2...) - open the exact version"
    if index >= len(versions):
        return None, f"cardmarket_ids.txt: V{index + 1} not found ({len(versions)} versions)"
    return versions[index]["idProduct"], ""


def update_cardmarket(cards, sets, state, matches, rates, overrides, log):
    modified = get(CM_GUIDE, method="HEAD").headers.get("Last-Modified", "")
    if modified and modified == state.get("cardmarket"):
        return []
    guide = get_json(CM_GUIDE, timeout=180)
    singles = get_json(CM_SINGLES, timeout=180)["products"]
    sealed = get_json(CM_SEALED, timeout=180)["products"]

    by_name = collections.defaultdict(list)
    for p in singles:
        by_name[norm(p["name"])].append(p)
    exp_names = collections.defaultdict(set)
    for p in sealed:   # "Metal Raiders Booster Box" -> expansion 1016 is "metal raiders"
        name = re.sub(r"\s+(booster box|booster|display|box|case|pack|blister|tin|bundle|set)\b.*$",
                      "", p["name"], flags=re.I)
        exp_names[p["idExpansion"]].add(norm(name))
    prices = {g["idProduct"]: g for g in guide["priceGuides"]}
    day = guide.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    to_gbp = rates["EUR"]

    rows, matched = [], 0
    for entry in cards:
        product, note = cardmarket_match(entry, sets, (by_name, exp_names), overrides.get(entry["key"]))
        matches.setdefault(entry["key"], {})["cardmarket"] = {"product": product, "note": note}
        g = prices.get(product) if product else None
        if product and not g:
            matches[entry["key"]]["cardmarket"]["note"] = "no Cardmarket price for this product"
        if not g or not g.get("trend"):
            continue
        matched += 1
        rows.append([day, entry["key"], "cardmarket", product, round2(g["trend"] * to_gbp),
                     round2(g["low"] * to_gbp if g.get("low") is not None else None),
                     round2(g["avg30"] * to_gbp if g.get("avg30") is not None else None)])
    log(f"references: Cardmarket {day} - {matched} of {len(cards)} printings priced")
    state["cardmarket"] = modified
    return rows


# ---------------- TCGplayer (tcgcsv.com) ----------------
def update_tcgplayer(cards, sets, state, matches, rates, log):
    with get("https://tcgcsv.com/last-updated.txt") as r:
        updated = r.read().decode().strip()
    if updated and updated == state.get("tcgplayer"):
        return []
    groups = get_json(f"{TCG}/groups")["results"]

    def groups_for(entry):
        wanted = printing_sets(entry, sets)
        prefix = code_parts(entry["code"])[0]
        out = []
        for g in groups:
            name = norm(g["name"])
            abbr = (g.get("abbreviation") or "").upper().partition("-")[0]
            fits = abbr == prefix or any(same_set(name, w) for w in wanted)
            if fits and edition_ok(g["name"], entry["edition"]):
                out.append(g["groupId"])
        return out

    wanted_groups = {entry["key"]: groups_for(entry) for entry in cards}
    needed = sorted({g for ids in wanted_groups.values() for g in ids})

    def fetch(group_id):
        products = get_json(f"{TCG}/{group_id}/products")["results"]
        prices = get_json(f"{TCG}/{group_id}/prices")["results"]
        return group_id, products, prices

    products, prices = collections.defaultdict(list), collections.defaultdict(list)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for group_id, prods, prs in pool.map(fetch, needed):
            products[group_id] = prods
            for p in prs:
                prices[p["productId"]].append(p)

    to_gbp = rates["EUR"] / rates["USD"]      # pounds per dollar
    day = updated[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows, matched = [], 0
    for entry in cards:
        found = []
        for group_id in wanted_groups[entry["key"]]:
            for p in products[group_id]:
                ext = {e["name"]: e["value"] for e in p.get("extendedData", [])}
                if (code_parts(ext.get("Number", "")) == code_parts(entry["code"])
                        and ext.get("Rarity", "").lower() in TCG_RARITY.get(entry["rarity"], {entry["rarity"]})
                        and norm(p["name"]).startswith(norm(entry["name"]))):
                    found.append(p["productId"])
        # 1st Edition / Unlimited / Limited are priced separately: keep the cheapest, like eBay
        options = [(pr["marketPrice"], pr.get("lowPrice"), pid) for pid in found
                   for pr in prices.get(pid, []) if pr.get("marketPrice")]
        note = "" if options else ("no TCGplayer price" if found else "not found on TCGplayer")
        best = min(options) if options else None
        matches.setdefault(entry["key"], {})["tcgplayer"] = {"product": best[2] if best else None, "note": note}
        if not best:
            continue
        matched += 1
        lows = [o[1] for o in options if o[1]]
        rows.append([day, entry["key"], "tcgplayer", best[2], round2(best[0] * to_gbp),
                     round2(min(lows) * to_gbp) if lows else None, None])
    log(f"references: TCGplayer {day} - {matched} of {len(cards)} printings priced")
    state["tcgplayer"] = updated
    return rows


# ---------------- MTG booster boxes ----------------
def tcg_box_name(box):
    """TCGplayer: 'Bloomburrow - Play Booster Display'."""
    return f"{box} booster display"


def update_mtg_boxes(boxes, state, matches, rates, log, tcg_updated):
    """TCGplayer and Cardmarket prices for MTG booster boxes. Returns rows for the history."""
    rows = []
    groups = get_json(f"{TCG_MTG}/groups")["results"]
    by_code = {}
    for g in groups:
        if g.get("abbreviation"):
            by_code.setdefault(g["abbreviation"].upper(), g)

    def skip(name):
        return re.search(r"\b(case|japanese|omega|sample|bundle)\b", name, re.I)

    # TCGplayer
    if tcg_updated and tcg_updated != state.get("tcgplayer_mtg"):
        needed = sorted({by_code[b["set"]]["groupId"] for b in boxes if b["set"] in by_code})

        def fetch(group_id):
            return (group_id, get_json(f"{TCG_MTG}/{group_id}/products")["results"],
                    get_json(f"{TCG_MTG}/{group_id}/prices")["results"])

        products, prices = {}, collections.defaultdict(list)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for group_id, prods, prs in pool.map(fetch, needed):
                products[group_id] = prods
                for p in prs:
                    prices[p["productId"]].append(p)
        to_gbp = rates["EUR"] / rates["USD"]
        day, matched = tcg_updated[:10], 0
        for b in boxes:
            group = by_code.get(b["set"])
            found = [p for p in products.get(group["groupId"], []) if not skip(p["name"])
                     and norm(p["name"]).endswith(norm(tcg_box_name(b["box"])))] if group else []
            note = "" if len(found) == 1 else (f"set {b['set']} not on TCGplayer" if not group
                                               else "no such box on TCGplayer" if not found else "several products")
            product = found[0]["productId"] if len(found) == 1 else None
            pr = next((x for x in prices.get(product, []) if x.get("marketPrice") or x.get("lowPrice")), None)
            matches.setdefault(b["key"], {})["tcgplayer"] = {"product": product, "note": note if pr or note else "no TCGplayer price"}
            if not pr:
                continue
            matched += 1
            rows.append([day, b["key"], "tcgplayer", product,
                         round2(pr["marketPrice"] * to_gbp) if pr.get("marketPrice") else None,
                         round2(pr["lowPrice"] * to_gbp) if pr.get("lowPrice") else None, None])
        log(f"references: TCGplayer MTG {day} - {matched} of {len(boxes)} boxes priced")
        state["tcgplayer_mtg"] = tcg_updated

    # Cardmarket
    modified = get(CM_MTG_GUIDE, method="HEAD").headers.get("Last-Modified", "")
    if not modified or modified != state.get("cardmarket_mtg"):
        sealed = [p for p in get_json(CM_MTG_SEALED, timeout=180)["products"]
                  if p.get("categoryName") == "Magic Display" and not skip(p["name"])]
        guide = get_json(CM_MTG_GUIDE, timeout=300)
        prices = {g["idProduct"]: g for g in guide["priceGuides"]}
        day = guide.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        to_gbp, matched = rates["EUR"], 0
        for b in boxes:
            # Cardmarket: "Bloomburrow Play Booster Box", "Magic: The Gathering - FINAL FANTASY Play Booster Box"
            set_names = [norm(n) for n in [b["name"]] + ([by_code[b["set"]]["name"]] if b["set"] in by_code else [])]
            suffix = f"{b['box']} booster box"
            best = {}   # product -> fewest extra words, over both spellings of the set name
            for p in sealed:
                name = norm(p["name"])
                if not name.endswith(suffix):
                    continue
                words = set(name[: -len(suffix)].split())
                for sn in set_names:
                    if set(sn.split()) <= words:
                        extra = len(words - set(sn.split()))
                        best[p["idProduct"]] = min(extra, best.get(p["idProduct"], extra))
            cands = sorted((extra, pid) for pid, extra in best.items())
            # the closest name wins ("Foundations" over "Foundations Jumpstart"); a tie can't be decided
            product = cands[0][1] if cands and (len(cands) == 1 or cands[0][0] < cands[1][0]) else None
            note = "" if product else ("no such box on Cardmarket" if not cands else "several products on Cardmarket")
            g = prices.get(product) if product else None
            matches.setdefault(b["key"], {})["cardmarket"] = {"product": product, "note": note if g or note else "no Cardmarket price"}
            if not g or (g.get("low") is None and g.get("trend") is None):
                continue
            matched += 1
            conv = lambda v: round2(v * to_gbp) if v else None
            rows.append([day, b["key"], "cardmarket", product, conv(g.get("trend")), conv(g.get("low")), conv(g.get("avg30"))])
        log(f"references: Cardmarket MTG {day} - {matched} of {len(boxes)} boxes priced")
        state["cardmarket_mtg"] = modified
    return rows


# ---------------- Sorcery: Contested Realm Uniques ----------------
def update_sorcery(entries, state, matches, rates, log, tcg_updated):
    """TCGplayer ('Aino' / 'Aino (Foil)') and Cardmarket (one product, with foil prices) for each line."""
    rows = []

    # TCGplayer: one group per set, foils are separate products
    if tcg_updated and tcg_updated != state.get("tcgplayer_sorcery"):
        groups = {norm(g["name"]): g["groupId"] for g in get_json(f"{TCG_SORCERY}/groups")["results"]}
        needed = sorted({groups[norm(e["set"])] for e in entries if norm(e["set"]) in groups})
        products, prices = {}, collections.defaultdict(list)
        for group_id in needed:
            for p in get_json(f"{TCG_SORCERY}/{group_id}/products")["results"]:
                products[(group_id, norm(p["name"]))] = p["productId"]
            for p in get_json(f"{TCG_SORCERY}/{group_id}/prices")["results"]:
                prices[p["productId"]].append(p)
        to_gbp = rates["EUR"] / rates["USD"]
        day, matched = tcg_updated[:10], 0
        for e in entries:
            group_id = groups.get(norm(e["set"]))
            product = products.get((group_id, norm(e["name"] + (" (Foil)" if e["foil"] else ""))))
            sub_type = "Foil" if e["foil"] else "Normal"
            pr = next((x for x in prices.get(product, []) if x["subTypeName"] == sub_type), None)
            note = "" if pr else ("not found on TCGplayer" if not product else "no TCGplayer price")
            matches.setdefault(e["key"], {})["tcgplayer"] = {"product": product, "note": note}
            if not pr or not (pr.get("marketPrice") or pr.get("lowPrice")):
                continue
            matched += 1
            rows.append([day, e["key"], "tcgplayer", product,
                         round2(pr["marketPrice"] * to_gbp) if pr.get("marketPrice") else None,
                         round2(pr["lowPrice"] * to_gbp) if pr.get("lowPrice") else None, None])
        log(f"references: TCGplayer Sorcery {day} - {matched} of {len(entries)} priced")
        state["tcgplayer_sorcery"] = tcg_updated

    # Cardmarket: expansion named by its sealed products ("Gothic Booster Box" -> Gothic)
    modified = get(CM_SORCERY_GUIDE, method="HEAD").headers.get("Last-Modified", "")
    if not modified or modified != state.get("cardmarket_sorcery"):
        exp_names = collections.defaultdict(set)
        for p in get_json(CM_SORCERY_SEALED, timeout=120)["products"]:
            exp_names[p["idExpansion"]].add(norm(re.sub(r"\s+(booster box|booster|box)\b.*$", "", p["name"], flags=re.I)))
        by_name = collections.defaultdict(list)
        for p in get_json(CM_SORCERY_SINGLES, timeout=120)["products"]:
            by_name[norm(p["name"])].append(p)
        guide = get_json(CM_SORCERY_GUIDE, timeout=120)
        prices = {g["idProduct"]: g for g in guide["priceGuides"]}
        day = guide.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        to_gbp, matched = rates["EUR"], 0
        for e in entries:
            cands = [p for p in by_name.get(norm(e["name"]), []) if norm(e["set"]) in exp_names.get(p["idExpansion"], ())]
            product = cands[0]["idProduct"] if len(cands) == 1 else None
            note = "" if product else ("not found on Cardmarket" if not cands else f"{len(cands)} versions on Cardmarket")
            g = prices.get(product) if product else None
            f = "-foil" if e["foil"] else ""
            if g and not g.get("trend" + f) and not g.get("low" + f):
                g, note = None, "no Cardmarket price"
            matches.setdefault(e["key"], {})["cardmarket"] = {"product": product, "note": note}
            if not g:
                continue
            matched += 1
            conv = lambda v: round2(v * to_gbp) if v else None
            rows.append([day, e["key"], "cardmarket", product, conv(g.get("trend" + f)), conv(g.get("low" + f)),
                         conv(g.get("avg30" + f))])
        log(f"references: Cardmarket Sorcery {day} - {matched} of {len(entries)} priced")
        state["cardmarket_sorcery"] = modified
    return rows


# ---------------- Frank Frazetta artwork ----------------
def update_frazetta(entries, state, matches, rates, log, tcg_updated, name="frazetta", title="Frazetta"):
    """Prices for the exact products named on each line ("1/2576/456562" and "1/688249").
    Also used for One Piece (name="onepiece"): foil None = whichever TCGplayer price exists."""
    rows = []

    # TCGplayer: category/group/product; foil lines use the Foil price
    if tcg_updated and tcg_updated != state.get(f"tcgplayer_{name}"):
        wanted = {e["key"]: e["tcgplayer"].split("/") for e in entries if e["tcgplayer"].count("/") == 2}
        prices = collections.defaultdict(list)
        for cat, group in sorted({(c, g) for c, g, _ in wanted.values()}):
            for p in get_json(f"https://tcgcsv.com/tcgplayer/{cat}/{group}/prices")["results"]:
                prices[str(p["productId"])].append(p)
        to_gbp = rates["EUR"] / rates["USD"]
        day, matched = tcg_updated[:10], 0
        for e in entries:
            if e["key"] not in wanted:
                matches.setdefault(e["key"], {})["tcgplayer"] = {"product": None, "note": "not on TCGplayer"}
                continue
            product = wanted[e["key"]][2]
            options = prices.get(product, [])
            if e["foil"] is None:      # One Piece: a card is either "Normal" or "Foil" on TCGplayer
                pr = max(options, key=lambda x: bool(x.get("marketPrice") or x.get("lowPrice")), default=None)
            else:
                pr = next((x for x in options if x["subTypeName"] == ("Foil" if e["foil"] else "Normal")), None)
            matches.setdefault(e["key"], {})["tcgplayer"] = {"product": int(product),
                                                             "note": "" if pr else "no TCGplayer price"}
            if not pr or not (pr.get("marketPrice") or pr.get("lowPrice")):
                continue
            matched += 1
            rows.append([day, e["key"], "tcgplayer", product, round2((pr.get("marketPrice") or 0) * to_gbp),
                         round2((pr.get("lowPrice") or 0) * to_gbp), None])
        log(f"references: TCGplayer {title} {day} - {matched} of {len(entries)} priced")
        state[f"tcgplayer_{name}"] = tcg_updated

    # Cardmarket: game/product; one product per card, foil prices in the "-foil" fields
    wanted = {e["key"]: e["cardmarket"].split("/") for e in entries if e["cardmarket"].count("/") == 1}
    games = sorted({g for g, _ in wanted.values()})
    guides = {g: f"{CM_FILES}/priceGuide/price_guide_{g}.json" for g in games}
    stamps = {g: get(url, method="HEAD").headers.get("Last-Modified", "") for g, url in guides.items()}
    if any(not v or v != state.get(f"cardmarket_{name}_{g}") for g, v in stamps.items()):
        prices, day = {}, ""
        for g, url in guides.items():
            guide = get_json(url, timeout=300)
            day = max(day, guide.get("createdAt", "")[:10])
            for p in guide["priceGuides"]:
                prices[(g, str(p["idProduct"]))] = p
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        to_gbp, matched = rates["EUR"], 0
        for e in entries:
            if e["key"] not in wanted:
                matches.setdefault(e["key"], {})["cardmarket"] = {"product": None, "note": "not on Cardmarket"}
                continue
            game, product = wanted[e["key"]]
            g, f = prices.get((game, product)), "-foil" if e["foil"] else ""
            ok = g and (g.get("trend" + f) or g.get("low" + f))
            matches.setdefault(e["key"], {})["cardmarket"] = {"product": int(product),
                                                              "note": "" if ok else "no Cardmarket price"}
            if not ok:
                continue
            matched += 1
            conv = lambda v: round2(v * to_gbp) if v else None
            rows.append([day, e["key"], "cardmarket", product, conv(g.get("trend" + f)), conv(g.get("low" + f)),
                         conv(g.get("avg30" + f))])
        log(f"references: Cardmarket {title} {day} - {matched} of {len(entries)} priced")
        for g, v in stamps.items():
            state[f"cardmarket_{name}_{g}"] = v
    return rows


# ---------------- entry point ----------------
def update(cards, data_dir, overrides_file, log, boxes=(), sorcery=(), frazetta=(), onepiece=()):
    """Save today's Cardmarket and TCGplayer prices, if they changed since the last save."""
    history = data_dir / "reference_history.csv"
    state_file = data_dir / "reference_state.json"
    matches_file = data_dir / "reference_matches.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    matches = json.loads(matches_file.read_text(encoding="utf-8")) if matches_file.exists() else {}
    cards = [c for c in cards if c["code"]]   # a line with only a card name has no single reference

    try:
        rates = euro_rates()   # per 1 euro
        state["rates"] = rates
    except Exception as e:     # rates barely move: use the last ones rather than lose a day
        if "rates" not in state:
            raise
        rates = state["rates"]
        log(f"references: exchange rates not loaded ({e}) - using the last saved ones")
    sets = ygoprodeck_sets([c["name"] for c in cards], log)

    with get("https://tcgcsv.com/last-updated.txt") as r:
        tcg_updated = r.read().decode().strip()

    rows = []
    for source, run in (("Cardmarket", lambda: update_cardmarket(cards, sets, state, matches, rates,
                                                                  load_overrides(overrides_file), log)),
                        ("TCGplayer", lambda: update_tcgplayer(cards, sets, state, matches, rates, log)),
                        ("MTG boxes", lambda: update_mtg_boxes(list(boxes), state, matches, rates, log,
                                                               tcg_updated) if boxes else []),
                        ("Sorcery", lambda: update_sorcery(list(sorcery), state, matches, rates, log,
                                                           tcg_updated) if sorcery else []),
                        ("Frazetta", lambda: update_frazetta(list(frazetta), state, matches, rates, log,
                                                             tcg_updated) if frazetta else []),
                        ("One Piece", lambda: update_frazetta(list(onepiece), state, matches, rates, log,
                                                              tcg_updated, "onepiece", "One Piece")
                                              if onepiece else [])):
        try:
            rows += run()
        except Exception as e:     # one source failing should not stop the other
            log(f"references: {source} failed - {e}")
    if not rows:
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    keys = {c["key"] for c in list(cards) + list(boxes) + list(sorcery) + list(frazetta) + list(onepiece)}
    matches = {k: v for k, v in matches.items() if k in keys}
    new_file = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(COLUMNS)
        writer.writerows(["" if v is None else v for v in row] for row in rows)
    state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")
    matches_file.write_text(json.dumps(matches, indent=1, ensure_ascii=False), encoding="utf-8")
