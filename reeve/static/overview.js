for (const button of document.querySelectorAll('[data-dialog]')) {
  button.setAttribute('aria-controls', button.dataset.dialog);
  button.addEventListener('click', () => document.getElementById(button.dataset.dialog)?.showModal());
}
for (const button of document.querySelectorAll('[data-close-dialog]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}
for (const button of document.querySelectorAll('[data-concern-toggle]')) {
  button.setAttribute('aria-controls', button.dataset.concernToggle);
  button.addEventListener('click', () => {
    const panel = document.getElementById(button.dataset.concernToggle);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    button.setAttribute('aria-expanded', String(!panel.hidden));
  });
}
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('click', event => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
}
for (const element of document.querySelectorAll('time[data-timestamp]')) {
  const date = new Date(Number(element.dataset.timestamp) * 1000);
  if (!Number.isNaN(date.getTime())) {
    element.dateTime = date.toISOString();
    element.textContent = new Intl.DateTimeFormat(undefined, {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}).format(date);
  }
}
// Keep scheduled state fresh without discarding an open editor or expanded details.
if (document.querySelector('.site-overview')) {
  window.setInterval(() => {
    if (!document.hidden && !document.querySelector('dialog[open], .modal.show, details[open], form:focus-within, [data-concern-panel]:not([hidden])')) window.location.reload();
  }, 30000);
}
// A form that says what it is about to do asks once before doing it.
for (const form of document.querySelectorAll('form[data-confirm]')) {
  form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
}
