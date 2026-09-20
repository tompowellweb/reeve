import copy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from reeve import compose_adopt as ca
from reeve import host as hm
from reeve.core import DEFAULTS, Ledger
from reeve.worker import dispatch


@pytest.fixture
def context(tmp_path, monkeypatch):
    sites = tmp_path/'sites'; sites.mkdir()
    store=tmp_path/'adoptions'
    monkeypatch.setattr(ca, 'PROXY', tmp_path/'proxy')
    monkeypatch.setattr(ca, 'SITES', sites); monkeypatch.setattr(ca, 'STORE', store)
    monkeypatch.setattr(ca, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    model={'name':'package-demo','services':{'web':{'image':'image:1'}},'networks':{'default':{}}}
    ledger=Ledger(tmp_path/'jobs.db',sites)
    # A Compose package row as package deployment reserves it; the plan is what it compiles.
    ident=str(uuid.uuid4())
    row=ledger.submit(ident,{'name':'demo','domain':'demo.example.com',**DEFAULTS})
    payload=dict(json.loads(row['payload']),runtime='compose',package_id=ident,project_name='package-demo')
    with ledger.db() as db:
        db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload,sort_keys=True),ident))
        db.execute('INSERT INTO adopted_projects VALUES (?,?)',('package-demo',ident))
    row=ledger.get(ident); (sites/'demo').mkdir()
    plan={'name':'demo','project_name':'package-demo','summary':{'services':[{'name':'web'}]},'route':{'domain':'demo.example.com','aliases':[],'web_service':'web','internal_port':8080},
          'sources':[],'model':model,'images':{'web':'sha256:original'},'original_containers':[],'volumes':{},'stage':'reviewed','package_id':ident}
    store.mkdir(); ca.plan_path(ident).mkdir(); ca.save(row,plan)
    return ledger, SimpleNamespace(defaults=DEFAULTS), row, plan


def test_compose_packages_do_not_get_template_tools_or_database_changes(context):
    ledger, host, row, _=context
    for op, extra in [('content-files',{'action':'list','path':'.'}), ('schedule-save',{'data':{}}),
                      ('add-database',{'id':str(uuid.uuid4()),'data':{'engine':'postgres'}})]:
        with pytest.raises(ValueError,match='managed static/PHP'):
            dispatch({'op':op,'site_id':row['id'],**extra},ledger,host)
    with ledger.db() as db:
        assert db.execute('pragma user_version').fetchone()[0]==17
        assert tuple(db.execute('select * from adopted_projects').fetchone())==('package-demo',row['id'])


def test_host_adopted_entry_points_are_retired(context):
    """Folders copied onto the host are no longer discovered, inspected or adopted in place."""
    ledger, host, row, _=context
    for message in ({'op':'compose-discover'}, {'op':'compose-inspect','name':'demo'},
                    {'op':'compose-adopt','id':str(uuid.uuid4()),'name':'demo','data':{}}):
        with pytest.raises(ValueError): dispatch(message,ledger,host)
    # A Compose row without a package cannot be (re)deployed: only the package path compiles plans.
    payload=dict(json.loads(row['payload'])); payload.pop('package_id')
    with pytest.raises(ValueError,match='package path'):
        hm.Host.create(SimpleNamespace(),dict(row,payload=json.dumps(payload)),lambda step:None)


def test_missing_retained_volume_fails_before_any_quota_or_container_change(context,monkeypatch):
    ledger, host, row, plan=context
    plan['volumes']={'db':{'name':'retained-data','created_at':'original','external':False}};ca.save(row,plan)
    changes=[]
    monkeypatch.setattr(ca,'preflight',lambda:None)
    monkeypatch.setattr(ca,'source_check',lambda p:None)
    monkeypatch.setattr(ca,'project_containers',lambda p:[])
    monkeypatch.setattr(ca,'volume_record',lambda name:None)
    monkeypatch.setattr(ca,'apply_quota',lambda *a:changes.append(a))
    with pytest.raises(ValueError,match='will not replace'):ca.perform(host,row,lambda step:None)
    assert not changes


def test_overlay_preserves_explicit_caps_pins_images_and_adds_no_hardening(context):
    _, _, _, plan=context
    plan['model']['services']['web'].update(mem_limit=268435456, cpus=0.5, pids_limit=96)
    plan['volumes']={'db':{'name':'retained-data'}}
    result=ca.overlay({'name':'demo','id':'operation'},plan)
    assert result['services']['web']['image']=='sha256:original'
    assert result['services']['web']['pids_limit']==96
    assert result['services']['web']['storage_opt']=={'size':'0'}
    assert result['volumes']=={'db':{'external':True,'name':'retained-data'}}
    assert result['networks']['default']==plan['model']['networks']['default']
    assert 'hosting_ingress' in result['services']['web']['networks']
    # Mode 2 runs images as shipped: no capability or privilege settings the author did not declare.
    assert not {'cap_drop','security_opt','user'} & set(result['services']['web'])


def test_eight_services_keep_segmentation_and_outbound_paths(context):
    _, _, _, plan=context
    plan['model']['networks']={'api':{'name':'original_api','internal':True},
        'data':{'name':'original_data','internal':True},'outbound':{'name':'original_outbound','internal':False}}
    plan['model']['services']={}
    for index, service in enumerate(('web','dotnet','python','php','search','queue','worker','database')):
        nets={'api':{}} if index<4 else {'data':{}}
        if service=='worker':nets['outbound']={}
        if service=='python':nets['data']={}
        plan['model']['services'][service]={'networks':nets}
        plan['images'][service]='sha256:'+service
    result=ca.overlay({'name':'demo','id':'operation'},plan)
    for service,spec in plan['model']['services'].items():
        assert set(result['services'][service]['networks'])==set(spec['networks'])|({'hosting_ingress'} if service=='web' else set())
    assert result['networks']['outbound']['internal'] is False
    assert 'data' not in result['services']['web']['networks']
    assert 'hosting_ingress' not in result['services']['worker']['networks']


def test_quota_boundary_refuses_nested_mounts_and_external_hardlinks(tmp_path):
    import os
    root=tmp_path/'site';root.mkdir()
    mounts=tmp_path/'mountinfo';mounts.write_text('')
    outside=tmp_path/'other-site-data';outside.write_text('retained')
    os.link(outside,root/'linked')
    with pytest.raises(ValueError,match='hard links outside'):ca.filesystem_boundaries([root],mounts)
    outside.unlink()
    ca.filesystem_boundaries([root],mounts)
    mounts.write_text(f'31 20 0:1 / {root}/nested rw - xfs /dev/vdb rw\n')
    with pytest.raises(ValueError,match='nested mount'):ca.filesystem_boundaries([root],mounts)


def test_unlimited_process_sentinels_require_actual_unlimited_kernel_limit(tmp_path):
    proc = tmp_path / 'proc'; cgroups = tmp_path / 'cgroups'
    (proc / '123').mkdir(parents=True); (cgroups / 'docker-test').mkdir(parents=True)
    (proc / '123/cgroup').write_text('0::/docker-test\n')
    limit = cgroups / 'docker-test/pids.max'; limit.write_text('max\n')
    container = {'HostConfig': {'PidsLimit': 0}, 'State': {'Pid': 123}}
    ca.verify_process_limit(container, -1, proc, cgroups)
    container['HostConfig']['PidsLimit'] = -1
    ca.verify_process_limit(container, -1, proc, cgroups)
    limit.write_text('512\n')
    with pytest.raises(ValueError, match='kernel process limit'):
        ca.verify_process_limit(container, -1, proc, cgroups)
    container['HostConfig']['PidsLimit'] = 512
    ca.verify_process_limit(container, 512, proc, cgroups)


def test_https_verification_waits_for_the_edge_and_names_the_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(hm, 'trust_bundle', lambda: '/tmp/bundle.crt')
    monkeypatch.setattr(ca, 'command', lambda args, timeout=120: calls.append((args, timeout)))
    ca.verify_https(['shop.example.com', 'www.shop.example.com'])
    assert [a[-1] for a, _ in calls] == ['https://shop.example.com/', 'https://www.shop.example.com/']
    args, timeout = calls[0]
    # Let's Encrypt has not issued a fresh name's certificate when the edge is published: keep trying.
    assert '--retry-all-errors' in args and args[args.index('--retry-max-time') + 1] == '150' and timeout > 150
    assert args[args.index('--resolve') + 1] == 'shop.example.com:443:127.0.0.1'

    def refuse(args, timeout=120): raise RuntimeError("curl failed (35): curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL")
    monkeypatch.setattr(ca, 'command', refuse)
    with pytest.raises(ValueError, match=r'did not serve https://shop.example.com/ .*SSL_ERROR_SYSCALL.*Retry deployment'):
        ca.verify_https(['shop.example.com'])
