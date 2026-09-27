(() => {
  'use strict';

  if (location.origin !== 'http://127.0.0.1:8766') return;
  window.addEventListener('message', async event => {
    if (event.source !== window || event.origin !== location.origin) return;
    if (event.data?.type === 'VODLOCK_INDEX_PING') {
      window.postMessage({type: 'VODLOCK_INDEX_READY'}, location.origin);
    }
    if (event.data?.type === 'VODLOCK_SAVE_INDEX') {
      try {
        const index = VodlockIndex.validate(event.data.index);
        await chrome.storage.local.set({['roundIndex:' + index.videoId]: index});
        window.postMessage({type: 'VODLOCK_INDEX_SAVED', requestId: event.data.requestId}, location.origin);
      } catch (error) {
        window.postMessage({type: 'VODLOCK_INDEX_SAVED', requestId: event.data.requestId, error: error.message}, location.origin);
      }
    }
  });
})();
