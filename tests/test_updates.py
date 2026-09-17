import json

import pytest

from reeve import host as hm, updates as up

LISTING = """1111111111111111111111111111111111111111\trefs/tags/v1.0.0
2222222222222222222222222222222222222222\trefs/tags/v1.0.0^{}
3333333333333333333333333333333333333333\trefs/tags/v1.2.0
4444444444444444444444444444444444444444\trefs/tags/v1.10.0
5555555555555555555555555555555555555555\trefs/tags/experiment
6666666666666666666666666666666666666666\trefs/tags/v2.0.0-rc1
"""


def test_versions_parse_and_the_newest_tag_wins_numerically():
    assert up.parse('1.2.3') == (1, 2, 3) and up.parse('v1.10.0') == (1, 10, 0) and up.parse('v2.0.0-rc1') is None and up.parse(None) is None
    assert up.newest(LISTING) == '1.10.0' and up.newest('') is None


@pytest.fixture
def box(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; (ops / 'panel/worker').mkdir(parents=True)
    for module in (up, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(up, 'OPS', ops); monkeypatch.setattr(up, 'RECORD', ops / 'panel/release.json'); monkeypatch.setattr(up, 'STATE', ops / 'panel/worker/update-check.json')
    calls = []
    monkeypatch.setattr(up, 'command', lambda args, timeout=120: calls.append(args) or LISTING)
    return ops, calls


def test_check_compares_the_newest_tag_with_the_installed_version_and_is_daily(box):
    ops, calls = box
    assert up.due(1000.0) is True
    (ops / 'panel/release.json').write_text(json.dumps({'schema': 2, 'version': '1.2.0', 'current': 'abc', 'source': 'https://example.net/reeve.git'}))
    state = up.check(now=1000.0)
    assert state == {'checked_at': 1000.0, 'latest': '1.10.0', 'installed': '1.2.0', 'source': 'https://example.net/reeve.git', 'available': True, 'error': ''}
    assert calls[-1] == ['git', 'ls-remote', '--tags', 'https://example.net/reeve.git']
    assert up.due(1000.0 + 3600) is False and up.due(1000.0 + 86400) is True
    assert up.state()['latest'] == '1.10.0'
    (ops / 'panel/release.json').write_text(json.dumps({'schema': 2, 'version': '1.10.0', 'current': 'abc'}))
    assert up.check(now=2000.0)['available'] is False and up.source() == up.DEFAULT_SOURCE
    # An old record without a version counts as older than any release; a failed listing is recorded, not raised.
    (ops / 'panel/release.json').write_text(json.dumps({'schema': 1, 'current': 'abc'}))
    assert up.check(now=3000.0)['available'] is True
    def failing(args, timeout=120): raise RuntimeError('no route to host')
    import reeve.updates as module
    module.command = failing
    state = up.check(now=4000.0)
    assert state['latest'] is None and state['available'] is False and 'no route' in state['error']


def test_status_judges_availability_against_what_runs_now(box):
    ops, calls = box
    (ops / 'panel/release.json').write_text(json.dumps({'schema': 2, 'version': '1.1.1', 'current': 'abc'}))
    up.check(now=1000.0)
    assert up.status()['available'] is True and up.status()['latest'] == '1.10.0'
    (ops / 'panel/release.json').write_text(json.dumps({'schema': 2, 'version': '1.10.0', 'current': 'def', 'previous_version': '1.1.1'}))
    result = up.status()  # updated since the check: the stale check must not say "available"
    assert result['available'] is False and result['version'] == '1.10.0' and result['previous_version'] == '1.1.1' and result['checked_at'] == 1000.0


def test_apply_fetches_the_tag_and_runs_the_installer(box, monkeypatch, tmp_path):
    ops, calls = box
    monkeypatch.setattr(up, 'SRC', tmp_path / 'src')
    ran = []
    monkeypatch.setattr(up.subprocess, 'run', lambda args, cwd=None: ran.append((args, cwd)) or type('R', (), {'returncode': 0})())
    logged = []
    assert up.apply(log=logged.append) == {'installed': '1.10.0', 'from': up.DEFAULT_SOURCE}
    assert calls[0][:2] == ['git', 'clone'] and calls[1][:3] == ['git', '-C', str(tmp_path / 'src')] and calls[-1][-1] == 'v1.10.0'
    assert ran[0][0][1] == str(tmp_path / 'src/install.py') and ran[0][1] == str(tmp_path / 'src')
    assert up.apply('1.2.0', log=logged.append)['installed'] == '1.2.0' and calls[-1][-1] == 'v1.2.0'
    with pytest.raises(ValueError): up.apply('nonsense')
    monkeypatch.setattr(up.subprocess, 'run', lambda args, cwd=None: type('R', (), {'returncode': 1})())
    with pytest.raises(RuntimeError): up.apply()
