"""Server settings as the panel reads, merges, validates and writes them."""
import pytest
import yaml

from reeve import settings as st


def test_read_reports_every_group_with_defaults_and_choices():
    groups = st.read({})
    assert groups['certificates'] == {'mode': 'internal', 'email': '', 'modes': ['internal', 'public']}
    assert groups['mail']['mode'] == 'direct' and groups['mail']['rate_per_hour'] == 100 and groups['mail']['modes'] == ['off', 'direct', 'relay', 'sink']
    assert groups['profile']['name'] == 'standard' and set(groups['profile']['profiles']) == {'small', 'standard', 'large'}
    assert groups['backups'] == {'local_path': '/srv/backups', 'hour': 3, 'database_days': 2, 'local_daily': 2, 'local_weekly': 0, 'local_monthly': 0, 'remote_daily': 7, 'remote_weekly': 4, 'remote_monthly': 12}
    assert groups['updates'] == {'hour': 4, 'every_days': 7}


def test_merge_applies_one_group_keeps_the_rest_and_refuses_what_the_readers_refuse():
    loaded = {'schema': 1, 'profile': 'small', 'nginx_image': 'nginx@sha256:x', 'mail': {'mode': 'sink', 'sink_image': 'mailpit@sha256:y'}}
    new = st.merge(loaded, 'certificates', {'mode': 'public', 'email': ' ops@example.com '})
    assert new['tls'] == {'mode': 'public', 'email': 'ops@example.com'} and new['profile'] == 'small' and new['nginx_image'] == 'nginx@sha256:x'
    assert loaded.get('tls') is None  # the input is untouched
    with pytest.raises(ValueError, match='tls.email'): st.merge(loaded, 'certificates', {'mode': 'public', 'email': 'not-an-address'})
    with pytest.raises(ValueError, match='tls.mode'): st.merge(loaded, 'certificates', {'mode': 'sideways'})
    new = st.merge(loaded, 'mail', {'mode': 'relay', 'relayhost': '[smtp.example.net]:587', 'hostname': 'Server.Example.COM', 'public_ip': '203.0.113.5', 'rate_per_hour': '50'})
    assert new['mail'] == {'mode': 'relay', 'relayhost': '[smtp.example.net]:587', 'hostname': 'server.example.com', 'public_ip': '203.0.113.5', 'rate_per_hour': 50, 'sink_image': 'mailpit@sha256:y'}
    with pytest.raises(ValueError, match='whole number'): st.merge(loaded, 'mail', {'mode': 'direct', 'rate_per_hour': 'lots'})
    with pytest.raises(ValueError, match='relayhost'): st.merge(loaded, 'mail', {'mode': 'relay', 'relayhost': 'bad host!', 'rate_per_hour': '1'})
    with pytest.raises(ValueError, match='profile'): st.merge(loaded, 'profile', {'name': 'huge'})
    new = st.merge(loaded, 'backups', {'local_path': '/data/backups', 'hour': '2', 'database_days': '3', 'local_daily': '3', 'local_weekly': '12', 'local_monthly': '62', 'remote_daily': '14', 'remote_weekly': '0', 'remote_monthly': '0'})
    assert new['backups'] == {'local_path': '/data/backups'} and new['site_backups'] == {'hour': 2}
    assert new['retention'] == {'database_days': 3, 'local': {'daily': 3, 'weekly': 12, 'monthly': 62}, 'remote': {'daily': 14, 'weekly': 0, 'monthly': 0}}
    with pytest.raises(ValueError, match='at least one day'): st.merge(loaded, 'backups', {'hour': '2', 'database_days': '0', 'local_daily': '1'})
    with pytest.raises(ValueError, match='at least one daily backup here'): st.merge(loaded, 'backups', {'hour': '2', 'database_days': '2', 'local_daily': '0'})
    with pytest.raises(ValueError, match='updates.every_days'): st.merge(loaded, 'updates', {'hour': '4', 'every_days': '400'})
    with pytest.raises(ValueError, match='Unknown settings group'): st.merge(loaded, 'firewall', {})


def test_write_and_restore_go_through_the_file_atomically(tmp_path, monkeypatch):
    import reeve.host as hm
    ops = tmp_path / 'ops'; ops.mkdir(); (ops / 'server.yaml').write_text('schema: 1\nprofile: small\n')
    monkeypatch.setattr(st, 'OPS', ops); monkeypatch.setattr(hm, 'OPS', ops)
    for module in ('mail', 'profile', 'retention', 'site_backup', 'php_updates'):
        monkeypatch.setattr(__import__('reeve.' + module, fromlist=['OPS']), 'OPS', ops)
    monkeypatch.setattr(st, 'trusted', lambda *a, **k: None); monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    new = st.write('updates', {'hour': '5', 'every_days': '14'})
    assert yaml.safe_load((ops / 'server.yaml').read_text()) == {'schema': 1, 'profile': 'small', 'updates': {'hour': 5, 'every_days': 14}} == new
    with pytest.raises(ValueError): st.write('updates', {'hour': '25', 'every_days': '14'})
    assert yaml.safe_load((ops / 'server.yaml').read_text())['updates'] == {'hour': 5, 'every_days': 14}  # nothing changed
    applied = []
    monkeypatch.setattr(st, 'apply', lambda host, ledger, group, before, after: applied.append(group) or group + ' done')
    result = st.restore(None, None, {'schema': 9, 'profile': 'large', 'tls': {'mode': 'public', 'email': 'a@b.example'}, 'updates': {'hour': 1, 'every_days': 7}, 'future_key': {'x': 1}})
    document = yaml.safe_load((ops / 'server.yaml').read_text())
    assert document['schema'] == 1 and document['profile'] == 'large' and document['tls']['mode'] == 'public' and document['future_key'] == {'x': 1}
    assert result['applied'] == ['future_key', 'profile', 'tls', 'updates'] and applied == ['certificates', 'mail']
