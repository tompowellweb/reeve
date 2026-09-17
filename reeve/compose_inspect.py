"""Confined readers and a bounded Docker runner shared by package deployment, backups and recovery.

Control inputs are root-owned regular files read without following symlinks; the parser
precheck confines what Compose may read before native resolution. Output and diagnostics are
bounded and never returned raw, as they may contain interpolated secrets.
"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

import yaml

from .host import ENV, trusted

BASE_FILES = ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml')
LIMIT = 1048576


class InspectionError(ValueError):
    pass


class ComposeLoader(yaml.SafeLoader):
    pass


def tagged(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


for tag in ('!reset', '!override'):
    ComposeLoader.add_constructor(tag, tagged)


def run(args, *, raw=False, timeout=12):
    """Bound output/time; diagnostics may contain interpolated secrets, so never return them."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(['/usr/bin/prlimit', '--fsize=1048576:1048576', '--', *map(str, args)],
            stdout=out, stderr=err, env={**ENV, 'COMPOSE_DISABLE_ENV_FILE': 'true'}, cwd='/', start_new_session=True)
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise InspectionError('Inspection timed out. Check the project and Docker locally.') from None
        out.seek(0); err.seek(0)
        output, diagnostic = out.read(LIMIT), err.read(LIMIT)
    if code or len(output) == LIMIT or len(diagnostic) == LIMIT:
        raise InspectionError('Docker could not inspect this project. Run docker compose config locally to review its diagnostics.')
    if b'variable is not set' in diagnostic:
        raise InspectionError('A Compose interpolation variable is missing. Complete .env and inspect again.')
    if raw:
        return None
    try:
        if args[:3] in (['docker', 'container', 'ls'], ['docker', 'volume', 'ls']):
            return [json.loads(line) for line in output.splitlines() if line.strip()]
        return json.loads(output)
    except (ValueError, UnicodeError):
        raise InspectionError('Docker returned an invalid inspection result.') from None


def regular(path):
    trusted(path)
    info = path.lstat()
    if info.st_nlink != 1:
        raise InspectionError('Control files must not be hard-linked to other paths.')
    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
        raise InspectionError('Control inputs must be regular files of at most 1 MiB.')
    # O_NOFOLLOW prevents replacement with a symlink between validation and open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_mode & 0o022:
            raise InspectionError('Control input permissions changed during inspection.')
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise InspectionError('Control input grew during inspection. Inspect again after copying finishes.')
    return data


def load_yaml(raw):
    try:
        result = yaml.load(raw, Loader=ComposeLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise InspectionError('Invalid YAML. Finish preparing the project and inspect again.') from None
    if not isinstance(result, dict):
        raise InspectionError('Expected a YAML mapping in the project configuration.')
    return result


def safe_parse(raw):
    model = load_yaml(raw)
    # Compose can read includes, extends and label files even with --no-env-resolution.
    # Restrict these until their complete input graph can be confined and inventoried.
    seen = set()
    def walk(value, depth=0):
        if depth > 40 or id(value) in seen:
            raise InspectionError('Recursive or excessively nested YAML is unsupported.')
        if isinstance(value, (dict, list)):
            seen.add(id(value))
            if isinstance(value, dict):
                if any(key in value for key in ('include', 'extends', 'label_file')):
                    raise InspectionError('include, extends and label_file need a confined input inventory before inspection. Use local Compose merge files instead.')
                children = value.values()
            else:
                children = value
            for child in children:
                walk(child, depth + 1)
            seen.remove(id(value))
    walk(model)
    return model


def inside(root, value, *, must_exist=True):
    """No symlink components or escape; do not follow arbitrary prepared paths."""
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if '..' in path.parts or not path.is_relative_to(root):
        raise InspectionError('A referenced path is outside this site folder.')
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise InspectionError('A referenced path contains a symlink; its storage boundary needs review.')
    if must_exist and not path.exists():
        raise InspectionError('A referenced input or bind mount is missing. Finish copying the folder before adoption.')
    return path


def fingerprint(path, raw):
    info = path.stat()
    return {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': oct(stat.S_IMODE(info.st_mode)), 'bytes': len(raw)}
