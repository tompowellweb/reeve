import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parent.parent / 'install.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


def test_the_data_filesystem_decision_adopts_formats_or_advises():
    assert installer.plan_data({'fstype': 'xfs', 'options': 'rw,relatime,prjquota'}, None) == 'adopt'
    assert installer.plan_data({'fstype': 'xfs', 'options': 'rw,pquota'}, '/dev/vdb') == 'adopt'  # a mounted /srv wins over a device
    assert installer.plan_data(None, '/dev/vdb') == 'format'
    with pytest.raises(SystemExit, match='prjquota'): installer.plan_data({'fstype': 'ext4', 'options': 'rw'}, None)
    with pytest.raises(SystemExit, match='not XFS with project quotas'): installer.plan_data({'fstype': 'xfs', 'options': 'rw,relatime'}, None)
    with pytest.raises(SystemExit, match='--data-device'): installer.plan_data(None, None)


def test_the_tree_is_the_commit_with_its_tag_or_version_and_refuses_uncommitted_changes(tmp_path):
    git = lambda *args: subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(tmp_path)], check=True)
    git('config', 'user.email', 'dev@example.net'); git('config', 'user.name', 'Dev')
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "reeve-panel"\nversion = "1.4.0"\n')
    (tmp_path / 'reeve').mkdir(); (tmp_path / 'reeve/__init__.py').write_text('')
    git('add', '.'); git('commit', '-q', '-m', 'first')
    first = git('rev-parse', 'HEAD')
    commit, version, modified, archive, changed = installer.tree(tmp_path, None, False)
    assert commit == first and version == '1.4.0+' + first[:7] and modified is False and changed == [] and len(archive) > 0
    git('tag', '-a', 'v1.4.0', '-m', 'release'); git('tag', 'v0.9.0')
    assert installer.tree(tmp_path, None, False)[1] == '1.4.0'  # the highest release tag at the commit
    (tmp_path / 'reeve/__init__.py').write_text('changed')
    with pytest.raises(SystemExit, match='uncommitted'): installer.tree(tmp_path, None, False)
    commit, version, modified, archive, changed = installer.tree(tmp_path, None, True)
    assert modified is True and changed == ['reeve/__init__.py']
    git('commit', '-q', '-am', 'second')
    second = git('rev-parse', 'HEAD')
    assert installer.tree(tmp_path, first, False)[:3] == (first, '1.4.0', False)  # a named commit from the history, clean by definition
    assert installer.tree(tmp_path, second, False)[1] == '1.4.0+' + second[:7]


def test_a_modified_release_carries_the_working_trees_files(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'BASE', tmp_path / 'opt')
    monkeypatch.setattr(installer, 'run', lambda *args, cwd=None: '')  # no venv or pip in a test
    source = tmp_path / 'src'; source.mkdir(); (source / 'reeve').mkdir()
    (source / 'reeve/__init__.py').write_text('new text'); (source / 'requirements.lock').write_text('')
    import io, tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        info = tarfile.TarInfo('reeve/__init__.py'); data = b'old text'; info.size = len(data); tar.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo('requirements.lock'); info.size = 0; tar.addfile(info, io.BytesIO(b''))
    release = installer.install_release(source, 'a' * 40, '1.0.0', True, buffer.getvalue(), ['reeve/__init__.py'])
    assert release.name.startswith('a' * 40 + '-modified-') and (release / 'reeve/__init__.py').read_text() == 'new text'
    assert (release / '.installed').read_text().startswith('1.0.0 ' + 'a' * 40 + ' modified')
    clean = installer.install_release(source, 'b' * 40, '1.0.0', False, buffer.getvalue(), [])
    assert clean.name == 'b' * 40 and (clean / 'reeve/__init__.py').read_text() == 'old text'
