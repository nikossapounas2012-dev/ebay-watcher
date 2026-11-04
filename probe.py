"""Temporary: page through broad Sorcery searches and see how titles name cards and sets."""
import json, urllib.parse, urllib.request, collections
import ebay_watch as w

api = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://api.sorcerytcg.com/api/cards", headers={"User-Agent": "ebay-card-watcher/1.0"}), timeout=60))
uniq = {c["name"]: {p["set"]["name"] for p in c["printings"]} for c in api if c["engine"]["rarity"] == "Unique"}
new = {n: s for n, s in uniq.items() if s & {"Gothic", "Arthurian Legends", "Dragonlord"}}
print("uniques in newer sets:", len(new), "also in Alpha/Beta:", sum(1 for s in new.values() if s & {"Alpha", "Beta"}))

items, calls = {}, 0
for q in ["sorcery contested realm", "sorcery gothic", "sorcery arthurian", "sorcery dragonlord", "sorcery tcg"]:
    offset = 0
    while True:
        params = {"q": q, "filter": "itemLocationCountry:GB,priceCurrency:GBP,buyingOptions:{FIXED_PRICE},price:[1..]",
                  "sort": "price", "limit": 200, "offset": offset}
        req = urllib.request.Request(f"{w.SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
            "Authorization": f"Bearer {w.get_token()}", "X-EBAY-C-MARKETPLACE-ID": w.MARKETPLACE})
        body = json.load(urllib.request.urlopen(req, timeout=30)); calls += 1
        page = body.get("itemSummaries", [])
        for i in page:
            items[i["itemId"]] = i["title"]
        print(f"{q!r} offset {offset}: total {body.get('total')} got {len(page)}", flush=True)
        offset += 200
        if offset >= body.get("total", 0) or not page or offset >= 3000:
            break
print("calls", calls, "distinct listings", len(items))
hit = collections.Counter(); withset = collections.Counter()
for t in items.values():
    words = w.words_of(t)
    for n, sets in new.items():
        if w.contains_phrase(words, w.words_of(n)):
            hit[n] += 1
            if {"gothic", "arthurian", "dragonlord"} & set(words):
                withset[n] += 1
print("unique cards seen:", len(hit), "listings naming a unique:", sum(hit.values()), "of which with set word:", sum(withset.values()))
print("examples without set word:", [t for t in items.values() if any(w.contains_phrase(w.words_of(t), w.words_of(n)) for n in new) and not {"gothic", "arthurian", "dragonlord"} & set(w.words_of(t))][:12])
