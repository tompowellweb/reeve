import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reeve import php_settings as ps, host as hm
from reeve.content_jobs import validate_content
from reeve.php_site import nginx
from reeve.requests_site import render


GOOD = {'max_execution_time': '120', 'upload_max_filesize_mb': '64', 'post_max_size_mb': '72', 'max_input_vars': '3000', 'memory_limit_mb': '512'}
WANT = {'max_execution_time': 120, 'upload_max_filesize_mb': 64, 'post_max_size_mb': 72, 'max_input_vars': 3000, 'memory_limit_mb': 512}


def test_validation_bounds_couples_post_to_upload_and_keeps_memory_inside_the_container():
    assert validate_content('php-settings', GOOD) == WANT and ps.validate(WANT) == WANT
    for bad in ({**GOOD, 'post_max_size_mb': '32'}, {**GOOD, 'max_execution_time': '0'}, {**GOOD, 'max_input_vars': '99'},
                {**GOOD, 'memory_limit_mb': '8192'}, {**GOOD, 'upload_max_filesize_mb': '12.5'}, {k: v for k, v in GOOD.items() if k != 'max_input_vars'}, 'x'):
        with pytest.raises(ValueError): ps.validate(bad)
    with pytest.raises(ValueError, match='PHP memory of 224'): ps.validate(WANT, {'memory_mb': 256})
    assert ps.validate({**WANT, 'memory_limit_mb': 224}, {'memory_mb': 256})['memory_limit_mb'] == 224


def test_defaults_follow_the_memory_budget_and_render_both_files_consistently():
    assert ps.defaults({}) == {'max_execution_time': 120, 'upload_max_filesize_mb': 128, 'post_max_size_mb': 136, 'max_input_vars': 3000, 'memory_limit_mb': 512}
    assert ps.defaults({'memory_mb': 512})['memory_limit_mb'] == 144 and ps.defaults({'memory_mb': 4096})['memory_limit_mb'] == 502
    assert ps.effective({'php_settings': {'max_execution_time': 300}}, {}) == {**ps.defaults({}), 'max_execution_time': 300}
    ini = ps.render_ini(WANT)
    assert 'memory_limit=512M\n' in ini and 'upload_max_filesize=64M\n' in ini and 'post_max_size=72M\n' in ini
    assert 'max_execution_time=120\n' in ini and 'max_input_vars=3000\n' in ini and 'expose_php=Off' in ini and 'sendmail_path=' in ini
    plain = nginx(hm.NGINX, WANT)
    assert 'client_max_body_size 72m;' in plain and 'fastcgi_read_timeout 130s;' in plain
    assert 'client_max_body_size 136m;' in nginx(hm.NGINX) and 'fastcgi_read_timeout 130s;' in nginx(hm.NGINX)
    profiled = render('10.240.7.0/24', 'wordpress', WANT)
    assert profiled.count('client_max_body_size') == 1 and 'client_max_body_size 72m;' in profiled and 'fastcgi_read_timeout 130s;' in profiled


@pytest.fixture
def site(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; root = sites / 'shop'; (root / 'conf').mkdir(parents=True); (root / 'html').mkdir()
    for module in (ps, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(ps, 'SITES', sites); monkeypatch.setattr(ps, 'SAVED', tmp_path / 'saved')
    monkeypatch.setattr('reeve.content_site.OUTPUT', tmp_path / 'output')
    row = {'id': str(uuid.uuid4()), 'name': 'shop', 'payload': json.dumps({'runtime': 'php', 'php_version': '8.3', 'memory_mb': 1024})}
    (root / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'php', 'operation_id': row['id'], 'domain': 'shop.example.com', 'aliases': ['www.shop.example.com'],
                                                       'web_settings': {'version': 1, 'profile': 'wordpress', 'trusted_ingress': '10.240.7.0/24'}}))
    (root / 'compose.yml').write_text(yaml.safe_dump({'services': {'web': {'image': 'nginx@sha256:abc', 'user': '30001:30001', 'container_name': 'hosting-site-shop', 'pids_limit': -1},
                                                                    'php': {'image': 'hosting-php-site:x', 'container_name': 'hosting-php-shop', 'pids_limit': -1}}}))
    defaults = ps.defaults(json.loads(row['payload']))
    (root / 'conf/nginx.conf').write_text(render('10.240.7.0/24', 'wordpress', defaults))
    (root / 'conf/php.ini').write_text(ps.render_ini(defaults))
    (root / 'conf/site.nginx.conf').write_text('# none\n')
    calls = []
    monkeypatch.setattr(ps, 'command', lambda args, timeout=120: calls.append(args))
    verified = []
    host = SimpleNamespace(verify_domains=lambda names: verified.append(names))
    return root, row, host, calls, verified, defaults


def test_apply_checks_nginx_recreates_php_and_web_and_records_the_previous_files(site):
    root, row, host, calls, verified, defaults = site
    ident = str(uuid.uuid4())
    assert ps.apply(host, row, GOOD, ident) == WANT
    check = calls[0]
    assert check[:3] == ['docker', 'run', '--rm'] and check[check.index('--network') + 1] == 'hosting-backend-shop' and check[-1] == '-t'
    assert 'nginx.candidate.conf:/etc/nginx/nginx.conf:ro' in ' '.join(check) and not (root / 'conf/nginx.candidate.conf').exists()
    assert calls[1][:4] == ['docker', 'compose', '-f', str(root / 'compose.yml')] and calls[1][-2:] == ['php', 'web'] and '--force-recreate' in calls[1]
    assert calls[2:] == [['docker', 'update', '--pids-limit', '-1', 'hosting-php-shop'], ['docker', 'update', '--pids-limit', '-1', 'hosting-site-shop']]
    assert verified == [['shop.example.com', 'www.shop.example.com']]
    assert yaml.safe_load((root / 'hosting.yaml').read_text())['php_settings'] == WANT
    assert 'upload_max_filesize=64M' in (root / 'conf/php.ini').read_text()
    conf = (root / 'conf/nginx.conf').read_text()
    assert 'client_max_body_size 72m;' in conf and 'fastcgi_read_timeout 130s;' in conf and 'set_real_ip_from 10.240.7.0/24' in conf
    saved = json.loads((ps.SAVED / (ident + '.json')).read_text())
    assert saved['site_id'] == row['id'] and saved['settings'] is None and 'upload_max_filesize=128M' in saved['ini']
    assert ps.stored(row) == WANT
    assert ps.effective(yaml.safe_load((root / 'hosting.yaml').read_text()), json.loads(row['payload'])) == WANT


def test_invalid_values_change_nothing_and_a_failed_apply_rolls_back(site, monkeypatch):
    root, row, host, calls, verified, defaults = site
    before = (root / 'conf/php.ini').read_text(), (root / 'conf/nginx.conf').read_text()
    from reeve.content_site import ContentFailed
    job = {'id': str(uuid.uuid4()), 'payload': json.dumps({**GOOD, 'post_max_size_mb': '1'})}
    with pytest.raises(ContentFailed, match='previous values stay'): ps.perform(host, row, job, lambda s: None)
    assert ((root / 'conf/php.ini').read_text(), (root / 'conf/nginx.conf').read_text()) == before and not verified and not calls
    assert 'at least the largest upload' in (Path(ps.SAVED).parent / 'output' / (job['id'] + '.txt')).read_text()
    # Valid values but the site stops answering: the previous files come back and the containers are recreated again.
    attempts = []
    def verify(names):
        attempts.append(names)
        if len(attempts) == 1: raise RuntimeError('curl failed (22)')
    host.verify_domains = verify
    with pytest.raises(RuntimeError, match='curl failed'): ps.apply(host, row, GOOD)
    assert ((root / 'conf/php.ini').read_text(), (root / 'conf/nginx.conf').read_text()) == before and len(attempts) == 2
    assert 'php_settings' not in yaml.safe_load((root / 'hosting.yaml').read_text())
    assert sum(1 for c in calls if c[:2] == ['docker', 'compose']) == 2
    # A static site has no PHP limits; a PHP site on the defaults has nothing stored.
    static = {'id': str(uuid.uuid4()), 'name': 'pages', 'payload': json.dumps({'runtime': 'static'})}
    assert ps.public(static) is None and ps.stored(row) is None


def test_rollback_restores_saved_files_for_the_same_site_only(site):
    root, row, host, calls, verified, defaults = site
    ident = str(uuid.uuid4()); ps.SAVED.mkdir()
    (ps.SAVED / (ident + '.json')).write_text(json.dumps({'site_id': 'other', 'settings': None, 'ini': 'x', 'nginx': 'y'}))
    with pytest.raises(ValueError, match='another site'): ps.rollback(host, row, ident)
    (ps.SAVED / (ident + '.json')).write_text(json.dumps({'site_id': row['id'], 'settings': WANT, 'ini': '# restored ini\n', 'nginx': '# restored nginx\n'}))
    ps.rollback(host, row, ident)
    assert (root / 'conf/php.ini').read_text() == '# restored ini\n' and (root / 'conf/nginx.conf').read_text() == '# restored nginx\n'
    assert yaml.safe_load((root / 'hosting.yaml').read_text())['php_settings'] == WANT and calls[-3][:2] == ['docker', 'compose']
