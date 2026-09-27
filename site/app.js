(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const state = {catalog: [], entry: null, index: null, player: null, ready: false, started: false, playing: false, toastTimer: null, statusTimer: null};

  function toast(message) {
    clearTimeout(state.toastTimer);
    $('toast').textContent = message;
    $('toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
  }

  function setSearchMessage(message, error = false) {
    $('search-message').textContent = message;
    $('search-message').classList.toggle('error', error);
  }

  function destroyPlayer() {
    if (state.player?.destroy) state.player.destroy();
    state.player = null;
    state.ready = false;
    state.playing = false;
  }

  function resetPlayerHost() {
    const current = $('media-player');
    if (current && current.tagName === 'DIV') {
      current.replaceChildren();
      return;
    }
    if (current) current.remove();
    const host = document.createElement('div');
    host.id = 'media-player';
    $('player-shell').prepend(host);
  }

  function entryKey(entry) {
    return VodlockSite.sourceKey(entry.provider, entry.sourceId);
  }

  function renderCatalog() {
    const catalog = $('catalog');
    catalog.replaceChildren();
    state.catalog.forEach(entry => {
      const card = document.createElement('article');
      card.className = 'vod-card';
      const top = document.createElement('div');
      top.className = 'card-top';
      const ready = document.createElement('span');
      ready.className = 'index-ready';
      ready.textContent = entry.provider.toUpperCase() + ' · ROUND INDEX READY';
      top.append(ready);
      const title = document.createElement('h3');
      title.textContent = entry.title;
      const event = document.createElement('p');
      event.textContent = entry.event;
      const watch = document.createElement('button');
      watch.type = 'button';
      watch.textContent = 'Watch rounds';
      watch.addEventListener('click', () => openVideo(entryKey(entry)));
      card.append(top, title, event, watch);
      catalog.append(card);
    });
    if (!state.catalog.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No processed broadcasts are available yet.';
      catalog.append(empty);
    }
  }

  async function loadCatalog() {
    const response = await fetch('/catalog.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('The VOD catalog could not be loaded.');
    const value = await response.json();
    if (![1, 2].includes(value?.version) || !Array.isArray(value.videos)) throw new Error('The VOD catalog is invalid.');
    state.catalog = value.videos.map(entry => {
      const provider = entry?.provider || 'youtube';
      const sourceId = entry?.sourceId || entry?.videoId;
      return {...entry, provider, sourceId};
    }).filter(entry => {
      const parsed = VodlockSite.mediaSource(VodlockSite.sourceKey(entry.provider, entry.sourceId));
      return parsed && typeof entry.index === 'string' && /^\/indexes\/[A-Za-z0-9_-]+\.json$/.test(entry.index) &&
        typeof entry.title === 'string' && typeof entry.event === 'string';
    });
    renderCatalog();
  }

  function showLibrary(updateHistory = true) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
    destroyPlayer();
    state.started = false;
    state.entry = null;
    state.index = null;
    setPlaybackControlsDisabled(true);
    resetPlayerHost();
    $('player-view').hidden = true;
    $('library-view').hidden = false;
    $('end-shield').hidden = true;
    $('start-gate').hidden = false;
    $('start-watching').disabled = false;
    $('start-watching').textContent = 'Start watching';
    if (updateHistory) history.pushState({}, '', location.pathname);
    document.title = 'VODLOCK | Indexed VALORANT VODs';
    window.scrollTo({top: 0});
  }

  async function openVideo(key, updateHistory = true) {
    const source = VodlockSite.mediaSource(key);
    const entry = source && state.catalog.find(candidate => entryKey(candidate) === VodlockSite.sourceKey(source.provider, source.sourceId));
    if (!entry) {
      setSearchMessage('This VOD has not been processed yet. Completed broadcasts appear after their round index passes validation.', true);
      return;
    }
    setSearchMessage('');
    const response = await fetch(entry.index, {cache: 'no-store'});
    if (!response.ok) throw new Error('This round index is temporarily unavailable.');
    const index = VodlockSite.validateIndex(await response.json());
    if (index.provider !== entry.provider || index.sourceId !== entry.sourceId) throw new Error('The catalog and round index do not match.');
    setPlaybackControlsDisabled(true);
    state.entry = entry;
    state.index = index;
    destroyPlayer();
    state.started = false;
    resetPlayerHost();
    $('watch-event').textContent = entry.event;
    $('watch-title').textContent = entry.title;
    $('timeline-status').textContent = entry.provider === 'youtube' ? 'Timeline hidden' : 'Twitch source controls';
    $('library-view').hidden = true;
    $('player-view').hidden = false;
    $('start-gate').hidden = false;
    $('end-shield').hidden = true;
    $('start-watching').disabled = false;
    $('start-watching').textContent = 'Start watching';
    if (updateHistory) history.pushState({source: entryKey(entry)}, '', '?v=' + encodeURIComponent(entryKey(entry)));
    document.title = entry.title + ' | VODLOCK';
    window.scrollTo({top: 0});
  }

  function loadYouTubeApi() {
    if (window.YT?.Player) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('YouTube took too long to respond.')), 15000);
      const previous = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        clearTimeout(timeout);
        if (previous) previous();
        resolve();
      };
      if (!document.querySelector('script[src="https://www.youtube.com/iframe_api"]')) {
        const script = document.createElement('script');
        script.src = 'https://www.youtube.com/iframe_api';
        script.onerror = () => reject(new Error('The YouTube player could not be loaded.'));
        document.head.append(script);
      }
    });
  }

  function loadTwitchApi() {
    if (window.Twitch?.Player) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Twitch took too long to respond.')), 15000);
      const existing = document.querySelector('script[src="https://player.twitch.tv/js/embed/v1.js"]');
      const script = existing || document.createElement('script');
      script.addEventListener('load', () => {
        clearTimeout(timeout);
        window.Twitch?.Player ? resolve() : reject(new Error('The Twitch player could not be loaded.'));
      }, {once: true});
      script.addEventListener('error', () => reject(new Error('The Twitch player could not be loaded.')), {once: true});
      if (!existing) {
        script.src = 'https://player.twitch.tv/js/embed/v1.js';
        document.head.append(script);
      }
    });
  }

  function playerReady() {
    state.ready = true;
    setVolume(Number($('volume').value));
    seekPlayer(Math.max(0, state.index.rounds[0].start - state.index.leadSeconds));
    playPlayer();
    setPlaybackControlsDisabled(false);
    updateStatus();
    clearInterval(state.statusTimer);
    state.statusTimer = setInterval(updateStatus, 500);
  }

  function createYouTubePlayer() {
    state.player = new YT.Player('media-player', {
      host: 'https://www.youtube-nocookie.com',
      videoId: state.entry.sourceId,
      playerVars: {autoplay: 0, controls: 0, disablekb: 1, fs: 0, iv_load_policy: 3, playsinline: 1, rel: 0},
      events: {
        onReady: playerReady,
        onStateChange: event => {
          setPlaying(event.data === YT.PlayerState.PLAYING);
          if (event.data === YT.PlayerState.ENDED) setTimeout(finishWatching, 0);
        },
        onError: () => toast('YouTube could not play this broadcast here.')
      }
    });
  }

  function createTwitchPlayer() {
    state.player = new Twitch.Player('media-player', {
      video: 'v' + state.entry.sourceId,
      parent: [location.hostname],
      width: '100%',
      height: '100%',
      autoplay: false
    });
    state.player.addEventListener(Twitch.Player.READY, playerReady);
    state.player.addEventListener(Twitch.Player.PLAY, () => setPlaying(true));
    state.player.addEventListener(Twitch.Player.PAUSE, () => setPlaying(false));
    state.player.addEventListener(Twitch.Player.ENDED, () => setTimeout(finishWatching, 0));
  }

  async function createPlayer() {
    try {
      await (state.entry.provider === 'youtube' ? loadYouTubeApi() : loadTwitchApi());
      if (!state.entry || !state.index) return;
      destroyPlayer();
      resetPlayerHost();
      if (state.entry.provider === 'youtube') createYouTubePlayer();
      else createTwitchPlayer();
    } catch (error) {
      state.started = false;
      $('start-gate').hidden = false;
      $('start-watching').disabled = false;
      $('start-watching').textContent = 'Try again';
      toast(error.message);
    }
  }

  function finishWatching() {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
    destroyPlayer();
    state.started = false;
    setPlaybackControlsDisabled(true);
    resetPlayerHost();
    $('end-shield').hidden = false;
  }

  function currentTime() {
    return state.ready && state.player?.getCurrentTime ? state.player.getCurrentTime() : 0;
  }

  function setPlaying(playing) {
    state.playing = playing;
    $('play-button').textContent = playing ? 'Pause' : 'Play';
    $('play-button').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }

  function playPlayer() {
    if (state.entry.provider === 'youtube') state.player.playVideo();
    else state.player.play();
  }

  function pausePlayer() {
    if (state.entry.provider === 'youtube') state.player.pauseVideo();
    else state.player.pause();
  }

  function seekPlayer(destination) {
    if (state.entry.provider === 'youtube') state.player.seekTo(destination, true);
    else state.player.seek(destination);
  }

  function isMuted() {
    return state.entry.provider === 'youtube' ? state.player.isMuted() : state.player.getMuted();
  }

  function setMuted(muted) {
    if (state.entry.provider === 'youtube') muted ? state.player.mute() : state.player.unMute();
    else state.player.setMuted(muted);
  }

  function setVolume(value) {
    state.player.setVolume(state.entry.provider === 'youtube' ? value : value / 100);
  }

  function setPlaybackControlsDisabled(disabled) {
    for (const identifier of ['play-button', 'back-ten', 'skip-replay', 'mute-button', 'volume',
      'previous-round', 'next-round', 'previous-map', 'next-map', 'restart-round']) $(identifier).disabled = disabled;
  }

  function seek(destination, message) {
    if (!state.ready || destination === null) {
      if (destination === null) toast('No indexed item in that direction.');
      return;
    }
    $('end-shield').hidden = true;
    const resume = state.playing;
    seekPlayer(destination);
    if (resume) setTimeout(() => state.player && playPlayer(), 200);
    updateStatus();
    if (message) toast(message);
  }

  function updateStatus() {
    if (!state.index) return;
    const position = VodlockSite.position(state.index, currentTime());
    const round = state.index.rounds[Math.max(position, 0)];
    if (position < 0) {
      $('current-map').textContent = 'Opening';
      $('current-round').textContent = 'Ready to begin';
    } else {
      $('current-map').textContent = 'Map ' + round.map;
      $('current-round').textContent = 'Round ' + round.round;
    }
  }

  function navigateRound(direction) {
    seek(VodlockSite.roundDestination(state.index, currentTime(), direction), direction > 0 ? 'Next round' : 'Previous round');
  }

  function navigateMap(direction) {
    seek(VodlockSite.mapDestination(state.index, currentTime(), direction), direction > 0 ? 'Next map' : 'Previous map');
  }

  function togglePlayback() {
    if (!state.ready) return;
    if (state.playing) pausePlayer();
    else playPlayer();
  }

  function startWatching() {
    if (state.started) return;
    state.started = true;
    $('start-gate').hidden = true;
    $('start-watching').disabled = true;
    $('start-watching').textContent = 'Loading ' + (state.entry.provider === 'youtube' ? 'YouTube' : 'Twitch') + '...';
    createPlayer();
  }

  function toggleMute() {
    if (!state.ready) return;
    const muted = !isMuted();
    setMuted(muted);
    $('mute-button').textContent = muted ? 'Muted' : 'Sound';
    $('mute-button').setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else $('player-shell').requestFullscreen?.();
  }

  $('video-search').addEventListener('submit', event => {
    event.preventDefault();
    const source = VodlockSite.mediaSource($('video-url').value);
    if (!source) {
      setSearchMessage('Paste a valid public YouTube or Twitch VOD link.', true);
      return;
    }
    openVideo(VodlockSite.sourceKey(source.provider, source.sourceId)).catch(error => setSearchMessage(error.message, true));
  });
  $('home-button').addEventListener('click', () => showLibrary());
  $('back-button').addEventListener('click', () => showLibrary());
  $('return-library').addEventListener('click', () => showLibrary());
  $('start-watching').addEventListener('click', startWatching);
  $('play-button').addEventListener('click', togglePlayback);
  $('back-ten').addEventListener('click', () => seek(Math.max(0, currentTime() - 10)));
  $('skip-replay').addEventListener('click', () => seek(currentTime() + 10, 'Skipped 10 seconds'));
  $('mute-button').addEventListener('click', toggleMute);
  $('volume').addEventListener('input', event => {
    if (!state.ready) return;
    setVolume(Number(event.target.value));
    if (Number(event.target.value)) setMuted(false);
  });
  $('fullscreen-button').addEventListener('click', toggleFullscreen);
  $('previous-round').addEventListener('click', () => navigateRound(-1));
  $('next-round').addEventListener('click', () => navigateRound(1));
  $('previous-map').addEventListener('click', () => navigateMap(-1));
  $('next-map').addEventListener('click', () => navigateMap(1));
  $('restart-round').addEventListener('click', () => {
    const position = VodlockSite.position(state.index, currentTime());
    const round = state.index.rounds[Math.max(0, position)];
    seek(Math.max(0, round.start - state.index.leadSeconds), 'Round restarted');
  });

  document.addEventListener('keydown', event => {
    if ($('player-view').hidden || ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
    if (!state.started) {
      if (event.key === ' ') {
        event.preventDefault();
        startWatching();
      }
      return;
    }
    const actions = {
      ' ': togglePlayback,
      ArrowUp: () => navigateRound(-1),
      ArrowDown: () => navigateRound(1),
      ArrowLeft: () => seek(Math.max(0, currentTime() - 10)),
      ArrowRight: () => seek(currentTime() + 10, 'Skipped 10 seconds'),
      f: toggleFullscreen,
      F: toggleFullscreen,
      m: toggleMute,
      M: toggleMute
    };
    if (!actions[event.key]) return;
    event.preventDefault();
    actions[event.key]();
  });

  addEventListener('popstate', () => {
    const source = new URL(location.href).searchParams.get('v');
    if (source) openVideo(source, false).catch(error => toast(error.message));
    else showLibrary(false);
  });

  loadCatalog().then(() => {
    const source = VodlockSite.mediaSource(new URL(location.href).searchParams.get('v'));
    if (source) return openVideo(VodlockSite.sourceKey(source.provider, source.sourceId), false);
  }).catch(error => {
    $('catalog').replaceChildren();
    setSearchMessage(error.message, true);
  });
})();
