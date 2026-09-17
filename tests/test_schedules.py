import json
import uuid
import pytest
from reeve.core import Ledger
from reeve import schedules,toolbox


def setup(tmp_path,monkeypatch):
    ledger=Ledger(tmp_path/'db',tmp_path/'sites'); row=ledger.submit(str(uuid.uuid4()),{'name':'alpha','domain':'alpha.example.com','runtime':'php','php_version':'8.4'})
    ledger.update(row['id'],'succeeded','ready'); monkeypatch.setattr(schedules,'export',lambda *a:None)
    settings={'name':'wordpress','interval':1,'enabled':True,'tool':'php','arguments':'wp-cron.php','path':'.','internet':False}
    schedules.save(ledger,row['id'],settings)
    return ledger,row,settings


def test_schedule_is_atomic_nonoverlapping_and_coalesces_missed_intervals(tmp_path,monkeypatch):
    ledger,row,settings=setup(tmp_path,monkeypatch)
    due=schedules.list_schedules(ledger,row['id'])[0]['next_run']+600
    schedules.tick(ledger,due); schedules.tick(ledger,due+600)
    jobs=ledger.content_jobs(); assert len(jobs)==1 and jobs[0]['kind']=='tool'
    assert json.loads(jobs[0]['payload'])['arguments']=='wp-cron.php'
    ledger.update_content(jobs[0]['id'],'succeeded','done'); schedules.tick(ledger,due+601)
    assert len(ledger.content_jobs())==2


def test_toolbox_blocks_scheduled_run_and_failure_needs_explicit_resume(tmp_path,monkeypatch):
    ledger,row,settings=setup(tmp_path,monkeypatch); due=schedules.list_schedules(ledger,row['id'])[0]['next_run']+1
    toolbox.update_session(ledger,row,str(uuid.uuid4()),'active',{})
    schedules.tick(ledger,due); assert not ledger.content_jobs()
    toolbox.update_session(ledger,row,str(uuid.uuid4()),'stopped',{})
    schedules.tick(ledger,due); job=ledger.content_jobs()[0]
    ledger.update_content(job['id'],'failed','failed')
    schedules.tick(ledger,due+61); assert not schedules.list_schedules(ledger,row['id'])[0]['enabled']
    schedules.save(ledger,row['id'],settings); schedules.tick(ledger,due+600)
    assert len(ledger.content_jobs())==2


def test_interrupted_run_is_not_replayed_and_disable_retains_run(tmp_path,monkeypatch):
    ledger,row,settings=setup(tmp_path,monkeypatch); due=schedules.list_schedules(ledger,row['id'])[0]['next_run']+1
    schedules.tick(ledger,due); job=ledger.content_jobs()[0]; ledger.update_content(job['id'],'running','executing'); ledger.interrupted()
    schedules.tick(ledger,due+600); assert len(ledger.content_jobs())==1
    schedules.save(ledger,row['id'],dict(settings,enabled=False)); assert schedules.list_schedules(ledger,row['id'])[0]['last_job']==job['id']
    schedules.tick(ledger,due+1200); assert len(ledger.content_jobs())==1


def test_schedule_bounds(tmp_path,monkeypatch):
    ledger,row,settings=setup(tmp_path,monkeypatch)
    for change in ({'interval':0},{'interval':True},{'interval':1441},{'name':'../escape'},{'path':'../escape'},{'arguments':''}):
        with pytest.raises(ValueError): schedules.save(ledger,row['id'],dict(settings,**change))
