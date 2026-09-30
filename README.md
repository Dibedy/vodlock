<div align="center">
  <img src="site/favicon.svg" width="72" height="72" alt="SPOILLESS crossed-eye mark">
  <h1>SPOILLESS</h1>
  <p><strong>Watch VALORANT VODs without learning the result first.</strong></p>
  <p>Round-by-round navigation for official broadcasts and watch parties, with timelines, scores, durations, brackets, and future matches kept out of sight.</p>
  <p>
    <a href="https://spoilless.vercel.app/"><strong>Open SPOILLESS</strong></a>
    ·
    <a href="site/README.md">Website documentation</a>
    ·
    <a href="vodlock/README.md">Extension documentation</a>
  </p>
</div>

## What is included

SPOILLESS is built as three small, independent parts:

| Component | Purpose |
| --- | --- |
| `site/` | Static, account-free viewer for the published match library |
| `vodlock/` | Manifest V3 browser extension for spoiler-safe YouTube, Twitch, and vods.space viewing |
| `indexer/` | Local Round Studio and the automatic VOD indexing pipeline |

The website embeds the original YouTube or Twitch broadcast. It does not host or redistribute video. Each match uses a compact JSON index of verified round starts, so navigation is immediate and no analysis runs on a viewer's device.

## Spoiler-safe by design

- No scores, results, map totals, round totals, VOD durations, thumbnails, or exposed progress bars.
- Matches are ordered by when they were played, not when a permanent upload appeared.
- Favourite teams surface unwatched matches without revealing tournament advancement.
- Official broadcasts and creator watch parties are grouped under the same match.
- Previous and next controls remain visually consistent instead of revealing when the final round or map has been reached.
- Playback preferences and watch progress stay in browser storage; there are no SPOILLESS accounts or analytics.

SPOILLESS can hide interface spoilers, not information already visible inside the broadcast image.

## Use the website

Open [spoilless.vercel.app](https://spoilless.vercel.app/), choose a match and select a broadcast. The player begins at the opening round and provides spoiler-safe round and map navigation.

The viewer is plain HTML, CSS, and JavaScript. To preview it locally:

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory site
```

Then open `http://127.0.0.1:4173`.

## Install the browser extension

1. Open `chrome://extensions` in a Chromium-based browser.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select the `vodlock` folder.
4. Refresh any open YouTube, Twitch, or vods.space tabs.

The extension adds a spoiler shield, configurable replay skipping, safe navigation for imported indexes, and a visual fallback for long breaks. See [`vodlock/README.md`](vodlock/README.md) for controls and limitations.

## Build an index locally

Round Studio currently has a Windows launcher and requires Python 3.10–3.12.

1. Double-click **Start Round Studio.cmd**. The first launch creates `indexer/.venv` and installs the pinned dependencies.
2. Add a public YouTube VOD or an exact local recording.
3. Build and review the detected round starts.
4. Correct or exclude uncertain detections, then export the index or send it directly to the extension.
5. Use **Stop Round Studio.cmd** when finished.

Round Studio listens only on `127.0.0.1:8766`. Generated data stays under `indexer/data/`, and local recordings are never modified or deleted.

## Automatic indexing

The publishing pipeline monitors configured official and watch-party sources after their broadcasts finish. It:

1. Tries a 540p adaptive pass using compact HUD regions and clock-first OCR.
2. Samples possible gameplay cheaply, then increases analysis around potential round starts.
3. Retries with the established 720p full-frame pass if confidence is insufficient.
4. Reuses a verified official index for matching watch parties when visual alignment is unambiguous.
5. Rejects alignments with gaps, cuts, reconnects, or missing round sequences and falls back to independent OCR.
6. Publishes only complete, high-confidence indexes with no detector warnings.

The scheduled workflow can process two independent sources concurrently. Detailed publishing and deployment instructions are in [`site/README.md`](site/README.md).

## Development

Install the indexer dependencies in a Python 3.10–3.12 virtual environment:

```powershell
python -m venv indexer/.venv
indexer/.venv/Scripts/python.exe -m pip install -r indexer/requirements.txt
```

Run the full test suites:

```powershell
node --test tests/*.test.cjs
indexer/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

The repository contains no build step for the website or extension. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) before proposing behavior that could reveal match or tournament progression.

## Accuracy and limitations

The OCR detector targets the VCT top-centre `ROUND` label and timer. Different HUDs, low-resolution text, covered clocks, camera cuts, and late replay returns can produce missed detections. Automatic publication is deliberately strict, and locally generated indexes should be reviewed before use.

YouTube and Twitch can change their players, delivery systems, or page markup at any time. Provider updates may temporarily affect downloading, embedding, or extension behavior.

## Privacy and third-party services

The website has no first-party analytics, advertising, accounts, payments, or forms. Starting an embedded player loads the selected provider and is then subject to that provider's policies. Selected Twitch broadcasts include a read-only archive of public VOD chat. See the live [Privacy Policy](https://spoilless.vercel.app/privacy.html) and [Terms](https://spoilless.vercel.app/terms.html).

VALORANT and VCT are trademarks of Riot Games. YouTube, Twitch, team names, event names, broadcasts, and related assets belong to their respective owners. SPOILLESS is an independent project and is not affiliated with or endorsed by Riot Games, YouTube, or Twitch.
