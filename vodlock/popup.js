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

const ids = Object.keys(defaults);

async function init() {
  document.getElementById('roundIndexFile').addEventListener('change', async event => {
    const status = document.getElementById('indexImportStatus');
    try {
      const file = event.target.files[0];
      if (!file) return;
      if (file.size > 1000000) throw new Error('Index files must be smaller than 1 MB.');
      const index = VodlockIndex.validate(JSON.parse(await file.text()));
      await chrome.storage.local.set({['roundIndex:' + index.videoId]: index});
      status.textContent = 'Index saved. Open the matching YouTube video to use it.';
    } catch (error) {
      status.textContent = 'Import failed: ' + error.message;
    }
  });
  const settings = await chrome.storage.sync.get(defaults);
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (id === 'replaySkipSeconds') {
      el.value = settings[id];
      el.addEventListener('change', () => {
        const seconds = Number(el.value);
        if (!Number.isFinite(seconds) || seconds < 5 || seconds > 60) {
          el.value = settings[id];
          return;
        }
        settings[id] = Math.round(seconds);
        el.value = settings[id];
        chrome.storage.sync.set({ [id]: settings[id] });
      });
      continue;
    }
    el.checked = Boolean(settings[id]);
    el.addEventListener('change', () => {
      chrome.storage.sync.set({ [id]: el.checked });
    });
  }
}

init();
