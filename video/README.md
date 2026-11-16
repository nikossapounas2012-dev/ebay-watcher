# Price videos

`make_video.py` turns the saved prices into a ~30 second vertical video (1080x1920) for YouTube Shorts,
TikTok or Reels, and sends it to Telegram with a ready-made title and description. You check it, then upload it.

- **Weekly**: the GitHub workflow "Price video" runs on Sundays (or press "Run workflow").
- **One item**: "Run workflow" with an item number, e.g. `P165`, or locally `python make_video.py --item P165`.
- **Music**: put a track here as `video/music.mp3` (e.g. from the YouTube Audio Library, which is free to use
  in YouTube videos) and commit it - it is added automatically and faded out. Without it the video is silent.

Which items: worth £15+, moved 10%+ in 7 days, price held for at least one more check, and both prices within
0.5x-2x of the Cardmarket/TCGplayer price (outside that it is usually a wrong match, not a real move).
Settings are at the top of `make_video.py`.

Notes: prices are eBay UK *asking* prices, not sales - the video and description say so. Card pictures are
TCGplayer's product images (publisher artwork; One Piece ones carry a "SAMPLE" watermark) - set
`USE_IMAGES = False` to leave them out. Uploading is manual because YouTube keeps videos uploaded through an
unaudited API project private.
