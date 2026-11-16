"""
Short vertical price videos (YouTube Shorts / TikTok / Reels) from the prices the watcher already saved.

    python make_video.py                  # weekly "biggest movers": the items whose eBay price moved most
    python make_video.py --item P165      # one item (any Telegram number: 12, B4, S20, F3, P165, G1, PB8)
    python make_video.py --no-send        # make the video only, don't send it to Telegram

Reads ebay_history.csv, ebay_prices.json and reference_history.csv from DATA_DIR (no eBay searches).
Writes out/price-video.mp4 plus out/upload.txt (title, description, hashtags) and sends both to Telegram
for a check before you upload it yourself.

Music: put an .mp3 at video/music.mp3 (e.g. from the YouTube Audio Library) and it is added, faded out.
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid

import imageio_ffmpeg
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates           # noqa: E402
import matplotlib.pyplot as plt   # noqa: E402
import matplotlib.ticker          # noqa: E402
import numpy as np                # noqa: E402

import ebay_watch as w            # noqa: E402  (list files, DATA_DIR and Telegram settings)

# ---------------- settings ----------------
DAYS = 7                    # "this week"
MIN_HISTORY_DAYS = 3        # an item first seen more recently than this has no real trend yet
MIN_PRICE = 15.0            # £: cheaper items are left out
MIN_CHANGE = 0.10           # 10%+ up or down
MAX_ITEMS = 5
MIN_HELD_HOURS = 2.5        # the current price must have lasted past at least one more check
MARKET_BAND = (0.5, 2.0)    # both prices must be within 0.5x-2x of the market price (when there is one):
                            # outside that it is usually a wrong match or a single odd listing, not a move
CHART_DAYS = 14             # chart window
USE_IMAGES = True           # TCGplayer product pictures (publisher artwork - see README notes)
SITE = "nikossapounas2012-dev.github.io/ebay-watcher"
# Google Drive: each run's files go to "<GDRIVE_FOLDER>/<date>" when GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET and
# GDRIVE_REFRESH_TOKEN are set (see video/README.md). Canva opens them through its Google Drive app.
GDRIVE_FOLDER = "eBay watcher videos"
MUSIC = w.BASE_DIR / "video" / "music.mp3"
OUT = w.BASE_DIR / "out"

W, H, FPS = 1080, 1920, 30
BG, PANEL, TEXT, MUTED = "#111317", "#1a1d22", "#e8eaed", "#a0a6b0"
EBAY, CM, TCG, LOW, NOW, UP = "#6b9ff5", "#b195f5", "#4cc3bf", "#4cc584", "#f29a45", "#f07070"
GAME = {"card": "Yu-Gi-Oh!", "box": "MTG booster box", "sorcery": "Sorcery: Contested Realm",
        "opcard": "One Piece", "opgraded": "One Piece · PSA 10", "opbox": "One Piece booster box"}


# ---------------- data ----------------
def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", ""))


def load_data():
    entries = w.load_all()
    by_key = {e["key"]: e for e in entries}
    history = {}
    with w.HISTORY_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            history.setdefault(row["card"], []).append(
                (parse_time(row["time"]), float(row["price"]) if row["price"] else None))
    for series in history.values():
        series.sort(key=lambda p: p[0])
    prices = json.loads(w.STATE_FILE.read_text(encoding="utf-8"))
    refs = {}
    ref_file = w.DATA_DIR / "reference_history.csv"
    if ref_file.exists():
        with ref_file.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["price"]:
                    refs.setdefault(row["card"], {}).setdefault(row["source"], []).append(
                        (parse_time(row["date"] + "T12:00:00"), float(row["price"])))
    matches_file = w.DATA_DIR / "reference_matches.json"
    matches = json.loads(matches_file.read_text(encoding="utf-8")) if matches_file.exists() else {}
    return entries, by_key, history, prices, refs, matches


def price_at(series, t):
    """The eBay price in force at time t (None = not listed or not seen yet)."""
    value = None
    for when, price in series:
        if when > t:
            break
        value = price
    return value


def market(key, kind, refs):
    """(price, 'Cardmarket'/'TCGplayer') - same rule as the alerts (no TCGplayer for Yu-Gi-Oh!)."""
    for source, label in (("cardmarket", "Cardmarket"), ("tcgplayer", "TCGplayer")):
        if source == "tcgplayer" and kind == "card":
            continue
        series = refs.get(key, {}).get(source)
        if series:
            return series[-1][1], label
    return None


def story(entry, history, prices, refs, now, days):
    """The numbers one item's video needs, or None if its price didn't move in a usable way."""
    key = entry["key"]
    series, offer = history.get(key, []), prices.get(key)
    if not series or not offer:
        return None
    start = now - timedelta(days=days)
    before = price_at(series, start)
    since = start
    if before is None:          # first seen during the week: use its first price if it's old enough
        first = next(((t, p) for t, p in series if p is not None), None)
        if not first or first[0] > now - timedelta(days=MIN_HISTORY_DAYS):
            return None
        since, before = first
    priced = [p for _, p in series if p is not None]
    return {"entry": entry, "key": key, "now": offer["price"], "before": before, "since": since,
            "change": offer["price"] / before - 1, "lowest": min(priced), "url": offer.get("url", ""),
            "last_change": series[-1][0], "market": market(key, entry["kind"], refs)}


def plausible(s):
    if not s["market"]:
        return True
    lo, hi = (f * s["market"][0] for f in MARKET_BAND)
    return lo <= s["before"] <= hi and lo <= s["now"] <= hi


def movers(entries, history, prices, refs, now):
    out = []
    for e in entries:
        s = story(e, history, prices, refs, now, DAYS)
        if (s and s["now"] >= MIN_PRICE and abs(s["change"]) >= MIN_CHANGE
                and s["last_change"] <= now - timedelta(hours=MIN_HELD_HOURS) and plausible(s)):
            out.append(s)
    out.sort(key=lambda s: -abs(s["change"]))
    return out[:MAX_ITEMS]


def image_for(entry, matches):
    """TCGplayer product picture (the ungraded card's for PSA 10 lines), as an RGB array, or None."""
    if not USE_IMAGES:
        return None
    key = entry["key"].removesuffix(f" | {w.OP_GRADED}")
    product = ((matches.get(key) or {}).get("tcgplayer") or {}).get("product")
    if not product:
        return None
    try:
        req = urllib.request.Request(f"https://tcgplayer-cdn.tcgplayer.com/product/{product}_400w.jpg",
                                     headers={"User-Agent": "ebay-watcher-video"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return plt.imread(io.BytesIO(r.read()), format="jpg")
    except Exception as e:
        w.log(f"video: no picture for {entry['label']} ({e})")
        return None


# ---------------- drawing ----------------
def new_figure():
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=BG)
    return fig


def frame_bytes(fig):
    fig.canvas.draw()
    return np.asarray(fig.canvas.buffer_rgba())[:, :, :3].tobytes()


def text(fig, x, y, s, size, color=TEXT, weight="normal", ha="center", **kw):
    return fig.text(x, y, s, fontsize=size, color=color, weight=weight, ha=ha, va="center", **kw)


def gbp(v):
    return f"£{v:,.2f}"


def steps(series, t0, t1):
    """Step-line points (x as datetimes) from t0 to t1; None prices become gaps (NaN)."""
    xs, ys = [], []
    start = price_at(series, t0)
    if start is not None:
        xs.append(t0); ys.append(start)
    for t, p in series:
        if t <= t0 or t > t1:
            continue
        if ys and not np.isnan(ys[-1]):
            xs.append(t); ys.append(ys[-1])        # hold the old price until the change
        xs.append(t); ys.append(np.nan if p is None else p)
    if ys and not np.isnan(ys[-1]):
        xs.append(t1); ys.append(ys[-1])
    return xs, ys


def clip(xs, ys, cut):
    """The part of a step line up to time cut (for the drawing animation)."""
    out_x, out_y = [], []
    for x, y in zip(xs, ys):
        if x > cut:
            if out_y:
                out_x.append(cut); out_y.append(out_y[-1])
            break
        out_x.append(x); out_y.append(y)
    return out_x, out_y


def draw_chart(ax, s, history, refs, now, progress):
    t0, t1 = now - timedelta(days=CHART_DAYS), now
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_visible(False)
    xs, ys = steps(history.get(s["key"], []), t0, t1)
    values = [y for y in ys if not np.isnan(y)]
    for source, color in (("cardmarket", CM), ("tcgplayer", TCG)):
        if source == "tcgplayer" and s["entry"]["kind"] == "card":
            continue
        series = [(t, p) for t, p in refs.get(s["key"], {}).get(source, []) if t >= t0 - timedelta(days=1)]
        if series:
            rx, ry = steps(series, t0, t1)
            ax.plot(rx, ry, color=color, lw=3, alpha=0.75, ls=(0, (4, 3)))
            values += [y for y in ry if not np.isnan(y)]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or hi * 0.1
    ax.set_ylim(max(lo - pad, 0), hi + pad)
    ax.set_xlim(t0, t1)
    cut = t0 + (t1 - t0) * progress
    cx, cy = clip(xs, ys, cut)
    ax.plot(cx, cy, color=EBAY, lw=6, solid_joinstyle="round")
    if progress >= 1:
        ax.scatter([t1], [s["now"]], s=260, color=NOW, zorder=5, edgecolors=PANEL, linewidths=3)
    ax.tick_params(colors=MUTED, labelsize=22, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"£{v:,.0f}"))
    ax.xaxis.set_major_locator(matplotlib.dates.DayLocator(interval=max(CHART_DAYS // 4, 1)))
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%-d %b"))
    ax.grid(axis="y", color="#262a30", lw=1.5)
    ax.set_axisbelow(True)


def legend(fig, y, s, refs):
    have = refs.get(s["key"], {})
    items = [("eBay UK cheapest", EBAY)] + [(label, color) for source, label, color in
             (("cardmarket", "Cardmarket", CM), ("tcgplayer", "TCGplayer", TCG))
             if have.get(source) and not (source == "tcgplayer" and s["entry"]["kind"] == "card")]
    width = 0.8 / len(items)
    for i, (label, color) in enumerate(items):
        x = 0.1 + width * (i + 0.5)
        fig.text(x, y, "━ " + label, fontsize=22, color=color, ha="center", va="center")


def item_scene(s, rank, total, history, refs, matches, now, single, still=None):
    """Frames for one item: the chart draws itself, then the numbers appear and hold."""
    e = s["entry"]
    picture = image_for(e, matches)
    frames = []
    reveal, hold = int(FPS * 2.4), int(FPS * 2.8)
    for i in range(reveal + 1):
        progress = i / reveal
        fig = new_figure()
        top = f"#{rank} of {total}" if not single else ""
        text(fig, 0.5, 0.96, f"{top}{'  ·  ' if top else ''}{GAME.get(e['kind'], e.get('game', ''))}", 30, MUTED)
        lines = textwrap.wrap(e["name"], 24)
        if len(lines) == 1:
            text(fig, 0.5, 0.905, lines[0], 56, weight="bold")
        else:                               # long names: two smaller lines, meta moves down
            text(fig, 0.5, 0.897, "\n".join(textwrap.wrap(e["name"], 30)[:2]), 44, weight="bold", linespacing=1.15)
        meta = e["label"][len(e["name"]):].strip(" ·()") if e["label"].startswith(e["name"]) else e["label"]
        text(fig, 0.5, 0.85 if len(lines) == 1 else 0.838, meta[:44], 30, MUTED)
        if picture is not None:
            ax_img = fig.add_axes([0.25, 0.555, 0.5, 0.27])
            ax_img.imshow(picture)
            ax_img.axis("off")
        ax = fig.add_axes([0.13, 0.25, 0.80, 0.27] if picture is not None else [0.13, 0.30, 0.80, 0.45])
        draw_chart(ax, s, history, refs, now, progress)
        legend(fig, 0.215, s, refs)
        if progress >= 1:
            colour = LOW if s["change"] < 0 else UP
            arrow = "▼" if s["change"] < 0 else "▲"
            text(fig, 0.5, 0.155, f"{gbp(s['before'])}  →  {gbp(s['now'])}", 58, weight="bold")
            since = "this week" if s["since"] <= now - timedelta(days=DAYS) + timedelta(hours=1) \
                else f"since {s['since']:%-d %b}"
            text(fig, 0.5, 0.105, f"{arrow} {abs(s['change']):.0%} {since}", 46, colour, weight="bold")
            notes = []
            if s["market"]:
                notes.append(f"{s['market'][1]} {gbp(s['market'][0])}")
            if s["now"] <= s["lowest"]:
                notes.append("lowest we've seen")
            text(fig, 0.5, 0.06, "   ·   ".join(notes), 30, MUTED)
        frame = frame_bytes(fig)
        plt.close(fig)
        frames.append(frame)
    frames += [frames[-1]] * hold
    if still:
        still.parent.mkdir(parents=True, exist_ok=True)
        plt.imsave(still, np.frombuffer(frames[-1], dtype=np.uint8).reshape(H, W, 3))
    return frames


def card_scene(lines, seconds):
    """A title or end card: [(text, size, colour, weight), ...] spread down the screen."""
    fig = new_figure()
    n = len(lines)
    for i, (s, size, color, weight) in enumerate(lines):
        text(fig, 0.5, 0.62 - i * (0.24 / max(n - 1, 1)), s, size, color, weight, linespacing=1.2)
    frame = frame_bytes(fig)
    plt.close(fig)
    return [frame] * int(FPS * seconds)


def write_video(frames, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    silent = path.with_suffix(".silent.mp4")
    writer = imageio_ffmpeg.write_frames(str(silent), (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p",
                                         macro_block_size=8, output_params=["-crf", "23", "-preset", "medium"])
    writer.send(None)
    for frame in frames:
        writer.send(frame)
    writer.close()
    seconds = len(frames) / FPS
    if MUSIC.exists():
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(silent), "-stream_loop", "-1", "-i", str(MUSIC),
                        "-t", f"{seconds:.2f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                        "-af", f"afade=t=in:d=0.5,afade=t=out:st={max(seconds - 1.5, 0):.2f}:d=1.5",
                        str(path)], check=True)
        silent.unlink()
    else:
        silent.replace(path)
    return seconds


# ---------------- text for the upload ----------------
def upload_text(stories, now, single):
    if single:
        s = stories[0]
        e = s["entry"]
        title = f"{e['name']} UK price {'drop' if s['change'] < 0 else 'jump'}: {gbp(s['now'])} #shorts"
    else:
        title = f"Biggest UK card price moves this week ({now:%-d %b}) #shorts"
    lines = [title[:100], "", "Cheapest UK eBay Buy It Now listing (postage included), checked every 2 hours:", ""]
    for i, s in enumerate(stories, 1):
        m = f" · {s['market'][1]} {gbp(s['market'][0])}" if s["market"] else ""
        lines.append(f"{'' if single else f'{i}. '}{s['entry']['label']}: {gbp(s['before'])} → {gbp(s['now'])} "
                     f"({s['change']:+.0%}){m}")
    games = {GAME.get(s["entry"]["kind"], "") for s in stories}
    tags = ["#tcg", "#cardprices"] + [t for g, t in (("Yu-Gi-Oh!", "#yugioh"), ("MTG", "#mtg"),
                                                      ("Sorcery", "#sorcerytcg"), ("One Piece", "#onepiecetcg"))
                                     if any(g in x for x in games)]
    lines += ["", f"All charts: https://{SITE}/",
              "Prices are asking prices on eBay UK, not sold prices. Not financial advice.", "", " ".join(tags)]
    return title[:100], "\n".join(lines)


# ---------------- brief for another AI (HeyGen, Synthesia, Veo, Sora, Runway, ChatGPT...) ----------------
def say_money(v):
    """£50.20 -> 'fifty pounds twenty' is overkill; voices read '£50.20' well, just drop .00."""
    return f"£{v:,.0f}" if v == int(v) else f"£{v:,.2f}"


def short_meta(e):
    return e["label"][len(e["name"]):].strip(" ·()") if e["label"].startswith(e["name"]) else ""


def ai_brief(stories, now, single, title, description):
    n = len(stories)
    lines = [
        f"# Video brief: \"{title}\"",
        "",
        "Make a vertical short video (YouTube Shorts / TikTok / Reels) from the data below.",
        "",
        "## Format",
        "- 9:16 vertical, 1080x1920, 25-35 seconds, on-screen captions for every spoken line.",
        "- Calm, clear, collector-to-collector tone. British English, prices in pounds.",
        "- Dark background (#111317), white text, accent orange (#f29a45), drops green (#4cc584), "
        "rises red (#f07070).",
        "",
        "## Hard rules (do not break these)",
        "1. Use every number, name, card code and date EXACTLY as written in the Data section. "
        "Do not round, convert, estimate or invent anything.",
        "2. Prices are the cheapest UK eBay Buy It Now ASKING price, postage included - not sold prices. "
        "Never say \"sold for\", \"worth\" or \"value\". Say \"cheapest UK listing\" or \"listed at\".",
        "3. Do NOT generate, redraw or imitate card artwork, logos, set symbols or packaging. Card visuals "
        "and charts come only from the attached images (chart_1.png ...). They contain the real data - "
        "show them full-screen or as large as possible, do not alter the numbers on them.",
        "4. AI-generated footage is allowed only for abstract filler: e.g. a blurred table with card sleeves, "
        "hands shuffling blank-backed cards, a soft background. Nothing that looks like a real product.",
        "5. No buying or investment advice. The video ends with: \"Asking prices, not sales. "
        "Not financial advice.\"",
        "",
        "## Voiceover script",
    ]
    t = 0.0
    hook = (f"{stories[0]['entry']['name']}: a big UK price move this week." if single
            else "Here are this week's biggest UK card price moves.")
    lines.append(f"[{t:.0f}-{t + 2.5:.0f}s] \"{hook}\"")
    t += 2.5
    for i, s in enumerate(stories, 1):
        e = s["entry"]
        meta = short_meta(e)
        direction = "down" if s["change"] < 0 else "up"
        since = "this week" if s["since"] <= now - timedelta(days=DAYS) + timedelta(hours=1) \
            else f"since {s['since']:%-d %B}"
        word = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][i - 1]
        spoken = (f"{'' if single else f'Number {word}: '}{e['name']}"
                  f"{', ' + meta if meta else ''}. Cheapest UK listing went from {say_money(s['before'])} "
                  f"to {say_money(s['now'])}, {direction} {abs(s['change']):.0%} {since}.")
        if s["market"]:
            spoken += f" {s['market'][1]} has it at {say_money(s['market'][0])}."
        lines.append(f"[{t:.0f}-{t + 5.5:.0f}s] \"{spoken}\"")
        t += 5.5
    lines.append(f"[{t:.0f}-{t + 3:.0f}s] \"Charts for every card at the link in the description. "
                 f"Asking prices, not sales. Not financial advice.\"")
    t += 3
    lines += ["", "## Scenes", "| Time | Visual | On-screen text |", "|---|---|---|",
              f"| 0-2.5s | Title card (abstract filler footage allowed behind it) | "
              f"{'UK card prices - ' + now.strftime('%-d %b') if not single else stories[0]['entry']['name']} |"]
    start = 2.5
    for i, s in enumerate(stories, 1):
        arrow = "▼" if s["change"] < 0 else "▲"
        lines.append(f"| {start:.1f}-{start + 5.5:.1f}s | chart_{i}.png full-screen (slow zoom is fine) | "
                     f"{'' if single else f'#{i} '}{s['entry']['name']} · {gbp(s['before'])} → {gbp(s['now'])} "
                     f"{arrow}{abs(s['change']):.0%} |")
        start += 5.5
    lines += [f"| {start:.1f}-{start + 3:.1f}s | End card | {SITE} · Asking prices, not sales. "
              "Not financial advice. |", "",
              "## Data (the only source of truth)",
              "| # | Game | Card / item | Version | Price a week ago | Cheapest now | Change | Market price | "
              "Lowest seen |", "|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(stories, 1):
        e = s["entry"]
        m = f"{s['market'][1]} {gbp(s['market'][0])}" if s["market"] else "none"
        lines.append(f"| {i} | {GAME.get(e['kind'], e.get('game', ''))} | {e['name']} | {short_meta(e) or '-'} | "
                     f"{gbp(s['before'])} | {gbp(s['now'])} | {s['change']:+.0%} | {m} | {gbp(s['lowest'])} |")
    lines += ["",
              f"Data date: {now:%-d %B %Y}. eBay prices: cheapest UK Buy It Now listing incl. postage, checked every "
              "2 hours. Market price: Cardmarket trend, else TCGplayer market price, converted to pounds.",
              "",
              "## Attached files",
              *[f"- chart_{i}.png - {s['entry']['label']}: card picture and its 14-day price chart (eBay line, "
                f"Cardmarket/TCGplayer dashed)" for i, s in enumerate(stories, 1)],
              "- price-video.mp4 - the plain version of this video, as a reference for timing and content",
              "",
              "## Title, description and hashtags for the upload", "", description]
    return "\n".join(lines) + "\n"


# ---------------- Canva (Bulk Create data + instructions) ----------------
def canva_rows(stories, now, single):
    """One row per item page, for Canva's Bulk Create (Apps -> Bulk create -> upload CSV)."""
    rows = []
    for i, s in enumerate(stories, 1):
        e = s["entry"]
        since = "this week" if s["since"] <= now - timedelta(days=DAYS) + timedelta(hours=1) \
            else f"since {s['since']:%-d %b}"
        word = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][i - 1]
        voice = (f"{'' if single else f'Number {word}: '}{e['name']}{', ' + short_meta(e) if short_meta(e) else ''}. "
                 f"Cheapest UK listing went from {say_money(s['before'])} to {say_money(s['now'])}, "
                 f"{'down' if s['change'] < 0 else 'up'} {abs(s['change']):.0%} {since}."
                 + (f" {s['market'][1]} has it at {say_money(s['market'][0])}." if s["market"] else ""))
        rows.append({
            "rank": "" if single else f"#{i}",
            "game": GAME.get(e["kind"], e.get("game", "")),
            "card": e["name"],
            "version": short_meta(e),
            "was": gbp(s["before"]),
            "now": gbp(s["now"]),
            "change": f"{'▼' if s['change'] < 0 else '▲'} {abs(s['change']):.0%} {since}",
            "direction": "DOWN" if s["change"] < 0 else "UP",
            "market": f"{s['market'][1]} {gbp(s['market'][0])}" if s["market"] else "",
            "lowest": gbp(s["lowest"]),
            "chart_image": f"chart_{i}.png",
            "voiceover": voice,
        })
    return rows


def canva_guide(stories, now, single, title, description, rows):
    n = len(rows)
    first = f"UK card prices · {now:%-d %b}" if not single else rows[0]["card"]
    lines = [
        f"# Canva: \"{title}\"",
        "",
        "Files: canva-bulk.csv (the data), chart_1.png ... (one picture per item: card + real price chart).",
        "",
        "## Option A - Bulk Create (exact numbers, recommended)",
        "1. Canva -> Create a design -> **Mobile Video** (1080 x 1920). Add 3 pages.",
        "2. **Page 1 (intro, 2.5 s)**: dark background #111317, big white text "
        f"\"{first}\", smaller orange (#f29a45) text \"{'Top ' + str(n) + ' price moves on eBay UK' if not single else 'big UK price move'}\".",
        "3. **Page 2 (the item template, 5.5 s)** - add these text boxes and an image frame:",
        "   - small grey text `{rank}  ·  {game}` at the top",
        "   - big bold white text `{card}`, grey text `{version}` under it",
        "   - an image frame filling the middle (for the chart picture)",
        "   - big bold white `{was}  →  {now}`",
        "   - bold `{change}` - colour it green #4cc584 (drops) or red #f07070 (rises); see the direction column",
        "   - small grey `{market}`",
        "4. Apps -> **Bulk create** -> Upload data -> canva-bulk.csv. Connect each text box to the column with the "
        "same name (right-click the box -> Connect data). Connect the image frame to `chart_image` and upload the "
        "chart_N.png files when Canva asks for them (or drop each picture into its page afterwards).",
        f"5. Continue -> Generate: Canva makes {n} item page{'s' * (n != 1)}. Copy them into the video between "
        "page 1 and the end page.",
        "6. **Last page (3 s)**: \"Charts for every card\" + nikossapounas2012-dev.github.io/ebay-watcher + small "
        "\"Asking prices, not sales. Not financial advice.\"",
        "7. Animate: page transitions *Dissolve* or *Slide*; on each item page animate the price text with *Rise* "
        "and give the chart picture a slow *Zoom in* (Pan). Recolour any `{change}` text whose direction is UP to red.",
        "8. Voice: use the `voiceover` column - record it yourself (Record yourself / Audio), or paste it into a "
        "text-to-speech app from Canva's Apps panel. Add music from Canva's Audio tab (keep it quiet under the voice).",
        "9. Captions: turn on auto-captions if your Canva has them, or add the voiceover lines as text.",
        "10. Download -> MP4, 1080p. Upload to YouTube Shorts with the title and description below.",
        "",
        "## Option B - Canva AI (faster, check every number afterwards)",
        "Upload the chart_N.png pictures first, then paste this into Canva AI / Magic Design:",
        "",
        "> " + (
            f"Create a {8 + 5 * n}-second vertical 9:16 video for YouTube Shorts called \"{title.replace(' #shorts', '')}\". "
            "Dark background #111317, white text, orange accent #f29a45, green for price drops, red for rises. "
            f"Page 1: title \"{first}\". "
            + " ".join(f"Page {i + 1}: use my uploaded image {r['chart_image']} large in the middle, text "
                       f"\"{(r['rank'] + ' ') if r['rank'] else ''}{r['card']} {r['version']}\" at the top and "
                       f"\"{r['was']} → {r['now']} ({r['change']})\" underneath."
                       for i, r in enumerate(rows, 1))
            + f" Last page: \"Charts for every card\", \"{SITE}\", \"Asking prices, not sales. "
            "Not financial advice.\" Use these exact prices and names; do not create card artwork or change any numbers."),
        "",
        "Canva AI can rewrite numbers: compare every page with the table below before downloading.",
        "",
        "## Voiceover (all pages)",
        f"- Intro: \"{'Here are this week' + chr(39) + 's biggest UK card price moves.' if not single else rows[0]['card'] + ': a big UK price move this week.'}\"",
        *[f"- {r['rank'] or 'Item'} {r['card']}: \"{r['voiceover']}\"" for r in rows],
        "- End: \"Charts for every card at the link in the description. Asking prices, not sales. "
        "Not financial advice.\"",
        "",
        "## Data (check against this)",
        "| | Card | Version | Was | Now | Change | Market |",
        "|---|---|---|---|---|---|---|",
        *[f"| {r['rank']} | {r['card']} | {r['version']} | {r['was']} | {r['now']} | {r['change']} | {r['market'] or '-'} |"
          for r in rows],
        "",
        "## Upload text", "", description,
    ]
    return "\n".join(lines) + "\n"


def write_canva(stories, now, single, title, description):
    rows = canva_rows(stories, now, single)
    data = OUT / "canva-bulk.csv"
    with data.open("w", newline="", encoding="utf-8-sig") as f:     # BOM so Canva/Excel read £ and arrows
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    guide = OUT / "canva-guide.md"
    guide.write_text(canva_guide(stories, now, single, title, description, rows), encoding="utf-8")
    return data, guide


# ---------------- Google Drive ----------------
DRIVE = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"


def drive_token():
    """A fresh access token from the saved refresh token, or None when Drive isn't set up."""
    ids = [os.getenv(k, "") for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN")]
    if not all(ids):
        return None
    data = urllib.parse.urlencode({"client_id": ids[0], "client_secret": ids[1], "refresh_token": ids[2],
                                   "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=30) as r:
        return json.load(r)["access_token"]


def drive_call(token, url, method="GET", body=None, content_type="application/json"):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def drive_find(token, name, parent=None, folder=False):
    q = [f"name = '{name.replace(chr(39), chr(92) + chr(39))}'", "trashed = false"]
    if parent:
        q.append(f"'{parent}' in parents")
    if folder:
        q.append(f"mimeType = '{FOLDER}'")
    found = drive_call(token, f"{DRIVE}?" + urllib.parse.urlencode({"q": " and ".join(q),
                                                                     "fields": "files(id,name)"}))["files"]
    return found[0]["id"] if found else None


def drive_folder(token, name, parent=None):
    return drive_find(token, name, parent, folder=True) or drive_call(
        token, DRIVE, "POST", json.dumps({"name": name, "mimeType": FOLDER, **({"parents": [parent]} if parent else {})}
                                         ).encode())["id"]


def drive_put(token, path, parent):
    """Upload a file into the folder; a re-run replaces the file of the same name instead of adding a copy."""
    boundary = uuid.uuid4().hex
    existing = drive_find(token, path.name, parent)
    meta = {} if existing else {"name": path.name, "parents": [parent]}
    mime = {".mp4": "video/mp4", ".png": "image/png", ".csv": "text/csv", ".md": "text/markdown",
            ".txt": "text/plain"}.get(path.suffix, "application/octet-stream")
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--".encode()
    url = f"{DRIVE_UPLOAD}/{existing}?uploadType=multipart" if existing else f"{DRIVE_UPLOAD}?uploadType=multipart"
    drive_call(token, url, "PATCH" if existing else "POST", body, f"multipart/related; boundary={boundary}")


def upload_to_drive(paths, name):
    """Upload the run's files to "<GDRIVE_FOLDER>/<name>"; returns the folder link, or None."""
    try:
        token = drive_token()
        if not token:
            return None
        folder = drive_folder(token, name, drive_folder(token, GDRIVE_FOLDER))
        for path in paths:
            drive_put(token, path, folder)
        w.log(f"video: {len(paths)} files uploaded to Google Drive / {GDRIVE_FOLDER} / {name}")
        return f"https://drive.google.com/drive/folders/{folder}"
    except urllib.error.HTTPError as e:
        w.log(f"video: Google Drive upload failed: {e.code} {e.read().decode(errors='ignore')[:300]}")
    except Exception as e:
        w.log(f"video: Google Drive upload failed: {e}")
    return None


# ---------------- Telegram ----------------
def send_file(path, caption="", method="sendDocument", field="document", mime="application/octet-stream"):
    """Upload one file to the Telegram chat (sendVideo / sendDocument)."""
    token, chat = w.telegram_config()
    if not (token and chat):
        w.log(f"video: Telegram not configured - {path.name} not sent")
        return
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    fields = [("chat_id", chat), ("caption", caption[:1024])]
    if method == "sendVideo":
        fields.append(("supports_streaming", "true"))
    for name, value in fields:
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{path.name}\"\r\n"
               f"Content-Type: {mime}\r\n\r\n".encode())
    body.write(path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        json.load(r)
    w.log(f"video: {path.name} sent to Telegram")


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--item", help="one Telegram number (e.g. P165, S20, B4) instead of the weekly movers")
    ap.add_argument("--no-send", action="store_true", help="don't send to Telegram")
    args = ap.parse_args()

    entries, by_key, history, prices, refs, matches = load_data()
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)   # saved times are UTC
    if args.item:
        entry = next((e for e in entries if e["num"].upper() == args.item.upper()), None)
        s = entry and story(entry, history, prices, refs, now, DAYS)
        if not s:
            raise SystemExit(f"{args.item}: not found, or not listed now / not enough history for a chart")
        stories = [s]
    else:
        stories = movers(entries, history, prices, refs, now)
    if not stories:
        msg = (f"🎬 No video this week: nothing worth £{MIN_PRICE:g}+ moved {MIN_CHANGE:.0%} or more "
               f"in the last {DAYS} days.")
        w.log(msg)
        if not args.no_send:
            w.send_telegram(msg)
        return

    single = bool(args.item)
    frames = []
    if not single:
        frames += card_scene([("UK card prices", 76, TEXT, "bold"), (f"week to {now:%-d %b}", 44, MUTED, "normal"),
                              (f"Top {len(stories)} price moves", 54, NOW, "bold"),
                              ("on eBay UK", 40, MUTED, "normal")], 2.2)
    for i, s in enumerate(stories, 1):
        w.log(f"video: {s['entry']['num']} {s['entry']['label']} {s['before']} -> {s['now']} ({s['change']:+.0%})")
        frames += item_scene(s, i, len(stories), history, refs, matches, now, single,
                             still=OUT / f"chart_{i}.png")
    frames += card_scene([("Charts for every card", 50, TEXT, "bold"), (SITE, 30, EBAY, "normal"),
                          ("Cheapest UK eBay Buy It Now,\npostage included.", 32, MUTED, "normal"),
                          ("Asking prices, not sales. Not financial advice.", 28, MUTED, "normal")], 2.5)

    path = OUT / "price-video.mp4"
    seconds = write_video(frames, path)
    title, description = upload_text(stories, now, single)
    (OUT / "upload.txt").write_text(description + "\n", encoding="utf-8")
    brief = OUT / "ai-video-brief.md"
    brief.write_text(ai_brief(stories, now, single, title, description), encoding="utf-8")
    canva_data, canva_md = write_canva(stories, now, single, title, description)
    charts = [OUT / f"chart_{i}.png" for i in range(1, len(stories) + 1)]
    drive = upload_to_drive([path, OUT / "upload.txt", brief, canva_md, canva_data, *charts],
                            f"{now:%Y-%m-%d}" + (f" {args.item.upper()}" if single else ""))
    w.log(f"video: {path} ({seconds:.0f}s, {path.stat().st_size / 1e6:.1f} MB)")
    if not args.no_send:
        send_file(path, f"🎬 Check, then upload to YouTube Shorts:\n\n{title}"
                  + (f"\n\n📁 All files (Canva, charts, brief) in Google Drive:\n{drive}" if drive else ""),
                  "sendVideo", "video", "video/mp4")
        w.send_telegram("Description to paste:\n\n" + description)
        send_file(brief, "🤖 Brief for another AI video tool (HeyGen, Synthesia, Veo, Sora...): paste it in "
                         "and attach the chart pictures below.", mime="text/markdown")
        send_file(canva_md, "🎨 Canva: step-by-step guide + Canva AI prompt", mime="text/markdown")
        send_file(canva_data, "🎨 Canva Bulk Create data (Apps -> Bulk create -> upload this CSV)", mime="text/csv")
        for i in range(1, len(stories) + 1):
            send_file(OUT / f"chart_{i}.png", f"chart_{i}.png", mime="image/png")


if __name__ == "__main__":
    main()
