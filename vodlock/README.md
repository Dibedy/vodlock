# VODLOCK 3.0.0

## Indexed rounds

Round Studio builds a local round-start index outside the viewing player. Start it using **Start Round Studio.cmd** in the complete package, add the video, review its detections, then use **Use in VODLOCK**. You can also import an exported JSON from the extension popup.

On the matching YouTube VOD, **Down Arrow** becomes an immediate next-round jump with five seconds of lead-in. Previous/Next round buttons appear in the safe toolbar. The index is saved locally and remains usable when Round Studio is closed. Right Arrow still performs the fixed replay skip. Videos without an index retain the long-break search below.

The detector is experimental and supports the VCT top-centre ROUND label and timer. It requires consistent early-round clock readings and checks for the replay badge; other layouts or obscured clocks may be missed. Review accuracy before trusting a new broadcast. See the package README for setup, storage and limitations.

## vods.space

The site's decorative white button gradient is removed from the full-video click-to-play layer. Click-to-play remains functional.

Archive match scores are replaced with a neutral vs label, and map links are hidden to avoid revealing the number of maps played. Team names and Play links remain usable.

Open a replay on vods.space. Spoiler mode hides the progress bar, full duration, round totals, future round list, map list and notes panel. Previous/Next round and map buttons use the site's existing navigation; only the current round and map are displayed. Both navigation directions stay present so their appearance does not reveal the final round or map. Access-restricted rounds still use the site's normal access checks.

The existing popup controls apply: Hide timeline controls the spoiler-safe round/map navigation; Hide comments controls the notes panel; Hide metadata hides the player title overlay. Disable spoiler mode to restore the site's controls. Embedded YouTube players also receive timeline, title and end-screen protection, without adding a second VODLOCK toolbar.

Scores or results baked into the video itself are not covered. This protects the website interface, not the broadcast image. The integration depends on the current vods.space markup and may need updating if the site changes.

Arrow shortcuts work while player sliders and buttons have focus. Typing in text fields is unaffected. Keyboard handling runs in window capture phase to prevent player document handlers from consuming the shortcut. Scan startup failures release the search state and restore playback.

Long-break searches keep muted playback running under the shield between frame probes. Seeking checks frame readiness through media events and polling rather than requiring one seeked event. A single frame has a 12-second loading limit; ready frames continue immediately. Final landing and rollback use the same frame-loading behavior.

A rejected play request does not by itself cancel frame analysis: a paused or interrupted player can still load the requested frame. If no frame loads, the error retains the underlying playback error type. Errors remain visible for six seconds.

Spoiler-safe YouTube and Twitch VOD viewing with separate replay and long-break controls.

## Replay skipping

Press **Right Arrow** or **Skip Replay** for one immediate jump. The default is 31 seconds: the supplied example starts at 2:25:16 and resumes at 2:25:47. Change Replay jump in the popup for broadcasts with shorter or longer replays. This control performs no frame search and works when canvas access is blocked. It is a fixed jump: pressing it partway through a replay can overshoot.

## Long-break skipping

The gameplay reference is built in from the supplied VCT screenshot. Detection compares the top timer panel and repeated player-card backgrounds using luminance, contrast, and dark/light pixel proportions. It ignores the minimap, centre scene, sponsors, and team colors. The earlier pre-round screenshot supplies a built-in bottom-table reference for that alternate layout. No setup or HUD learning is required.

Press **Down Arrow** or **Skip Long Break** during downtime. VODLOCK checks 30 seconds ahead first, then searches in 60-second steps for the built-in HUD reference, confirms a candidate two seconds later, refines the boundary to within two seconds, and lands five seconds before the detected return. This detects the HUD return, not the round timer: if the HUD appears early in buy phase, some pre-round may remain.

Searches cover up to 45 minutes of VOD time and stop after approximately 45 seconds of actual search time. Cancel or press Escape to return to the original position. Failed searches restore the original position, mute setting, and pause/play state. Long-break detection requires canvas frame access. Scan frames stay hidden under the shield, including player fullscreen. Exit native video fullscreen before skipping.

## Other controls

- Alt + Left / Right: back / forward 30 seconds
- Alt + Up: forward 2 minutes
- Alt + S: toggle VODLOCK
- Alt + H: spoiler shield
- Escape: cancel search or reveal spoiler shield

## Install

1. Extract the ZIP.
2. Open chrome://extensions and enable Developer mode.
3. Choose Load unpacked and select the extracted vodlock folder.
4. Remove or reload the old extension and refresh the VOD page.
