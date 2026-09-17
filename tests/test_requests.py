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
    assert 'tls internal' not in public and ' email ops@example.com\n' in public and 'one.example.com {' in public and 'reverse_proxy web:8080' in public
    assert 'email' not in render_routes({}, {'mode': 'public', 'email': ''})
    ops = tmp_path / 'ops'; ops.mkdir(); monkeypatch.setattr(hm, 'OPS', ops)
    assert hm.tls_settings() == {'mode': 'internal', 'email': ''}
    (ops / 'server.yaml').write_text('tls:\n  mode: public\n  email: ops@example.com\n')
    assert hm.tls_settings()['mode'] == 'public' and 'tls internal' not in render_routes({'a.example': {'upstream': 'w:1'}})
    for bad in ('tls:\n  mode: sideways\n', 'tls:\n  email: not-an-address\n', 'tls:\n  cert: x\n'):
        (ops / 'server.yaml').write_text(bad)
        with pytest.raises(ValueError): hm.tls_settings()
    # The hostname check trusts the edge's CA only in internal mode.
    calls = []
    monkeypatch.setattr(hm, 'command', lambda args, timeout=120: calls.append([str(a) for a in args]) or '')
    (ops / 'server.yaml').write_text('tls:\n  mode: public\n')
    hm.Host.verify_domains(None, ['a.example'])
    assert '--cacert' not in calls[-1] and calls[-1][-1] == 'https://a.example/__hosting_health'
    (ops / 'server.yaml').write_text('tls:\n  mode: internal\n')
    hm.Host.verify_domains(None, ['a.example'])
    assert '--cacert' in calls[-1]
