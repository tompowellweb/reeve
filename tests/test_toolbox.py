import base64
import json
import struct
import uuid
from pathlib import Path

import pytest

from reeve import toolbox
from reeve.core import Ledger


def key():
    kind=b'ssh-ed25519'; value=b'x'*32
    return 'ssh-ed25519 '+base64.b64encode(struct.pack('>I',len(kind))+kind+struct.pack('>I',len(value))+value).decode()


def data(**overrides):
    return {'recipe':'php-workbench','path':'wp-content/themes/custom','public_key':key(),
            'jump':'admin@203.0.113.10','internet':True,**overrides}


def test_public_key_options_private_material_and_path_traversal_rejected():
    assert toolbox.validate('toolbox-start',data())['public_key']==key()
    for overrides in ({'public_key':'-----BEGIN OPENSSH PRIVATE KEY-----'},
                      {'public_key':'command="id" '+key()}, {'path':'../../root'},
                      {'jump':'admin@server;id'}, {'recipe':'../other'}, {'internet':1}):
        with pytest.raises(ValueError): toolbox.validate('toolbox-start',data(**overrides))


def test_recipe_edits_preserve_separate_start_snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(toolbox,'ROOT',tmp_path/'worker')
    monkeypatch.setattr(toolbox,'SITES',tmp_path/'sites')
    monkeypatch.setattr(toolbox,'trusted',lambda *a,**k:None)
    monkeypatch.setattr(toolbox,'atomic',lambda path,text,mode=0o600:path.write_text(text))
    monkeypatch.setattr(toolbox,'verify_quota',lambda *a,**k:{'used_bytes':0,'hard_bytes':104857600})
    toolbox.initialize()
    toolbox.save_recipe('custom','FROM debian:trixie\nRUN echo first\n')
    row={'id':str(uuid.uuid4()),'name':'site','project':100000,'payload':'{"data_mb":100}'}
    ident=str(uuid.uuid4())
    toolbox.prepare(row,ident,'toolbox-start',data(recipe='custom'))
    toolbox.save_recipe('custom','FROM debian:trixie\nRUN echo second\n')
    saved=json.loads((toolbox.ROOT/'intents'/(ident+'.json')).read_text())
    assert 'first' in saved['dockerfile'] and 'second' not in saved['dockerfile']
    assert next(r for r in toolbox.recipes() if r['name']=='custom')['dockerfile'].endswith('second\n')


def test_active_toolbox_serializes_mutations_but_permits_stop(tmp_path):
    ledger=Ledger(tmp_path/'db',tmp_path/'sites')
    row=ledger.submit(str(uuid.uuid4()),{'name':'alpha','domain':'alpha.example.com'})
    ledger.update(row['id'],'succeeded','ready')
    toolbox.update_session(ledger,row,str(uuid.uuid4()),'active',{})
    with pytest.raises(ValueError,match='pending'):
        ledger.submit_content(str(uuid.uuid4()),row['id'],'tool',{'tool':'shell','path':'.','arguments':'id','internet':False})
    with pytest.raises(ValueError,match='pending'):
        ledger.submit_domains(str(uuid.uuid4()),row['id'],['new.example.com'])
    stop=ledger.submit_content(str(uuid.uuid4()),row['id'],'toolbox-stop',{})
    assert stop['state']=='queued'


def test_worker_recovery_stops_unfinished_start_and_keeps_completed_session(tmp_path,monkeypatch):
    ledger=Ledger(tmp_path/'db',tmp_path/'sites')
    sites=[]
    for name in ('alpha','beta'):
        row=ledger.submit(str(uuid.uuid4()),{'name':name,'domain':name+'.example.com'}); ledger.update(row['id'],'succeeded','ready')
        job=ledger.submit_content(str(uuid.uuid4()),row['id'],'toolbox-start',data())
        toolbox.update_session(ledger,row,job['id'],'active',{})
        ledger.update_content(job['id'],'succeeded' if name=='alpha' else 'running','started')
        sites.append(row)
    ledger.interrupted()
    stopped=[]
    monkeypatch.setattr(toolbox,'initialize',lambda:None)
    monkeypatch.setattr(toolbox,'stop',lambda ledger,host,row:stopped.append(row['name']))
    toolbox.recover(ledger,None)
    assert stopped==['beta']
