(() => {
  const matrix = document.querySelector('.matrix[data-restore-key]');
  if (!matrix) return;
  const key = `racheltoolkit:gradebook:${matrix.dataset.restoreKey}`;
  // Keep return navigation state in this tab, never scores or feedback.
  matrix.querySelectorAll('[data-student-link]').forEach(link => {
    let pointerLeft;
    // Focusing a link inside the sticky column can scroll its ancestor before click.
    link.addEventListener('pointerdown', () => { pointerLeft = matrix.scrollLeft; });
    link.addEventListener('pointercancel', () => { pointerLeft = undefined; });
    link.addEventListener('click', event => {
      const left = event.detail > 0 && Number.isFinite(pointerLeft) ? pointerLeft : matrix.scrollLeft;
      pointerLeft = undefined;
      try {
        sessionStorage.setItem(key, JSON.stringify({left, anchor: link.dataset.studentLink}));
      } catch (_) { /* Returning by anchor still works when storage is unavailable. */ }
    });
  });
  if (!location.hash.startsWith('#student-')) return;
  try {
    const position = JSON.parse(sessionStorage.getItem(key));
    if (!position || `#${position.anchor}` !== location.hash || !Number.isFinite(position.left)) return;
    const restore = () => requestAnimationFrame(() => {
      const link = document.getElementById(position.anchor)?.querySelector('[data-student-link]');
      link?.focus({preventScroll: true});
      matrix.scrollLeft = position.left;
    });
    // Restore after native fragment navigation, which may run after a deferred script.
    if (document.readyState === 'complete') restore();
    else window.addEventListener('pageshow', restore, {once: true});
  } catch (_) { /* Preserve the browser's native anchor navigation. */ }
})();
