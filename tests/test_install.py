import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parent.parent / 'install.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


def test_the_data_filesystem_decision_adopts_amends_formats_or_advises():
    assert installer.plan_data({'fstype': 'xfs', 'options': 'rw,relatime,prjquota'}, None) == 'adopt'
    assert installer.plan_data({'fstype': 'xfs', 'options': 'rw,pquota'}, '/dev/vdb') == 'adopt'  # a mounted /srv wins over a device
    assert installer.plan_data({'fstype': 'xfs', 'options': 'rw,relatime'}, None) == 'amend'  # XFS without quotas: add the option
    assert installer.plan_data({'fstype': 'ext4', 'options': 'rw', 'source': '/dev/vdb'}, None, empty=True) == 'format-mounted'
    assert installer.plan_data(None, '/dev/vdb') == 'format'
    with pytest.raises(SystemExit, match='holds data'): installer.plan_data({'fstype': 'ext4', 'options': 'rw', 'source': '/dev/vdb'}, None, empty=False)
    assert installer.plan_data(None, None) == 'image'  # nothing separate: the image file is offered


def test_fstab_is_amended_in_place_or_extended():
    text = "UUID=root / ext4 errors=remount-ro 0 1\nUUID=abc /srv xfs defaults 0 0\n"
    amended = installer.amend_fstab(text, '/srv', 'UUID=abc')
    assert amended.splitlines()[1].split() == ['UUID=abc', '/srv', 'xfs', 'defaults,prjquota', '0', '0'] and amended.splitlines()[0] == text.splitlines()[0]
    assert installer.amend_fstab(amended, '/srv', 'UUID=abc') == amended  # once is enough
    text = "UUID=root / ext4 errors=remount-ro 0 1\nUUID=old /srv ext4 defaults 0 2\n"
    assert installer.amend_fstab(text, '/srv', 'UUID=new').splitlines()[1].split() == ['UUID=new', '/srv', 'xfs', 'defaults,prjquota', '0', '2']
    absent = "UUID=root / ext4 errors=remount-ro 0 1\n"
    assert installer.amend_fstab(absent, '/srv', 'UUID=abc').splitlines()[-1] == 'UUID=abc /srv xfs defaults,prjquota 0 0'
    assert '# Reeve data' in installer.amend_fstab(absent, '/srv', 'UUID=abc')


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


def test_a_tree_without_git_installs_as_the_project_version(tmp_path):
    source = tmp_path / 'reeve-1.4.0'; (source / 'reeve').mkdir(parents=True)
    (source / 'pyproject.toml').write_text('[project]\nname = "reeve-panel"\nversion = "1.4.0"\n')
    (source / 'reeve/__init__.py').write_text(''); (source / '.venv').mkdir(); (source / '.venv/junk').write_text('x')
    commit, version, modified, archive, changed = installer.tree(source, None, False)
    assert version == '1.4.0' and modified is False and len(commit) == 40 and changed == []
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar: names = tar.getnames()
    assert 'reeve/__init__.py' in names and 'pyproject.toml' in names and not any(n.startswith('.venv') for n in names)
    assert installer.tree(source, None, False)[0] == commit  # the same tree gives the same identity


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


def test_the_image_takes_the_chosen_share_but_leaves_the_root_its_reserve():
    gib = 1024 ** 3
    assert installer.image_size(100 * gib, 80) == 80 * gib
    assert installer.image_size(30 * gib, 80) == 20 * gib  # 80% would leave 6 GB; the reserve wins
    assert installer.image_size(50 * gib, 30) == 15 * gib
    with pytest.raises(SystemExit, match='Free space, attach a volume'): installer.image_size(15 * gib, 80)
    assert installer.amend_fstab('UUID=root / ext4 errors=remount-ro 0 1\n', '/srv', '/var/lib/reeve/srv.img', 'xfs', 'loop,prjquota').splitlines()[-1] == '/var/lib/reeve/srv.img /srv xfs loop,prjquota 0 0'


def test_only_empty_disks_and_partitions_of_a_usable_size_are_offered():
    gib = 1024 ** 3
    listing = [
        {'path': '/dev/vda', 'type': 'disk', 'size': 30 * gib, 'fstype': None, 'pttype': 'gpt', 'mountpoint': None},   # partitioned
        {'path': '/dev/vda1', 'type': 'part', 'size': 29 * gib, 'fstype': 'ext4', 'pttype': 'gpt', 'mountpoint': '/'},
        {'path': '/dev/vda15', 'type': 'part', 'size': 124 * 1024 ** 2, 'fstype': None, 'pttype': 'gpt', 'mountpoint': None},  # too small
        {'path': '/dev/vda3', 'type': 'part', 'size': 25 * gib, 'fstype': None, 'pttype': 'gpt', 'mountpoint': None},  # spare partition
        {'path': '/dev/vdb', 'type': 'disk', 'size': 40 * gib, 'fstype': None, 'pttype': None, 'mountpoint': None},   # empty disk
        {'path': '/dev/vdc', 'type': 'disk', 'size': 40 * gib, 'fstype': 'xfs', 'pttype': None, 'mountpoint': None},  # holds a filesystem
        {'path': '/dev/sr0', 'type': 'rom', 'size': 379 * 1024, 'fstype': 'iso9660', 'pttype': None, 'mountpoint': None},
        {'path': '/dev/zram0', 'type': 'disk', 'size': 8 * gib, 'fstype': 'swap', 'pttype': None, 'mountpoint': '[SWAP]'},
        {'path': '/dev/loop0', 'type': 'loop', 'size': 8 * gib, 'fstype': None, 'pttype': None, 'mountpoint': None},
        {'path': '/dev/vdd', 'type': 'disk', 'size': '40000000000', 'fstype': None, 'pttype': None, 'mountpoint': None},  # older lsblk: strings
    ]
    assert installer.empty_devices(listing) == [
        ('/dev/vda3', 25 * gib, 'empty partition'), ('/dev/vdb', 40 * gib, 'empty disk'), ('/dev/vdd', 40000000000, 'empty disk')]


def test_the_data_menu_lists_devices_then_the_image_when_the_root_has_room():
    gib = 1024 ** 3
    devices = [('/dev/vdb', 40 * gib, 'empty disk')]
    labels = [label for label, _ in installer.data_choices(devices, 32 * gib)]
    assert labels == ['/dev/vdb          40 GB  empty disk', 'An XFS image file on the root filesystem (about 32 GB)']
    assert [action for _, action in installer.data_choices(devices, 32 * gib)] == [('format', '/dev/vdb'), ('image', None)]
    assert installer.data_choices(devices, None) == [('/dev/vdb          40 GB  empty disk', ('format', '/dev/vdb'))]  # root too small
    assert installer.data_choices([], None) == []


def test_the_choice_is_asked_only_when_there_is_one_to_make(monkeypatch):
    gib = 1024 ** 3
    listing = {'blockdevices': [{'path': '/dev/vdb', 'type': 'disk', 'size': 40 * gib, 'fstype': None, 'pttype': None, 'mountpoint': None}]}
    monkeypatch.setattr(installer, 'run', lambda *a: json.dumps(listing))
    monkeypatch.setattr(installer, 'root_free', lambda: 50 * gib)
    answers = iter(['', '9', '2'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert installer.choose_data(80) == ('image', 'menu')  # two bad answers, then the image
    answers = iter(['/dev/vdb'])
    assert installer.choose_data(80) == ('format', '/dev/vdb')  # the path itself is accepted
    answers = iter(['x', 'x', 'x'])
    with pytest.raises(SystemExit, match='Nothing chosen'): installer.choose_data(80)
    monkeypatch.setattr(installer, 'run', lambda *a: json.dumps({'blockdevices': []}))
    assert installer.choose_data(80) == ('image', None)  # only the image: its own question asks
    monkeypatch.setattr(installer, 'root_free', lambda: 12 * gib)
    with pytest.raises(SystemExit, match='Free space, attach a volume'): installer.choose_data(80)


def test_the_forward_command_names_the_installing_account_and_the_servers_address(monkeypatch):
    assert installer.forward_command('admin', '144.126.207.172') == 'ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 admin@144.126.207.172'
    monkeypatch.setattr(installer, 'run', lambda *a: json.dumps([{'dst': '1.1.1.1', 'dev': 'eth0', 'prefsrc': '10.0.0.5'}]))
    assert installer.server_address() == '10.0.0.5'
    monkeypatch.setattr(installer, 'run', lambda *a: (_ for _ in ()).throw(subprocess.CalledProcessError(2, 'ip')))
    monkeypatch.setattr(installer.socket, 'gethostname', lambda: 'server')
    assert installer.server_address() == 'server'  # no default route: the hostname stands in
