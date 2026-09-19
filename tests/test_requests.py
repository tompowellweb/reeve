import pytest
from reeve.requests_site import render,validate
from reeve.host import render_routes


def test_trust_is_scoped_and_cgi_fields_have_single_authoritative_values():
    config=render('10.240.25.0/24','wordpress')
    assert 'set_real_ip_from 10.240.25.0/24;' in config and 'set_real_ip_from 0.0.0.0/0' not in config
    assert 'if ($hosting_proxy = 0) { return 403; }' in config
    assert config.count('fastcgi_param SERVER_PORT ')==1 and 'fastcgi_param SERVER_PORT 443;' in config
    assert config.count('fastcgi_param REQUEST_SCHEME ')==1 and 'fastcgi_param REQUEST_SCHEME https;' in config
    assert 'absolute_redirect off;' in config
    edge=render_routes({'one.example.com':{'upstream':'web:8080'}})
    assert 'output file /data/logs/one.example.com.log' in edge and 'roll_keep_for 720h' in edge and 'format json' in edge
    assert 'header_up -Forwarded' in edge and 'header_up X-Forwarded-For {http.request.remote.host}' in edge


def test_explicit_drupal_profile_preserves_query_and_blocks_source_extensions():
    config=render('10.240.25.0/24','drupal7')
    assert '/index.php?q=$uri&$args' in config
    assert 'install|make|module|profile' in config
    assert config.index('wp-config')<config.index(r'location ~ \.php$')
    assert 'sites/[^/]+/files' in config
    with pytest.raises(ValueError): validate({'profile':'custom; include /etc/passwd'})


def test_private_config_filter_does_not_block_wordpress_compose_js_or_public_slugs():
    import re
    from reeve.host import NGINX
    line=next(line for line in NGINX.splitlines() if 'Dockerfile' in line)
    pattern=line.split('location ~* ',1)[1].split(' {',1)[0]
    rule=re.compile(pattern,re.I)
    for path in ('/wp-includes/js/dist/compose.min.js','/hosting-permalink-proof/'):
        assert not rule.search(path)
    for path in ('/compose.yaml','/docker-compose.override.yml','/hosting.yaml','/Dockerfile','/composer.lock'):
        assert rule.search(path)


def test_the_edge_takes_public_certificates_when_tls_mode_is_public(tmp_path, monkeypatch):
    from reeve import host as hm
    internal = render_routes({'one.example.com': {'upstream': 'web:8080'}}, {'mode': 'internal', 'email': ''})
    assert ' tls internal\n' in internal and 'email' not in internal
    public = render_routes({'one.example.com': {'upstream': 'web:8080'}}, {'mode': 'public', 'email': 'ops@example.com'})
    # Public mode asks Let's Encrypt first and serves the edge's own certificate until that succeeds, so a name whose
    # DNS does not point here yet still routes, still passes the loopback checks and shows its state on the site page.
    assert ' tls internal\n' not in public and ' tls {\n  issuer acme\n  issuer internal\n }\n' in public
    assert ' email ops@example.com\n' in public and 'one.example.com {' in public and 'reverse_proxy web:8080' in public
    assert 'email' not in render_routes({}, {'mode': 'public', 'email': ''})
    ops = tmp_path / 'ops'; ops.mkdir(); monkeypatch.setattr(hm, 'OPS', ops)
    assert hm.tls_settings() == {'mode': 'internal', 'email': ''}
    (ops / 'server.yaml').write_text('tls:\n  mode: public\n  email: ops@example.com\n')
    assert hm.tls_settings()['mode'] == 'public' and ' tls internal\n' not in render_routes({'a.example': {'upstream': 'w:1'}})
    for bad in ('tls:\n  mode: sideways\n', 'tls:\n  email: not-an-address\n', 'tls:\n  cert: x\n'):
        (ops / 'server.yaml').write_text(bad)
        with pytest.raises(ValueError): hm.tls_settings()
    # The hostname check trusts the system CAs and the edge's own together, in either mode.
    calls = []
    monkeypatch.setattr(hm, 'command', lambda args, timeout=120: calls.append([str(a) for a in args]) or '')
    proxy = tmp_path / 'proxy'; (proxy / 'data/caddy/pki/authorities/local').mkdir(parents=True)
    (proxy / 'data/caddy/pki/authorities/local/root.crt').write_text('EDGE\n'); monkeypatch.setattr(hm, 'PROXY', proxy)
    for mode in ('public', 'internal'):
        (ops / 'server.yaml').write_text(f'tls:\n  mode: {mode}\n')
        hm.Host.verify_domains(None, ['a.example'])
        assert calls[-1][calls[-1].index('--cacert') + 1] == str(ops / 'panel/worker/ca-bundle.crt') and calls[-1][-1] == 'https://a.example/__hosting_health'
    bundle = (ops / 'panel/worker/ca-bundle.crt').read_text()
    assert bundle.endswith('EDGE\n') and len(bundle) > 5  # the system's CAs, then the edge's
    (proxy / 'data/caddy/pki/authorities/local/root.crt').write_text('EDGE2\n')
    hm.trust_bundle()
    assert (ops / 'panel/worker/ca-bundle.crt').read_text().endswith('EDGE2\n')  # refreshed when the edge's CA changes


def test_a_domain_change_checks_the_new_primary_name_not_the_one_still_recorded(tmp_path, monkeypatch):
    # The edge already routes the new names when the routing profile is re-applied, and the old primary has no
    # route any more. A DigitalOcean install on 2026-09-19 failed here: the check used hosting.yaml's old name,
    # the rollback did the same, and the job ended in "rollback needs review" with the panel showing the old name.
    import json, uuid, yaml
    from types import SimpleNamespace
    import reeve.requests_site as rs
    sites = tmp_path / 'sites'; root = sites / 'test'; (root / 'conf').mkdir(parents=True)
    monkeypatch.setattr(rs, 'SITES', sites); monkeypatch.setattr(rs, 'SAVED', tmp_path / 'saved'); monkeypatch.setattr(rs, 'PROXY', tmp_path / 'proxy')
    monkeypatch.setattr(rs, 'trusted', lambda *a, **k: None); monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None)
    (tmp_path / 'proxy/data/caddy/pki/authorities/local').mkdir(parents=True); (tmp_path / 'proxy/data/caddy/pki/authorities/local/root.crt').write_text('local\n')
    real = rs.Path; (tmp_path / 'ca.crt').write_text('system\n')
    monkeypatch.setattr(rs, 'Path', lambda p: real(str(p).replace('/etc/ssl/certs/ca-certificates.crt', str(tmp_path / 'ca.crt'))))
    row = {'id': str(uuid.uuid4()), 'name': 'test', 'payload': json.dumps({'runtime': 'php', 'php_version': '8.4'})}
    (root / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'php', 'operation_id': row['id'], 'domain': 'test.yoursitepreview.co.uk', 'aliases': [],
                                                       'web_settings': {'version': 1, 'profile': 'wordpress', 'trusted_ingress': '10.240.7.0/24'}}))
    (root / 'compose.yml').write_text(yaml.safe_dump({'services': {'web': {'image': 'nginx@sha256:abc', 'user': '30001:30001', 'container_name': 'hosting-site-test', 'volumes': []},
                                                                    'php': {'image': 'hosting-php-site:x', 'container_name': 'hosting-php-test', 'volumes': [], 'networks': {}}}, 'networks': {}}))
    (root / 'conf/nginx.conf').write_text('# old\n'); (root / 'conf/site.nginx.conf').write_text('# none\n')
    network = json.dumps([{'Labels': {'hosting.operation': row['id']}, 'Internal': True, 'IPAM': {'Config': [{'Subnet': '10.240.7.0/24'}]}}])
    def command(args, timeout=120):
        if args[:3] == ['docker', 'network', 'inspect']: return network
        if args[:3] == ['docker', 'network', 'ls']: return ''
        return ''
    monkeypatch.setattr(rs, 'command', command)
    verified = []
    host = SimpleNamespace(verify_domains=lambda names: verified.append(list(names)))
    rs.apply(host, row, 'wordpress', domains=['test.powell.systems'], publish=False)
    assert verified == [['test.powell.systems']]
    assert yaml.safe_load((root / 'hosting.yaml').read_text())['domain'] == 'test.powell.systems'
    # When the recreated site does not answer on the new name, the rollback checks that same routed name, so it can succeed.
    attempts = []
    def flaky(names):
        attempts.append(list(names))
        if len(attempts) == 1: raise RuntimeError('curl failed (35)')
    host.verify_domains = flaky
    with pytest.raises(RuntimeError, match='curl failed'): rs.apply(host, row, 'wordpress', domains=['test.powell.systems'], publish=False)
    assert attempts == [['test.powell.systems'], ['test.powell.systems']]
