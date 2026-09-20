// Tabler's own script opens and closes the modals; what is left here is the panel's own behaviour.
for (const button of document.querySelectorAll('[data-concern-toggle]')) {
  button.setAttribute('aria-controls', button.dataset.concernToggle);
  button.addEventListener('click', () => {
    const panel = document.getElementById(button.dataset.concernToggle);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    button.setAttribute('aria-expanded', String(!panel.hidden));
  });
}
for (const element of document.querySelectorAll('time[data-timestamp]')) {
  const date = new Date(Number(element.dataset.timestamp) * 1000);
  if (!Number.isNaN(date.getTime())) {
    element.dateTime = date.toISOString();
    element.textContent = new Intl.DateTimeFormat(undefined, {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}).format(date);
  }
}
// Keep scheduled state fresh without discarding an open modal, an expanded section or an editor in use.
if (document.querySelector('.site-overview')) {
  window.setInterval(() => {
    if (!document.hidden && !document.querySelector('.modal.show, details[open], form:focus-within, [data-concern-panel]:not([hidden])')) window.location.reload();
  }, 30000);
}
// A text the operator is meant to copy selects itself when clicked. This lives here rather than in an
// onclick attribute, which the panel's content security policy blocks.
for (const box of document.querySelectorAll('[data-select-all]')) {
  box.addEventListener('click', () => box.select());
}
// A form that says what it is about to do asks once before doing it.
for (const form of document.querySelectorAll('form[data-confirm]')) {
  form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
}
