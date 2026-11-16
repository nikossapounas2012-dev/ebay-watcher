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

## Google Drive (optional): files appear in Drive, Canva opens them from there

Each run then uploads the video, charts, Canva CSV and guides to `My Drive / eBay watcher videos / <date>`
and the Telegram message links to the folder. In Canva: Apps -> Google Drive -> pick the files
(Bulk create: upload the CSV from the Google Drive tab). One-time setup (about 10 minutes):

1. https://console.cloud.google.com -> create a project (e.g. "ebay-watcher").
2. APIs & Services -> Library -> **Google Drive API** -> Enable.
3. APIs & Services -> OAuth consent screen (Google Auth Platform): External, app name "ebay-watcher",
   your email as support and developer contact. Add yourself under Test users / Audience.
   Then **Publish app** (status "In production") - in "Testing" Google expires the sign-in after 7 days.
   No verification is needed: the only scope is drive.file (files the app creates).
4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> **Desktop app**.
   Copy the client ID and client secret.
5. On your Mac: `python3 video/google_drive_login.py`, paste the ID and secret, sign in, allow access
   (Google warns the app is unverified: Advanced -> Go to ebay-watcher).
6. GitHub repo -> Settings -> Secrets and variables -> Actions -> add `GDRIVE_CLIENT_ID`,
   `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` with the printed values.

Without these secrets the video run works as before (Telegram only). A Drive error never stops the run.
