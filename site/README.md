# VODLOCK website

This folder is a standalone static website. Viewers do not need the browser extension or Round Studio. It embeds the original YouTube or Twitch broadcast and loads small, precomputed round indexes from `indexes/`.

## Local preview

From the repository root:

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:4173`.

## Deploy to Vercel

Create a Vercel project whose root directory is `site`. There is no build command and the output directory is `.`. `vercel.json` supplies the security and caching headers.

The viewer is static and needs no database, API key, server function, or hosted video files. Automatic source discovery and processing run outside the website. Twitch discovery uses Twitch application credentials.

## Publish another processed VOD

1. Build and review the index in Round Studio.
2. Export its JSON file.
3. Save it as `indexes/PROVIDER-VIDEO_ID.json`.
4. Add one matching entry to `catalog.json` with `provider`, `sourceId`, and the index path.
5. Redeploy the site.

Manually created indexes should be reviewed before publishing. Catalog cards intentionally omit scores, map totals, round totals, durations, and thumbnails.

## Automatic channel monitoring

`indexer/auto_publish.py` monitors the official VALORANT YouTube upload feed plus the `gofns`, `ohnepixel`, and `valorant` Twitch channels in `indexer/auto_channels.json`. Twitch discovery requests archived broadcasts and rejects any VOD whose stream ID is still reported live, so processing starts only after that broadcast has ended. Ohnepixel VODs must also have a VALORANT event marker in their title.

Official broadcasts are analyzed from Twitch. The analysis records a small visual fingerprint every two seconds. When a `FULL MATCH` upload appears on the official YouTube channel, VODLOCK downloads its timeline storyboard, verifies matching visual anchors across the beginning, middle, and end, detects breaks removed by the YouTube edit, and translates the round index to YouTube timestamps. It never publishes an uncertain alignment. Once verified, the permanent YouTube match replaces the temporary official Twitch card. Creator watch parties remain on Twitch with archived chat.

Automatic publishing is deliberately strict. An index is published only when it begins at map 1 round 1, contains at least 13 rounds, has no sequence gaps or detector warnings, and every detection meets the configured confidence threshold. Rejected indexes are recorded as held in `indexer/auto_state.json`.

The GitHub Actions workflow in `.github/workflows/auto-publish.yml` checks the channels twice per hour, processes up to four new sources per run, commits accepted indexes and fingerprints, and deploys to `vodlock.vercel.app` when the repository has a `VERCEL_TOKEN` secret. Twitch discovery also needs `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` repository secrets from a registered Twitch application. The repository must be pushed to GitHub before the schedule can run.

The 720p limit applies only to the temporary analysis copy used for OCR. Website playback uses each platform's adaptive embedded player and can play 1080p when the platform selects it for the viewer's display, bandwidth, device, and source video.
