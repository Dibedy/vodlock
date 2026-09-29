# SPOILLESS website

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
4. Add one matching entry to `catalog.json` with `provider`, `sourceId`, the index path, and the first round's UTC `playedAt` timestamp.
5. Redeploy the site.

Manually created indexes should be reviewed before publishing. Catalog cards intentionally omit scores, map totals, round totals, durations, and thumbnails.

## Automatic channel monitoring

`indexer/auto_publish.py` monitors the official VALORANT YouTube upload feed plus the `gofns`, `ohnepixel`, and `valorant` Twitch channels in `indexer/auto_channels.json`. Twitch discovery requests archived broadcasts and rejects any VOD whose stream ID is still reported live, so processing starts only after that broadcast has ended. Ohnepixel VODs must also have a VALORANT event marker in their title.

Official broadcasts are analyzed from Twitch without storing the full archive on the hosted runner. The adaptive pass uses a 540p source, sends only the relevant HUD regions through FFmpeg, checks the clock every four seconds, and switches to one-second analysis around possible round starts. Round, score, and replay OCR runs only inside the valid round-start clock window. An uncertain adaptive result is automatically retried with the established 720p full-frame analysis.

The official analysis records a small visual fingerprint every two seconds. Creator watch parties first try a sparse visual alignment against the matching official index. Piecewise timeline segments account for pauses, cuts, and reconnects; ambiguous or discontinuous results fall back to independent OCR instead of being published. When a `FULL MATCH` upload appears on the official YouTube channel, SPOILLESS downloads its timeline storyboard, verifies matching visual anchors across the beginning, middle, and end, detects breaks removed by the YouTube edit, and translates the round index to YouTube timestamps. It never publishes an uncertain alignment. Once verified, the permanent YouTube match replaces the temporary official Twitch card. Creator watch parties remain on Twitch with archived chat. Up to two independent VODs are processed concurrently after required official sources are ready. Hosted YouTube analysis runs a local proof-of-origin token provider to reduce bot-challenge failures without storing account cookies.

Automatic publishing is deliberately strict. An index is published only when it begins at map 1 round 1, contains at least 13 rounds, has no sequence gaps or detector warnings, and every detection meets the configured confidence threshold. Rejected indexes are recorded as held in `indexer/auto_state.json`. Detector-related holds wait for a pipeline update instead of repeatedly consuming runner time, while service and infrastructure failures retry after a cooldown. Held detector runs retain a small set of cropped HUD images as short-lived private workflow artifacts for diagnosis. Every run writes a private GitHub Actions summary with status counts, recent results and hold reasons. The manual workflow's **Retry held sources** option bypasses the normal cooldown for one eligible held source.

The GitHub Actions workflow in `.github/workflows/auto-publish.yml` checks the channels twice per hour, prioritizes fresh official broadcasts and match uploads over creator streams and retries, processes up to four sources per run, and commits accepted indexes and fingerprints. It does not run on code pushes and active processing is never cancelled by a deployment. Changes under `site/` independently trigger `.github/workflows/deploy-site.yml`, which tests and deploys the static viewer immediately when the repository has a `VERCEL_TOKEN` secret. Twitch discovery also needs `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` repository secrets from a registered Twitch application. The repository must be pushed to GitHub before the schedule can run.

The 540p adaptive and 720p fallback limits apply only to the sources used for OCR. Website playback uses each platform's adaptive embedded player and can play 1080p when the platform selects it for the viewer's display, bandwidth, device, and source video.
