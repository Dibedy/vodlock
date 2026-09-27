# VODLOCK · Round Studio

A local, independent Valorant round indexer paired with the VODLOCK spoiler-safe browser extension. No subscription, server account or vods.space timestamp database is used.

## Standalone website

The `site` folder contains a Vercel-ready viewer that requires no extension and performs no processing on the viewer's computer. It embeds the original YouTube or Twitch broadcast, fetches a precomputed JSON index, starts at the opening round and provides previous/next map and round controls.

The website is seeded with the supplied `ZphbktbT26k` index. See `site/README.md` for local preview, deployment and catalog publishing instructions. Round Studio remains the private tool used by the catalog owner to generate and review new indexes once before publishing them for everyone.

## Try your supplied VOD immediately

The package includes `vodlock-ZphbktbT26k.json`, already built and clock-checked for your YouTube link. Load the updated `vodlock` extension folder, open its popup and choose **Import round index**, then select that JSON. Open the same video and press Down Arrow. You do not need to start Round Studio or process this video again to use the included index.

## Start

1. Load the `vodlock` folder using **Load unpacked** on `chrome://extensions`. Remove or reload your older VODLOCK installation, then refresh your video tabs.
2. Double-click **Start Round Studio.cmd**. The first launch creates a project-local Python environment and installs the packages in `indexer/requirements.txt`. Python 3.10–3.12 is required; Python 3.12 is recommended. The bundled Codex Python runtime is also supported when available.
3. Paste a public YouTube VOD link, or choose **Local recording** and paste the full path to a video file. A local recording must be the exact, uncut version of its linked YouTube VOD for timestamps to line up.
4. Build the index. Downloading and analysis happen outside your viewing player. Keep Round Studio running until it finishes.
5. Use **Review index** to check detections. This view deliberately reveals timestamps and detected round numbers. Save corrections, add missed rounds, or exclude false detections. Exclusion is reversible. Then press **Use in VODLOCK**.
6. Open the matching YouTube video. **Down Arrow** jumps to the next indexed round with five seconds of lead-in. The toolbar also has previous/next round controls. **Right Arrow** retains the configurable replay jump, defaulting to 31 seconds.

If the extension is not detected, reload the Round Studio page after installing it. Alternatively, export the JSON and import it in the extension popup. Once imported, round navigation works without Round Studio running.

Use **Stop Round Studio.cmd** to stop an instance started by the launcher. Stopping during analysis interrupts that job; add its source again to retry. Closing the browser tab alone does not stop the local server.

## Accuracy and performance

The detector is experimental. It reads the VCT top-centre `ROUND` label and countdown, checks the bottom-right replay sign, and requires two consistent early-round clock observations. It estimates the actual round start from the countdown rather than using the later confirmation frame. The imported index applies a five-second lead-in.

Different HUDs, low-resolution text, camera cuts, covered timers or late returns from replays can cause missed detections. Detected gaps are flagged for review, and VODLOCK refuses a forward round jump across a known gap within one map. Gaps at the end of a recording cannot reliably be identified. This is not a claim of equivalent accuracy to vods.space. Review a new broadcast before relying on its index.

Analysis reads one frame every two seconds. A full match can take several minutes or longer depending on CPU and video download speed. That cost is paid once; indexed navigation performs one direct seek with no gameplay search. Unindexed videos retain the existing long-break search as a fallback.

YouTube downloads require a public video accessible without cookies. Restricted or blocked downloads fail visibly; use an authorized local recording instead. Downloads are capped at 720p and 2 GB. YouTube may change its delivery system, so downloader compatibility can change.

## Privacy and files

The app listens only on `127.0.0.1:8766`. Videos and indexes are kept in `indexer/data`, dependencies in `indexer/.venv`, and imported indexes in the extension's local storage. The app does not upload video. YouTube requests and dependency installation still require an internet connection. If you place the package in OneDrive or another synced folder, that provider may independently sync generated files; use a non-synced folder if you do not want that.

Analysis copies are retained for inspection; they consume disk space. Stop Round Studio before manually removing unwanted job files from `indexer/data`. Local source recordings are never modified or deleted. Do not move the extension folder after loading it unpacked.

Spoiler protection hides website timelines, durations, metadata and end screens according to your popup settings. It does not hide scores baked into the broadcast itself. Round Studio's library does not show round totals or timestamps unless you open review mode.

## Development checks

Run `node --test tests/*.test.cjs` for website and extension logic, and `indexer/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"` for the clock detector and local API checks. Run `indexer/.venv/Scripts/python.exe indexer/server.py` to start the server from a terminal.
