(() => {
  'use strict';

  const defaults = {
    enabled: true,
    hideTimeline: true,
    hideMetadata: true,
    hideComments: true,
    hideThumbnails: true,
    endScreenProtection: true
  };
  let settings = { ...defaults };
  let queued = false;
  const root = document.documentElement;
  const isSpace = /^(www\.)?vods\.space$/.test(location.hostname);

  function mark(element, name) {
    if (element && !element.classList.contains(name)) element.classList.add(name);
  }

  function getSelection(section, type) {
    const buttons = [...section.querySelectorAll(type === 'round' ? 'button[title^="Round "]' : 'button')]
      .filter(button => !button.closest('[data-vodlock-nav]'));
    const index = buttons.findIndex(button => button.classList.contains('bg-brand'));
    return { buttons, index };
  }

  function navigate(section, type, direction) {
    const { buttons, index } = getSelection(section, type);
    if (index < 0) return;
    buttons[index + direction]?.click();
  }

  function protectNavigation(section, type) {
    if (!section) return;
    mark(section, 'vodlock-space-navigation');
    let nav = section.querySelector('[data-vodlock-nav]');
    if (!nav) {
      nav = document.createElement('div');
      nav.dataset.vodlockNav = type;
      nav.setAttribute('role', 'group');
      nav.setAttribute('aria-label', `Spoiler-safe ${type} navigation`);
      for (const direction of [-1, 1]) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = `${direction < 0 ? '← Previous' : 'Next'} ${type}${direction > 0 ? ' →' : ''}`;
        button.addEventListener('click', () => navigate(section, type, direction));
        nav.append(button);
        if (direction < 0) nav.append(document.createElement('span'));
      }
      section.append(nav);
    }
    const { buttons, index } = getSelection(section, type);
    const label = type === 'round'
      ? buttons[index]?.getAttribute('title')
      : buttons[index]?.textContent.trim();
    const current = nav.querySelector('span');
    const text = label || `Current ${type}`;
    if (current.textContent !== text) current.textContent = text;
  }

  function refresh() {
    queued = false;
    root.classList.toggle('vodlock-space', isSpace);
    for (const key of ['enabled', 'hideTimeline', 'hideMetadata', 'hideComments', 'hideThumbnails', 'endScreenProtection']) {
      const className = 'vodlock-' + key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());
      const enabled = settings.enabled && settings[key];
      if (root.classList.contains(className) !== enabled) root.classList.toggle(className, enabled);
    }
    if (isSpace) {
      for (const heading of document.querySelectorAll('h2')) {
        const text = heading.textContent.trim();
        if (text === 'Round index') protectNavigation(heading.closest('section'), 'round');
        if (text === 'Select map') protectNavigation(heading.closest('section'), 'map');
      }
      mark(document.getElementById('watch-log-entry')?.closest('aside'), 'vodlock-space-notes');
      const title = 'VODLOCK · vods.space';
      if (settings.enabled && settings.hideMetadata && document.title !== title) document.title = title;
    }
    mark(root, 'vodlock-space-ready');
  }

  function scheduleRefresh() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(refresh);
  }

  refresh();
  chrome.storage.sync.get(defaults).then(value => {
    settings = value;
    refresh();
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync') return;
    for (const key of Object.keys(defaults)) {
      if (changes[key]) settings[key] = changes[key].newValue ?? defaults[key];
    }
    refresh();
  });
  new MutationObserver(records => {
    if (records.some(record => record.target !== root &&
        !record.target.parentElement?.closest('[data-vodlock-nav]') &&
        !record.target.closest?.('[data-vodlock-nav]'))) scheduleRefresh();
  }).observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'], characterData: true });
})();
