const jobCard = document.getElementById('content-jobs');
if (jobCard?.dataset.pending === 'yes' && !document.querySelector('form[action*="/resolve/"]')) {
  const check = async () => {
    try {
      const response = await fetch(jobCard.dataset.statusUrl);
      if (response.ok) {
        const jobs = await response.json();
        if (!jobs.some(job => ['queued', 'running'].includes(job.state)) && !document.querySelector('dialog[open], .modal.show, [data-concern-panel]:not([hidden])')) { window.location.reload(); return; }
      }
    } catch (_) { /* Retry after a temporary panel interruption. */ }
    window.setTimeout(check, 3000);
  };
  window.setTimeout(check, 3000);
}
for (const form of document.querySelectorAll('.content-upload')) {
  form.addEventListener('submit', event => {
    event.preventDefault();
    const status = form.querySelector('.upload-status');
    const data = new FormData(form);
    const body = data.get('kind') === 'edit' ? new Blob([data.get('text')], {type: 'application/octet-stream'}) : data.get('file');
    if (!body || body.size > 512 * 1048576) { status.textContent = 'Choose a file up to 512 MiB.'; return; }
    const params = new URLSearchParams({id: crypto.randomUUID()});
    for (const key of ['kind', 'path', 'replace', 'expected', 'confirm', 'return_to']) if (data.has(key)) params.set(key, data.get(key));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', form.dataset.url + '?' + params.toString());
    xhr.setRequestHeader('X-CSRF-Token', form.dataset.csrf);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    const button = form.querySelector('button'); button.disabled = true;
    xhr.upload.onprogress = event => { status.textContent = event.lengthComputable ? 'Uploading ' + Math.round(event.loaded / event.total * 100) + '%' : 'Uploading…'; };
    xhr.onload = () => {
      button.disabled = false;
      let result;
      try { result = JSON.parse(xhr.responseText); } catch (_) { status.textContent = 'Upload response unavailable; check Recent operations before uploading again.'; return; }
      if (xhr.status >= 200 && xhr.status < 300) window.location.assign(result.url);
      else status.textContent = result.detail || 'Upload failed.';
    };
    xhr.onerror = () => { button.disabled = false; status.textContent = 'Connection lost. Check Recent operations before uploading again.'; };
    status.textContent = 'Uploading…'; xhr.send(body);
  });
}
