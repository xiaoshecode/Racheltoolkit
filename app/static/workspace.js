/* Drafts save on the server; local edits only get a leave-page reminder. */
(() => {
  const forms = [...document.querySelectorAll('form[data-unsaved-form]')];
  if (!forms.length) return;
  // Initialize structured fields before recording the baseline, in the same
  // format their editor uses. Loading existing annotations is not a user edit.
  for (const form of forms) {
    for (const field of form.querySelectorAll('[data-initial-json]')) {
      const source = document.getElementById(field.dataset.initialJson);
      if (source) field.value = field.defaultValue = JSON.stringify(JSON.parse(source.textContent));
    }
  }
  const snapshot = form => JSON.stringify([...new FormData(form)].map(([key, value]) =>
    [key, value instanceof File ? (value.name ? [value.name, value.size, value.lastModified] : null) : value]));
  const originals = new Map(forms.map(form => [form, snapshot(form)]));
  let leaving = false;
  const changed = form => snapshot(form) !== originals.get(form);
  for (const form of forms) {
    const message = document.createElement('p');
    message.className = 'unsaved-indicator';
    message.setAttribute('role', 'status');
    message.textContent = '有未保存的修改。请保存本区内容后再离开。';
    form.prepend(message);
    const refresh = () => form.classList.toggle('is-dirty', changed(form));
    form.addEventListener('input', refresh);
    form.addEventListener('change', refresh);
    // Native reset restores controls after the reset event's microtasks.
    form.addEventListener('reset', () => setTimeout(refresh, 0));
  }
  document.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    if (forms.some(form => form !== event.target && changed(form)) &&
        !window.confirm('其他编辑区还有未保存的修改，继续操作会丢弃这些修改。仍然继续？')) {
      event.preventDefault();
      return;
    }
    // Preserve submitter values, including action=save and action=publish.
    leaving = true;
    setTimeout(() => { leaving = false; }, 1000);
  });
  window.addEventListener('beforeunload', event => {
    if (!leaving && forms.some(changed)) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
})();
