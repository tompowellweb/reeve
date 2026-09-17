"""The confined readers and bounded runner that package deployment, backups and recovery share."""
import os

import pytest

from reeve import compose_inspect as ci


@pytest.mark.parametrize('contents', ['include: /root/private.yaml', 'services:\n  web:\n    extends:\n      file: /root/private.yaml',
                                      'services:\n  web:\n    label_file: /root/private', 'recursive: &a [*a]', '- not a mapping', 'key: [unclosed'])
def test_unconfined_or_invalid_inputs_never_reach_compose(contents):
    with pytest.raises(ci.InspectionError): ci.safe_parse(contents.encode())


def test_native_merge_tags_parse_and_ordinary_projects_pass():
    model = ci.safe_parse(b'services:\n  web:\n    image: example/app:1\n    ports: !reset []\n    command: !override [run]\n')
    assert model['services']['web'] == {'image': 'example/app:1', 'ports': [], 'command': ['run']}


def test_inside_confines_paths_to_the_site_folder(tmp_path):
    root = tmp_path / 'site'; (root / 'data').mkdir(parents=True); (root / 'data/file').write_text('x')
    assert ci.inside(root, 'data/file') == root / 'data/file'
    assert ci.inside(root, str(root / 'data/file')) == root / 'data/file'
    assert ci.inside(root, 'absent', must_exist=False) == root / 'absent'
    with pytest.raises(ci.InspectionError, match='missing'): ci.inside(root, 'absent')
    with pytest.raises(ci.InspectionError, match='outside'): ci.inside(root, '../other')
    with pytest.raises(ci.InspectionError, match='outside'): ci.inside(root, '/etc/passwd')
    (root / 'data/escape').symlink_to('/etc')
    with pytest.raises(ci.InspectionError, match='symlink'): ci.inside(root, 'data/escape/passwd')


def test_regular_refuses_symlinks_hard_links_and_oversized_control_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, 'trusted', lambda *a, **k: None)
    target = tmp_path / 'compose.yaml'; target.write_text('services: {}\n')
    (tmp_path / 'link').symlink_to(target)
    with pytest.raises(ci.InspectionError, match='regular files'): ci.regular(tmp_path / 'link')
    os.link(target, tmp_path / 'hard')
    with pytest.raises(ci.InspectionError, match='hard-linked'): ci.regular(target)
    (tmp_path / 'hard').unlink()
    big = tmp_path / 'big'; big.write_bytes(b'x' * (ci.LIMIT + 1))
    with pytest.raises(ci.InspectionError, match='at most'): ci.regular(big)


def test_native_output_is_parsed_and_mutation_output_can_be_discarded():
    assert ci.run(['/usr/bin/printf', '%s', '{"native":true}']) == {'native': True}
    assert ci.run(['/usr/bin/printf', '%s', 'operation completed'], raw=True) is None
    with pytest.raises(ci.InspectionError): ci.run(['/usr/bin/printf', '%s', 'not-json'])
    with pytest.raises(ci.InspectionError, match='timed out'): ci.run(['/bin/sleep', '5'], timeout=0.2)
