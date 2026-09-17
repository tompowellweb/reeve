import json
import os
import time

from reeve import housekeeping as hk, host as hm


def test_prune_removes_aged_entries_keeps_protected_names_and_records_the_run(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; (ops / 'panel/worker').mkdir(parents=True)
    for module in (hk, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(hk, 'OPS', ops); monkeypatch.setattr(hk, 'STATE', ops / 'panel/worker/housekeeping.json')
    monkeypatch.setattr(hk, 'RULES', [(ops / 'panel/worker/content-output', 90 * 86400, (), False), (ops / 'panel/worker/php-rebuilds', 365 * 86400, ('latest.json', 'schedule.json'), False), (ops / 'panel/web/downloads', 6 * 3600, (), True)])
    monkeypatch.setattr(hk, 'SCRATCH', [ops / 'backups/staging/restore-fetch'])
    now = time.time()
    out = ops / 'panel/worker/content-output'; out.mkdir()
    (out / 'old.txt').write_text('x'); os.utime(out / 'old.txt', (now - 100 * 86400,) * 2)
    (out / 'new.txt').write_text('x')
    reports = ops / 'panel/worker/php-rebuilds'; reports.mkdir()
    for name in ('latest.json', 'schedule.json', 'ancient.json'):
        (reports / name).write_text('{}'); os.utime(reports / name, (now - 400 * 86400,) * 2)
    import uuid
    stale_token, fresh_token = str(uuid.uuid4()), str(uuid.uuid4())
    downloads = ops / 'panel/web/downloads'; (downloads / stale_token).mkdir(parents=True); (downloads / stale_token / 'f').write_text('x'); os.utime(downloads / stale_token, (now - 7 * 3600,) * 2)
    (downloads / fresh_token).mkdir(); (downloads / 'not-a-token').mkdir(); os.utime(downloads / 'not-a-token', (now - 7 * 3600,) * 2)
    scratch = ops / 'backups/staging/restore-fetch'; scratch.mkdir(parents=True); os.utime(scratch, (now - 2 * 86400,) * 2)
    logged = []
    report = hk.prune(now, log=logged.append)
    assert not (out / 'old.txt').exists() and (out / 'new.txt').exists()
    assert (reports / 'latest.json').exists() and (reports / 'schedule.json').exists() and not (reports / 'ancient.json').exists()
    assert not (downloads / stale_token).exists() and (downloads / fresh_token).exists() and (downloads / 'not-a-token').exists() and not scratch.exists()
    assert report[str(out)] == 1 and report[str(downloads)] == 1 and logged and 'removed' in logged[0]
    assert not hk.due(now) and hk.due(now + 86401) and json.loads(hk.STATE.read_text())['last_run'] == now
