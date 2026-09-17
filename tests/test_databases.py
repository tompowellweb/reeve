import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from reeve.core import Ledger, validate_create
from reeve.database_jobs import validate_database
from reeve import database_versions as versions
from reeve import database_site
from reeve.worker import dispatch


def test_fixed_numeric_versions_and_no_default_container_caps():
    data = validate_create({'name': 'dbtest', 'domain': 'dbtest.hosting.test', 'database': {'engine': 'mysql', 'exact': '8.4.11'}})
    assert data['database'] == {'engine': 'mysql', 'exact': '8.4.11', 'usage': 'standard'}  # the profile's usage, standard on a clean install
    assert data['data_mb'] == 1024
    for bad in ({'engine':'redis'}, {'engine':'postgres','exact':'18'}, {'engine':'postgres','exact':'9.6'},
                {'engine':'mysql','exact':'mysql:8.4'}, {'engine':'mysql','mount':'/root'}, {'engine':'mysql','memory_mb':float('nan')}):
        with pytest.raises(ValueError): validate_database(bad)
    assert versions.numeric('9.6.24','postgres')
    assert versions.series('postgres','9.6.24') == '9.6'
    assert versions.series('postgres','18.6') == '18'


def test_series_from_official_metadata_selects_lts_not_innovation():
    text = 'Tags: 26.7.0, latest, innovation\nArchitectures: amd64\n\nTags: 9.7.2, lts\nArchitectures: amd64, arm64v8\n\nTags: 10.0.0-rc\nArchitectures: amd64'
    result = versions.published(text,'mysql','amd64')
    assert result['9.7.2']['default'] and not result['26.7.0']['default']
    assert len(result) == 2
    assert set(versions.published(text,'mysql','arm64')) == {'9.7.2'}


def test_catalogue_failure_preserves_last_success_and_selection(tmp_path, monkeypatch):
    path = tmp_path / 'cache.json'
    before = {'engines': {'postgres': {'series': [{'series':'18','version':'18.6','default':True}]}}, 'checked_at':1,'attempted_at':1,'error':''}
    path.write_text(json.dumps(before))
    monkeypatch.setattr(versions, 'CACHE', path)
    monkeypatch.setattr(versions, 'trusted', lambda p: None)
    monkeypatch.setattr(versions, 'atomic', lambda p,t: p.write_text(t))
    monkeypatch.setattr(versions, 'fetch', lambda *a, **kw: (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(OSError): versions.refresh()
    result=versions.read()
    assert result['engines']==before['engines'] and result['checked_at']==1 and result['error']=='offline'
    assert versions.select({'engine':'postgres'}) == '18.6'
    assert versions.select({'engine':'postgres','exact':'9.6.24'}) == '9.6.24'


def test_registry_resolution_pins_only_requested_architecture(monkeypatch):
    monkeypatch.setattr(versions,'token',lambda e:'private-token')
    monkeypatch.setattr(versions,'architecture',lambda:'amd64')
    monkeypatch.setattr(versions,'fetch',lambda *a:json.dumps({'manifests':[
        {'platform':{'os':'linux','architecture':'arm64'},'digest':'sha256:'+'a'*64},
        {'platform':{'os':'linux','architecture':'amd64'},'digest':'sha256:'+'b'*64}]}).encode())
    assert versions.resolve('mysql','9.7.2').endswith('sha256:'+'b'*64)


def test_database_jobs_serialize_domain_and_php_and_survive_restart(tmp_path):
    ledger=Ledger(tmp_path/'jobs.db',sites=tmp_path/'sites')
    row=ledger.submit(str(uuid.uuid4()),{'name':'alpha','domain':'alpha.hosting.test','runtime':'php','php_version':'8.3'})
    ledger.update(row['id'],'succeeded','published')
    ident=str(uuid.uuid4());spec={'engine':'postgres','series':'18'}
    job=ledger.submit_database(ident,'add',row['id'],spec)
    assert ledger.submit_database(ident,'add',row['id'],spec)==job
    with pytest.raises(ValueError): ledger.submit_database(ident,'add',row['id'],{'engine':'mysql'})
    with pytest.raises(ValueError): ledger.submit_domains(str(uuid.uuid4()),row['id'],['next.hosting.test'])
    with pytest.raises(ValueError): ledger.submit_runtime(str(uuid.uuid4()),'switch',row['id'],{'branch':'8.2'})
    ledger.update_database(ident,'running','initializing database');ledger.interrupted()
    assert ledger.database_jobs()[0]['state']=='recovery-needed'
    Ledger(ledger.path,ledger.sites).retry_database(ident)
    assert ledger.database_jobs()[0]['state']=='queued'
    ledger.update_database(ident,'succeeded','verified')
    with pytest.raises(ValueError): ledger.submit_database(str(uuid.uuid4()),'add',row['id'],spec)
    ledger.submit_domains(str(uuid.uuid4()),row['id'],['next.hosting.test'])


def test_routine_database_status_never_returns_credentials(monkeypatch):
    monkeypatch.setattr(database_site,'state',lambda row:{'engine':'mysql','version':'9.7.2','series':'9.7','image':'pinned','stage':'ready','app_password':'sensitive-app','admin_password':'sensitive-admin'})
    row={'name':'alpha','id':str(uuid.uuid4())}
    result=database_site.public(row,SimpleNamespace(inspect=lambda n:None))
    assert 'sensitive' not in json.dumps(result)
    assert database_site.credentials(row)['password']=='sensitive-app'


def test_unhealthy_database_marks_site_unhealthy(tmp_path, monkeypatch):
    ledger=Ledger(tmp_path/'jobs.db',sites=tmp_path/'sites')
    ledger.submit(str(uuid.uuid4()),{'name':'alpha','domain':'alpha.hosting.test'})
    monkeypatch.setattr(database_site,'public',lambda row,host:{'health':'unhealthy'})
    rows=dispatch({'op':'list'},ledger,SimpleNamespace(health=lambda row:{'application':'healthy'}))
    assert rows[0]['health']=={'application':'unhealthy','database':'unhealthy'}


def test_legacy_php_gets_native_mysql_authentication_on_8_0_only():
    from reeve.database_site import legacy_php, mysql_auth
    assert legacy_php('7.0') and legacy_php('7.1') and not legacy_php('7.2') and not legacy_php('8.5') and not legacy_php(None)
    # PHP 7.0's mysqlnd lacks caching_sha2_password, MySQL 8's default; 8.0 still offers the native plugin.
    assert mysql_auth('mysql', '8.0', '7.0') == 'mysql_native_password'
    assert mysql_auth('mysql', '8.0', '8.3') is None and mysql_auth('mariadb', '10.11', '7.0') is None and mysql_auth('postgres', '16', '7.0') is None
    with pytest.raises(ValueError, match='MySQL 8.0 series or MariaDB'): mysql_auth('mysql', '8.4', '7.1')
    from reeve.database_site import server_options
    assert server_options('postgres', None) == ['postgres', '-c', 'shared_buffers=128MB', '-c', 'max_connections=150']
    assert server_options('mysql', None) == ['--socket=/tmp/mysql.sock', '--pid-file=/tmp/mysqld.pid', '--innodb-buffer-pool-size=256M', '--max-connections=150', '--skip-log-bin', '--performance-schema=OFF']
    assert server_options('mysql', 'mysql_native_password')[-1] == '--default-authentication-plugin=mysql_native_password'
