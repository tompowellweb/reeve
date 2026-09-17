const form = document.getElementById('application-upload');
if (form) form.addEventListener('submit', async event => {
  event.preventDefault();
  const status = form.querySelector('[role=status]');
  const button = form.querySelector('button');
  const file = form.elements.file.files[0];
  if (!file) return;
  if (file.size > 512 * 1048576) { status.textContent = 'The upload limit is 512 MiB.'; return; }
  button.disabled = true; status.textContent = 'Uploading and checking the project…';
  const params = new URLSearchParams();
  for (const key of ['name', 'domain', 'service', 'port']) params.set(key, form.elements[key].value);
  try {
    const response = await fetch('/api/v1/imports?' + params, {method: 'POST', body: file,
      headers: {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': form.dataset.csrf, 'Idempotency-Key': crypto.randomUUID()}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Upload failed.');
    window.location.href = result.url;
  } catch (error) { status.textContent = error.message; button.disabled = false; }
});
