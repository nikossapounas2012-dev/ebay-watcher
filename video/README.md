# Price videos

`make_video.py` turns the saved prices into a ~30 second vertical video (1080x1920) for YouTube Shorts,
TikTok or Reels, and sends it to Telegram with a ready-made title and description. You check it, then upload it.

- **Weekly**: the GitHub workflow "Price video" runs on Sundays (or press "Run workflow").
- **One item**: "Run workflow" with an item number, e.g. `P165`, or locally `python make_video.py --item P165`.
- **Brief for another AI**: `out/ai-video-brief.md` (also sent to Telegram with the chart pictures) - paste it into HeyGen, Synthesia, Veo, Sora, Runway or ChatGPT and attach the charts; it has the voiceover, scenes, exact data and rules (no invented numbers or card art).
- **Canva**: `out/canva-guide.md` (step-by-step template + a Canva AI prompt) and `out/canva-bulk.csv` (Apps -> Bulk create), also sent to Telegram.
- **Music**: put a track here as `video/music.mp3` (e.g. from the YouTube Audio Library, which is free to use
  in YouTube videos) and commit it - it is added automatically and faded out. Without it the video is silent.

Which items: worth £15+, moved 10%+ in 7 days, price held for at least one more check, and both prices within
0.5x-2x of the Cardmarket/TCGplayer price (outside that it is usually a wrong match, not a real move).
Settings are at the top of `make_video.py`.

Notes: prices are eBay UK *asking* prices, not sales - the video and description say so. Card pictures are
TCGplayer's product images (publisher artwork; One Piece ones carry a "SAMPLE" watermark) - set
`USE_IMAGES = False` to leave them out. Uploading is manual because YouTube keeps videos uploaded through an
unaudited API project private.

## Dropbox (optional): files appear in Dropbox, Canva opens them from there

Each run then uploads the video, charts, Canva CSV and guides to `Dropbox / Apps / <your app> / <date>`
and the Telegram message says where. In Canva: Apps -> Dropbox -> pick the files. One-time setup (5 minutes):

1. https://www.dropbox.com/developers/apps -> **Create app** -> Scoped access -> **App folder** ->
   a name (must be unique on Dropbox, e.g. `ebay-watcher-videos-nick`).
2. **Permissions** tab: tick `files.content.write` -> **Submit** (do this before step 4).
3. **Settings** tab: copy the **App key**. (No need to publish: "Development" status is fine for your own account.)
4. On your Mac, in this folder: `python3 video/dropbox_login.py`, paste the app key, allow access in the
   browser, paste the code Dropbox shows back into the terminal.
5. GitHub repo -> Settings -> Secrets and variables -> Actions -> add `DROPBOX_APP_KEY` and
   `DROPBOX_REFRESH_TOKEN` with the printed values.

The app can only see its own folder (Apps / <app name>), nothing else in your Dropbox. Remove its access any
time at https://www.dropbox.com/account/connected_apps. Without these secrets the video run works as before
(Telegram only); a Dropbox error never stops the run.
