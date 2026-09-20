const operations = document.querySelectorAll('#operation, #domain-operation, #runtime-operation, #catalogue-operation, #rebuild-operation, #database-operation, #database-catalogue-operation, #backup-operation, #site-backup-operation, #restore-operation, #recovery-scan, #progress, #actions');
if ([...operations].some(operation => ['queued', 'running'].includes(operation.dataset.state))) {
  const refreshOperation = async () => {
    if (document.querySelector('.modal.show, [data-concern-panel]:not([hidden])')) { window.setTimeout(refreshOperation, 2500); return; }
    try {
      const response = await fetch('/api/sites', {credentials: 'same-origin'});
      if (response.ok && !document.querySelector('.modal.show, [data-concern-panel]:not([hidden])')) window.location.reload();
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

document.querySelectorAll('[data-recover-row]').forEach(row => {
  const from = row.querySelector('[data-from]'); const as = row.querySelector('[data-as]'); const fields = row.querySelector('[data-new-site]');
  const update = () => {
    const dump = from && from.selectedOptions[0] && from.selectedOptions[0].dataset.kind === 'dump';
    if (as) {
      [...as.options].forEach(option => { option.disabled = dump ? option.value !== 'database' : false; option.hidden = option.disabled; });
      if (dump) as.value = 'database';
    }
    if (fields) fields.hidden = !(as ? as.value === 'new' : true) || dump;
  };
  if (from) from.addEventListener('change', update);
  if (as) as.addEventListener('change', update);
  update();
});
