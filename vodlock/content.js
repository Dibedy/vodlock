(() => {
  'use strict';

  const defaults = {
    enabled: true,
    hideTimeline: true,
    hideMetadata: true,
    hideSidebar: true,
    hideComments: true,
    hideThumbnails: true,
    safeControls: true,
    smartSkip: true,
    hideBrowseDurations: true,
    endScreenProtection: true,
    panicShield: true,
    replaySkipSeconds: 31
  };

  const VCT_PROFILE_REGIONS = [
    [0.467, 0.005, 0.533, 0.069],
    ...Array.from({ length: 5 }, (_, row) => [0.008, 0.508 + row * 0.098, 0.171, 0.590 + row * 0.098]),
    ...Array.from({ length: 5 }, (_, row) => [0.829, 0.508 + row * 0.098, 0.992, 0.590 + row * 0.098]),
    ...[0.01, 0.22, 0.52, 0.80].map(x => [x, 0.725, x + 0.18, 0.975])
  ];

  const VCT_GAMEPLAY_REFERENCE = [[61.368,60.684,0.7273,0.1049],[60.493,39.685,0.6981,0.0425],[51.458,40.2,0.7862,0.0403],[59.308,37.881,0.7995,0.0377],[60.467,42.24,0.73,0.0554],[55.724,38.965,0.7761,0.0428],[95.649,43.801,0.4139,0.1462],[76.431,48.074,0.6403,0.1258],[76.143,40.844,0.5884,0.0625],[68.533,48.089,0.7017,0.0979],[63.645,49.525,0.7082,0.1082],[60.302,37.466,0.7676,0.0409],[48.21,34.914,0.8699,0.0206],[53.715,40.757,0.7744,0.0281],[68.194,45.334,0.6769,0.0742]];
  const VCT_PREROUND_REFERENCE = [[42.268,32.613,0.8681,0.0195],[61.004,42.175,0.7785,0.0663],[51.564,39.503,0.8231,0.0491],[44.981,35.953,0.8651,0.0319]];

  let settings = { ...defaults };
  let controls;
  let toast;
  let toastTimer;
  let shield;
  let activeScan;
  let panicShieldVisible = false;
  let analysisCanvas;
  let analysisContext;
  let roundIndex;
  let roundIndexRequest = 0;

  const root = document.documentElement;

  function isYouTube() {
    return location.hostname.includes('youtube.com');
  }

  function isTwitch() {
    return location.hostname.includes('twitch.tv');
  }

  function getVideo() {
    const videos = [...document.querySelectorAll('video')].filter(v => {
      const rect = v.getBoundingClientRect();
      return rect.width > 200 && rect.height > 100;
    });
    if (!videos.length) return document.querySelector('video');
    return videos.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight))[0];
  }

  function youtubeVideoId() {
    if (!isYouTube()) return null;
    const url = new URL(location.href);
    return url.searchParams.get('v') || url.pathname.match(/^\/embed\/([A-Za-z0-9_-]{11})/)?.[1] || null;
  }

  async function loadRoundIndex() {
    const request = ++roundIndexRequest;
    const identifier = youtubeVideoId();
    roundIndex = undefined;
    root.classList.remove('vodlock-index-ready');
    if (identifier) {
      const key = 'roundIndex:' + identifier;
      const stored = await chrome.storage.local.get(key);
      if (request !== roundIndexRequest || youtubeVideoId() !== identifier) return;
      if (stored[key]) {
        try {
          const index = VodlockIndex.validate(stored[key]);
          if (index.videoId !== identifier) throw new Error('The saved index belongs to another video.');
          roundIndex = index;
        } catch (error) {
          showToast('Saved round index is invalid: ' + error.message, 5000);
        }
      }
    }
    root.classList.toggle('vodlock-index-ready', !!roundIndex);
    const button = controls?.querySelector('[data-action="skip-break"]');
    if (button) {
      button.textContent = roundIndex ? 'Next Round ↓' : 'Skip Long Break ↓';
      button.title = roundIndex ? 'Down Arrow: jump to the next indexed round' : 'Down Arrow: search for the VCT gameplay HUD';
    }
  }

  function seekIndexedRound(direction) {
    const video = getVideo();
    if (!roundIndex || roundIndex.videoId !== youtubeVideoId() || !video || activeScan) return;
    const destination = VodlockIndex.destination(roundIndex, video.currentTime, direction);
    if (destination === null || destination >= video.duration) {
      showToast('No indexed round in that direction. The index may be incomplete', 3000);
      return;
    }
    if (direction > 0) {
      const position = roundIndex.rounds.findIndex(round => Math.max(0, round.start - roundIndex.leadSeconds) === destination);
      const previous = roundIndex.rounds[position - 1];
      const target = roundIndex.rounds[position];
      if (previous && target.map === previous.map && target.round > previous.round + 1) {
        showToast('This index has missing rounds here. Review and add them in Round Studio before skipping', 5000);
        return;
      }
    }
    video.currentTime = destination;
    showToast(direction > 0 ? 'Next round · five-second lead-in' : 'Previous round · five-second lead-in', 2500);
  }

  function updateClasses() {
    root.classList.toggle('vodlock-enabled', settings.enabled);
    root.classList.toggle('vodlock-hide-timeline', settings.enabled && settings.hideTimeline);
    root.classList.toggle('vodlock-hide-metadata', settings.enabled && settings.hideMetadata);
    root.classList.toggle('vodlock-hide-sidebar', settings.enabled && settings.hideSidebar);
    root.classList.toggle('vodlock-hide-comments', settings.enabled && settings.hideComments);
    root.classList.toggle('vodlock-hide-thumbnails', settings.enabled && settings.hideThumbnails);
    root.classList.toggle('vodlock-safe-controls', settings.enabled && settings.safeControls);
    root.classList.toggle('vodlock-smart-skip', settings.enabled && settings.smartSkip);
    root.classList.toggle('vodlock-hide-browse-durations', settings.enabled && settings.hideBrowseDurations);
    root.classList.toggle('vodlock-end-screen-protection', settings.enabled && settings.endScreenProtection);
  }

  function showToast(message, duration = 1100) {
    if (!toast) return;
    const host = document.fullscreenElement?.tagName === 'VIDEO' ? root : document.fullscreenElement || root;
    if (toast.parentElement !== host) host.append(toast);
    toast.textContent = message;
    toast.classList.add('vodlock-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast?.classList.remove('vodlock-show'), duration);
  }

  function seekBy(seconds) {
    if (activeScan) return;
    const video = getVideo();
    if (!video || !Number.isFinite(video.duration)) {
      showToast('No VOD video detected');
      return;
    }

    const next = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
    video.currentTime = next;
    showToast(seconds > 0 ? `Skipped forward ${formatSkip(seconds)}` : `Back ${formatSkip(-seconds)}`);
  }

  function formatSkip(seconds) {
    if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60}m`;
    return `${seconds}s`;
  }

  async function togglePlayback() {
    if (activeScan) return;
    const video = getVideo();
    if (!video) {
      showToast('No video detected');
      return;
    }
    if (video.paused) {
      try { await video.play(); } catch (_) {}
    } else {
      video.pause();
    }
    updatePlayButton();
  }

  function updatePlayButton() {
    const button = controls?.querySelector('[data-action="play"]');
    const video = getVideo();
    if (!button || !video) return;
    button.textContent = video.paused ? 'Play' : 'Pause';
  }

  function createShield() {
    if (shield) return shield;
    shield = document.createElement('div');
    shield.id = 'vodlock-shield';
    shield.innerHTML = `
      <div class="vodlock-shield-card">
        <strong>SPOILLESS</strong>
        <span id="vodlock-shield-status">Scanning safely…</span>
        <button type="button" id="vodlock-cancel-scan">Cancel</button>
      </div>
    `;
    shield.querySelector('#vodlock-cancel-scan').addEventListener('click', () => {
      if (panicShieldVisible && !activeScan) {
        togglePanicShield(false);
        return;
      }
      if (activeScan) activeScan.controller.abort();
    });
    document.documentElement.append(shield);
    return shield;
  }

  function showShield(text = 'Scanning safely…') {
    createShield();
    const host = document.fullscreenElement || root;
    if (shield.parentElement !== host) host.append(shield);
    const status = shield.querySelector('#vodlock-shield-status');
    if (status) status.textContent = text;
    shield.classList.add('vodlock-visible');
  }

  function hideShield() {
    shield?.classList.remove('vodlock-visible');
  }

  function togglePanicShield(force) {
    if (activeScan) return;
    if (!settings.enabled || !settings.panicShield) return;
    const next = typeof force === 'boolean' ? force : !panicShieldVisible;
    panicShieldVisible = next;
    if (next) {
      showShield('Spoiler shield active · press Escape to reveal');
      const cancel = shield?.querySelector('#vodlock-cancel-scan');
      if (cancel) cancel.textContent = 'Reveal';
    } else {
      hideShield();
      const cancel = shield?.querySelector('#vodlock-cancel-scan');
      if (cancel) cancel.textContent = 'Cancel';
    }
  }

  function checkScan(scan) {
    if (scan.controller.signal.aborted) throw new Error('SCAN_CANCELLED');
    if (getVideo() !== scan.video || location.href !== scan.url) throw new Error('VIDEO_CHANGED');
    if (performance.now() > scan.deadline) throw new Error('SCAN_TIMEOUT');
  }

  function seekTo(video, time, signal, decode = false) {
    return new Promise((resolve, reject) => {
      let timer;
      let poll;
      let done = false;
      let playError;
      const events = ['seeked', 'loadeddata', 'canplay', 'timeupdate', 'progress'];
      const ready = () => !video.seeking && video.readyState >= 2 &&
        Math.abs(video.currentTime - time) <= 1;
      const finish = error => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        clearInterval(poll);
        for (const event of events) video.removeEventListener(event, onReady);
        video.removeEventListener('error', onError);
        signal?.removeEventListener('abort', onAbort);
        error ? reject(error) : resolve();
      };
      const onAbort = () => finish(new Error('SCAN_CANCELLED'));
      const onError = () => finish(new Error('PLAYER_ERROR'));
      const onReady = () => {
        if (ready()) finish();
      };

      if (signal?.aborted) {
        reject(new Error('SCAN_CANCELLED'));
        return;
      }
      if (Math.abs(video.currentTime - time) < 0.05 && ready()) {
        resolve();
        return;
      }
      for (const event of events) video.addEventListener(event, onReady);
      video.addEventListener('error', onError);
      signal?.addEventListener('abort', onAbort, { once: true });
      timer = setTimeout(() => {
        if (ready()) {
          finish();
        } else {
          const error = new Error(video.readyState < 2 ? 'FRAME_LOAD_TIMEOUT' : 'SEEK_TIMEOUT');
          if (playError) error.cause = playError;
          finish(error);
        }
      }, 12000);
      poll = setInterval(onReady, 100);

      try {
        video.currentTime = time;
        if (decode && video.paused) {
          video.play().catch(error => {
            playError = error;
            onReady();
          });
        }
      } catch (error) {
        finish(error);
      }
    });
  }

  function regionFeature(data, width, height, x0, y0, x1, y1) {
    const sx = Math.max(0, Math.floor(width * x0));
    const sy = Math.max(0, Math.floor(height * y0));
    const ex = Math.min(width, Math.ceil(width * x1));
    const ey = Math.min(height, Math.ceil(height * y1));
    let sum = 0, sumSquared = 0, dark = 0, bright = 0, n = 0;

    for (let y = sy; y < ey; y++) {
      for (let x = sx; x < ex; x++) {
        const i = (y * width + x) * 4;
        const lum = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
        sum += lum;
        sumSquared += lum * lum;
        if (lum < 75) dark++;
        if (lum > 150) bright++;
        n++;
      }
    }

    if (!n) return [0, 0, 0, 0];
    const mean = sum / n;
    return [mean, Math.sqrt(Math.max(0, sumSquared / n - mean * mean)), dark / n, bright / n];
  }

  function captureHud(video) {
    if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
      throw new Error('FRAME_NOT_READY');
    }
    if (!analysisCanvas) {
      analysisCanvas = document.createElement('canvas');
      analysisCanvas.width = 320;
      analysisCanvas.height = 180;
      analysisContext = analysisCanvas.getContext('2d', { willReadFrequently: true });
    }
    if (!analysisContext) throw new Error('Canvas unavailable');

    try {
      analysisContext.drawImage(video, 0, 0, 320, 180);
      const image = analysisContext.getImageData(0, 0, 320, 180);
      return VCT_PROFILE_REGIONS.map(region => regionFeature(image.data, 320, 180, ...region));
    } catch (error) {
      const wrapped = new Error('FRAME_ACCESS_BLOCKED');
      wrapped.cause = error;
      throw wrapped;
    }
  }

  function signatureSimilarity(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== 4 || b.length !== 4) return 0;
    if (!a.every(Number.isFinite) || !b.every(Number.isFinite)) return 0;
    const difference = Math.abs(a[0] - b[0]) / 80 +
      Math.abs(a[1] - b[1]) / 60 +
      Math.abs(a[2] - b[2]) +
      Math.abs(a[3] - b[3]) * 2;
    return Math.max(0, 1 - difference / 4);
  }

  function matchesGameplay(hud) {
    if (!Array.isArray(hud) || hud.length !== VCT_GAMEPLAY_REFERENCE.length) return false;
    const timer = hud[0];
    if (signatureSimilarity(timer, VCT_GAMEPLAY_REFERENCE[0]) < 0.85 ||
        timer[1] < 35 || timer[2] < 0.50 || timer[3] < 0.025 || timer[3] > 0.25) return false;

    const matchesCard = (region, reference) => signatureSimilarity(region, reference) >= 0.80 &&
      region[1] >= 20 && region[2] >= 0.35 && region[3] >= 0.008;
    const leftMatches = hud.slice(1, 6).filter((region, index) =>
      matchesCard(region, VCT_GAMEPLAY_REFERENCE[index + 1])).length;
    const rightMatches = hud.slice(6, 11).filter((region, index) =>
      matchesCard(region, VCT_GAMEPLAY_REFERENCE[index + 6])).length;
    const preroundMatches = hud.slice(11).filter((region, index) =>
      matchesCard(region, VCT_PREROUND_REFERENCE[index])).length;

    return (leftMatches >= 3 && rightMatches >= 3) || preroundMatches >= 3;
  }

  async function sampleGameplay(scan, time) {
    checkScan(scan);
    showShield('Loading a broadcast frame…');
    await seekTo(scan.video, time, scan.controller.signal, true);
    checkScan(scan);
    return matchesGameplay(captureHud(scan.video));
  }

  async function findLongBreakEnd(scan) {
    const end = Math.min(scan.video.duration - 3, scan.startTime + 45 * 60);
    let previousTime = scan.startTime;

    for (let time = scan.startTime + 30; time <= end; time = Math.min(time + 60, end)) {
      showShield('Searching for the VCT gameplay HUD…');
      if (await sampleGameplay(scan, time)) {
        if (time + 2 <= end && await sampleGameplay(scan, time + 2)) {
          let lower = previousTime;
          let upper = time;
          while (upper - lower > 2) {
            const midpoint = (lower + upper) / 2;
            showShield('Locating the gameplay return…');
            if (await sampleGameplay(scan, midpoint)) {
              upper = midpoint;
            } else {
              lower = midpoint;
            }
          }
          return Math.max(scan.startTime, upper - 5);
        }
      }
      previousTime = time;
      if (time === end) break;
    }
    throw new Error('NO_GAMEPLAY_MATCH');
  }

  async function skipSegment(mode) {
    if (activeScan) {
      showToast('A search is already running. Press Escape to cancel', 2500);
      return;
    }
    if (!settings.enabled || !settings.smartSkip) return;
    const video = getVideo();
    if (!video || !Number.isFinite(video.duration) || video.duration <= 0) {
      showToast('No VOD video detected');
      return;
    }
    if (mode === 'break' && roundIndex?.videoId === youtubeVideoId()) {
      seekIndexedRound(1);
      return;
    }
    if (mode === 'break' && document.fullscreenElement?.tagName === 'VIDEO') {
      showToast('Exit native video fullscreen before skipping', 3200);
      return;
    }
    const scan = {
      video,
      url: location.href,
      startTime: video.currentTime,
      wasPaused: video.paused,
      wasMuted: video.muted,
      controller: new AbortController(),
      deadline: performance.now() + 45000
    };
    activeScan = scan;
    let cancel;
    let completed = false;
    let message;

    try {
      video.pause();
      video.muted = true;
      showShield(mode === 'replay' ? 'Skipping replay…' : 'Searching for gameplay…');
      cancel = shield.querySelector('#vodlock-cancel-scan');
      if (cancel) cancel.textContent = 'Cancel';
      const configured = Number(settings.replaySkipSeconds);
      const replaySeconds = Number.isFinite(configured) ? Math.max(5, Math.min(60, configured)) : 31;
      const destination = mode === 'replay'
        ? Math.min(video.duration - 0.1, scan.startTime + replaySeconds)
        : await findLongBreakEnd(scan);
      checkScan(scan);
      await seekTo(video, destination, scan.controller.signal, mode === 'break');
      video.pause();
      checkScan(scan);
      completed = true;
      message = mode === 'replay' ? 'Replay skipped' : 'Gameplay found';
    } catch (error) {
      const messages = {
        SCAN_CANCELLED: 'Skip cancelled',
        VIDEO_CHANGED: 'Video changed. Skip stopped',
        SCAN_TIMEOUT: 'Search timed out. Returned to your original position',
        SEEK_TIMEOUT: 'Player could not reach the requested position',
        FRAME_LOAD_TIMEOUT: 'Player did not load a video frame',
        PLAYER_ERROR: 'Player reported a video loading error',
        FRAME_ACCESS_BLOCKED: 'This player blocks frame analysis',
        NO_GAMEPLAY_MATCH: 'VCT gameplay HUD was not found'
      };
      message = messages[error.message] || 'Skip failed: ' + error.message;
      if (error.cause?.name) message += ` (${error.cause.name})`;
    } finally {
      const sameVideo = getVideo() === video && location.href === scan.url;
      video.pause();
      if (!completed && sameVideo) {
        try {
          await seekTo(video, scan.startTime, undefined, mode === 'break');
        } catch (error) {
          message += '. Could not confirm the restored frame';
          if (error.cause?.name) message += ` (${error.cause.name})`;
        }
      }
      video.pause();
      video.muted = scan.wasMuted;
      if (sameVideo && !scan.wasPaused) {
        try {
          await video.play();
        } catch (error) {
          message += `. Press Play to resume (${error.name})`;
        }
      }
      activeScan = undefined;
      if (panicShieldVisible) {
        showShield('Spoiler shield active · press Escape to reveal');
        if (cancel) cancel.textContent = 'Reveal';
      } else {
        hideShield();
      }
      updatePlayButton();
      showToast(message, completed ? 3200 : 6000);
    }
  }

  function createControls() {
    if (document.getElementById('vodlock-controls')) {
      controls = document.getElementById('vodlock-controls');
      return;
    }

    controls = document.createElement('div');
    controls.id = 'vodlock-controls';
    controls.setAttribute('role', 'toolbar');
    controls.setAttribute('aria-label', 'SPOILLESS spoiler safe controls');
    controls.innerHTML = `
      <button type="button" data-seek="-30" title="Back 30 seconds">−30s</button>
      <button type="button" data-action="play" class="vodlock-primary" title="Play or pause">Pause</button>
      <button type="button" data-seek="30" title="Forward 30 seconds">+30s</button>
      <button type="button" data-seek="120" title="Forward 2 minutes">+2m</button>
      <button type="button" data-seek="300" title="Forward 5 minutes">+5m</button>
      <span class="vodlock-divider" aria-hidden="true"></span>
      <button type="button" data-action="skip-replay" class="vodlock-smart vodlock-skip-break" title="Right Arrow: skip replay">Skip Replay →</button>
      <button type="button" data-action="previous-round" class="vodlock-indexed" title="Previous indexed round">← Previous Round</button>
      <button type="button" data-action="skip-break" class="vodlock-smart vodlock-skip-break" title="Down Arrow: search for the VCT gameplay HUD">Skip Long Break ↓</button>
    `;

    controls.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.dataset.seek) seekBy(Number(button.dataset.seek));
      if (button.dataset.action === 'play') togglePlayback();
      if (button.dataset.action === 'skip-replay') skipSegment('replay');
      if (button.dataset.action === 'skip-break') skipSegment('break');
      if (button.dataset.action === 'previous-round') seekIndexedRound(-1);
    });

    toast = document.createElement('div');
    toast.id = 'vodlock-toast';
    toast.setAttribute('aria-live', 'polite');

    document.documentElement.append(controls, toast);
  }

  function scrubTextSpoilers() {
    if (!settings.enabled) return;

    if (isYouTube() && settings.hideTimeline) {
      document.querySelectorAll('ytd-thumbnail-overlay-time-status-renderer, #time-status').forEach(el => {
        el.classList.add('vodlock-hidden');
      });
    }

    if (settings.hideBrowseDurations) {
      // YouTube keeps changing the markup used for duration badges. Hide both
      // known elements and short time-like badges that live inside thumbnails/cards.
      const ytDurationSelectors = [
        'ytd-thumbnail-overlay-time-status-renderer',
        'yt-thumbnail-overlay-badge-view-model',
        '.yt-badge-shape',
        '#time-status',
        '[overlay-style="DEFAULT"]'
      ];
      if (isYouTube()) {
        document.querySelectorAll(ytDurationSelectors.join(',')).forEach(el => {
          const text = (el.textContent || '').trim();
          if (/^\d{1,2}:\d{2}(?::\d{2})?$/.test(text) || el.matches('ytd-thumbnail-overlay-time-status-renderer, #time-status')) {
            el.classList.add('vodlock-hidden');
          }
        });
      }

      if (isTwitch()) {
        document.querySelectorAll('a, article, [data-a-target], [class*="card" i], [class*="thumbnail" i]').forEach(card => {
          card.querySelectorAll('span, div').forEach(el => {
            const text = (el.textContent || '').trim();
            if (/^\d{1,2}:\d{2}(?::\d{2})?$/.test(text) || /^\d+\s*(?:h|hr|hrs|hour|hours)\s*\d*\s*(?:m|min|mins|minute|minutes)?$/i.test(text)) {
              el.classList.add('vodlock-hidden');
            }
          });
        });
      }
    }

    if (isTwitch() && settings.hideTimeline) {
      const candidates = document.querySelectorAll('[data-a-target], [aria-label]');
      for (const el of candidates) {
        const target = (el.getAttribute('data-a-target') || '').toLowerCase();
        const label = (el.getAttribute('aria-label') || '').toLowerCase();
        if (
          target.includes('seekbar') ||
          label.includes('seek') ||
          label.includes('video progress') ||
          label.includes('duration') ||
          label.includes('elapsed')
        ) {
          el.classList.add('vodlock-hidden');
        }
      }
    }
  }

  function protectDocumentTitle() {
    if (!settings.enabled || !settings.hideMetadata) return;
    const site = isYouTube() ? 'YouTube' : isTwitch() ? 'Twitch' : 'VOD';
    const safeTitle = `SPOILLESS · ${site}`;
    if (document.title !== safeTitle) document.title = safeTitle;
  }

  function onKeyDown(event) {
    const tag = event.target?.tagName?.toLowerCase();
    if ((tag === 'input' && !['range', 'button', 'submit', 'reset', 'checkbox', 'radio'].includes(event.target.type)) ||
        tag === 'textarea' || tag === 'select' || event.target?.isContentEditable) return;

    if (event.code === 'Escape' && activeScan) {
      event.preventDefault();
      event.stopImmediatePropagation();
      activeScan.controller.abort();
      return;
    }

    if (event.code === 'Escape' && panicShieldVisible) {
      event.preventDefault();
      event.stopImmediatePropagation();
      togglePanicShield(false);
      return;
    }

    if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey &&
        (event.code === 'ArrowRight' || event.code === 'ArrowDown') && settings.enabled && settings.smartSkip) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!event.repeat) return skipSegment(event.code === 'ArrowRight' ? 'replay' : 'break');
      return;
    }

    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;

    if (event.code === 'KeyS') {
      event.preventDefault();
      chrome.storage.sync.set({ enabled: !settings.enabled });
      return;
    }

    if (!settings.enabled) return;

    if (event.code === 'ArrowLeft') {
      event.preventDefault();
      seekBy(-30);
    } else if (event.code === 'ArrowRight') {
      event.preventDefault();
      seekBy(30);
    } else if (event.code === 'ArrowUp') {
      event.preventDefault();
      seekBy(120);
    } else if (event.code === 'KeyH' && settings.panicShield) {
      event.preventDefault();
      togglePanicShield();
    }
  }

  function bindVideoEvents() {
    const video = getVideo();
    if (!video || video.dataset.vodlockBound === '1') return;
    video.dataset.vodlockBound = '1';
    video.addEventListener('play', updatePlayButton);
    video.addEventListener('pause', updatePlayButton);
    video.addEventListener('loadedmetadata', updatePlayButton);
    updatePlayButton();
  }

  function refresh() {
    updateClasses();
    createControls();
    scrubTextSpoilers();
    bindVideoEvents();
    protectDocumentTitle();
  }

  async function init() {
    settings = await chrome.storage.sync.get(defaults);
    refresh();

    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'local') {
        if (changes['roundIndex:' + youtubeVideoId()]) loadRoundIndex();
        return;
      }
      if (area !== 'sync') return;
      for (const [key, value] of Object.entries(changes)) {
        settings[key] = value.newValue;
      }
      if (activeScan && (!settings.enabled || !settings.smartSkip)) activeScan.controller.abort();
      refresh();
    });

    window.addEventListener('keydown', onKeyDown, true);

    if (isYouTube()) {
      ['yt-navigate-finish', 'yt-page-data-updated', 'yt-player-updated'].forEach(name => {
        document.addEventListener(name, () => setTimeout(() => { refresh(); loadRoundIndex(); }, 50));
      });
    }

    await loadRoundIndex();

    const observer = new MutationObserver(refresh);
    observer.observe(document.documentElement, { childList: true, subtree: true });

    setInterval(() => {
      bindVideoEvents();
      protectDocumentTitle();
    }, 1000);
  }

  init();
})();
