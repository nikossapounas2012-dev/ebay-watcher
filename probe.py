"""Temporary: how many UK Buy It Now listings does each Sorcery set search return?"""
import json, urllib.parse, urllib.request
import ebay_watch as w

for q in ["sorcery gothic", "sorcery arthurian", "sorcery dragonlord", "sorcery contested realm gothic unique"]:
    for cat in (w.CATEGORY_ID, None):
        params = {"q": q, "filter": "itemLocationCountry:GB,priceCurrency:GBP,buyingOptions:{FIXED_PRICE},price:[3..]",
                  "sort": "price", "limit": 200}
        if cat:
            params["category_ids"] = cat
        req = urllib.request.Request(f"{w.SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
            "Authorization": f"Bearer {w.get_token()}", "X-EBAY-C-MARKETPLACE-ID": w.MARKETPLACE})
        body = json.load(urllib.request.urlopen(req, timeout=30))
        items = body.get("itemSummaries", [])
        print(f"{q!r} category={cat}: total {body.get('total')}  first: {[i['title'][:60] for i in items[:3]]}", flush=True)
