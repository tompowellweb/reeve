const operations = document.querySelectorAll('#operation, #domain-operation, #runtime-operation, #catalogue-operation, #database-operation, #database-catalogue-operation, #backup-operation');
if ([...operations].some(operation => ['queued', 'running'].includes(operation.dataset.state))) {
  const refreshOperation = async () => {
    if (document.querySelector('dialog[open], [data-concern-panel]:not([hidden])')) { window.setTimeout(refreshOperation, 2500); return; }
    try {
      const response = await fetch('/api/sites', {credentials: 'same-origin'});
      if (response.ok && !document.querySelector('dialog[open], [data-concern-panel]:not([hidden])')) window.location.reload();
    } catch (_) { /* Retry below without interrupting an open editor. */ }
    window.setTimeout(refreshOperation, 5000);
  };
  window.setTimeout(refreshOperation, 2500);
}

const runtime = document.getElementById('runtime');
if (runtime) {
  const updateBranch = () => {
    const label = document.getElementById('php-branch');
    label.hidden = runtime.value !== 'php';
    label.querySelector('select').disabled = runtime.value !== 'php';
  };
  runtime.addEventListener('change', updateBranch);
  updateBranch();
}

const dbEngine = document.getElementById('db-engine');
if (dbEngine) {
  const updateDatabase = () => {
    document.getElementById('db-fields').hidden = !dbEngine.value;
    document.querySelectorAll('[data-db-engine]').forEach(group => {
      group.hidden = group.dataset.dbEngine !== dbEngine.value;
      group.querySelector('select').disabled = group.hidden;
    });
  };
  dbEngine.addEventListener('change', updateDatabase);
  updateDatabase();
}
