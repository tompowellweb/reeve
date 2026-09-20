"""Server settings as the panel shows and changes them: the groups of `server.yaml` an operator is meant to
touch, read for the Settings page and written back through the worker.

Each group is validated by the reader that consumes it (certificates by `tls_settings`, mail by
`mail.settings` and so on) against the whole document as it would be after the change, so nothing the
panel writes can be a file the worker refuses to read. Keys the panel does not manage stay as they are.
After a write, the consequence is applied at once and named in the result: the edge is rebuilt for
certificates, the relay redeployed for mail; the profile, backups and updates apply from their next use.
"""
import copy
import ipaddress
import re
import shutil

import yaml

from .host import OPS, PROXY, atomic, command, trusted, tls_settings, backup_root

GROUPS = ('certificates', 'mail', 'profile', 'backups', 'updates')


def document():
    """The whole of server.yaml as a mapping (root only)."""
    path = OPS / 'server.yaml'
    if not path.exists(): return {}
    trusted(path)
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict): raise ValueError('server.yaml is not a mapping')
    return loaded


def read(loaded=None):
    """Every group with its current values, for the page. Choices are listed so the form needs no other source."""
    from . import mail, profile, retention, site_backup, php_updates
    loaded = document() if loaded is None else loaded
    tls = tls_settings(loaded); mail_values = mail.settings(loaded); keep = retention.policy(loaded)
    return {
        'certificates': {'mode': tls['mode'], 'email': tls['email'], 'modes': ['internal', 'public']},
        'mail': {k: mail_values[k] for k in ('mode', 'relayhost', 'hostname', 'public_ip', 'rate_per_hour')} | {'modes': ['off', 'direct', 'relay', 'sink']},
        'profile': {'name': profile.name(loaded), 'profiles': {k: v['label'] for k, v in profile.PROFILES.items()}},
        'backups': {'local_path': str(backup_root(loaded)), 'hour': site_backup.policy(loaded)['hour'], 'database_days': keep['database_days'],
                    **{scope + '_' + tier: keep[scope][tier] for scope in ('local', 'remote') for tier in keep[scope]}},
        'updates': dict(php_updates.policy(loaded)),
    }


def merge(loaded, group, values):
    """Pure: the document with one group's values from the form applied, other keys untouched. Form values
    arrive as text; numbers are converted here and refused when not whole."""
    if group not in GROUPS: raise ValueError('Unknown settings group')
    new = copy.deepcopy(loaded)
    def number(key, text):
        text = str(text).strip()
        if not re.fullmatch(r'-?\d+', text): raise ValueError(key + ' must be a whole number')
        return int(text)
    if group == 'certificates':
        new['tls'] = {'mode': str(values.get('mode', 'internal')).strip(), 'email': str(values.get('email', '')).strip()}
    elif group == 'mail':
        current = dict(new.get('mail') or {})
        current.update(mode=str(values.get('mode', 'direct')).strip(), relayhost=str(values.get('relayhost', '')).strip(),
                       hostname=str(values.get('hostname', '')).strip().lower(), public_ip=str(values.get('public_ip', '')).strip(),
                       rate_per_hour=number('rate_per_hour', values.get('rate_per_hour', 100)))
        new['mail'] = current
    elif group == 'profile':
        new['profile'] = str(values.get('name', 'standard')).strip()
    elif group == 'backups':
        new['backups'] = {**(new.get('backups') or {}), 'local_path': str(values.get('local_path', '/srv/backups')).strip()}
        new['site_backups'] = {**(new.get('site_backups') or {}), 'hour': number('hour', values.get('hour', 3))}
        from .retention import TIERS, DEFAULT as RETENTION
        new['retention'] = {'database_days': number('database_days', values.get('database_days', 2)),
                            **{scope: {tier: number(scope + '_' + tier, values.get(scope + '_' + tier, RETENTION[scope][tier])) for tier in TIERS} for scope in ('local', 'remote')}}
    elif group == 'updates':
        new['updates'] = {'hour': number('hour', values.get('hour', 4)), 'every_days': number('every_days', values.get('every_days', 7))}
    read(new)   # every reader validates the document as it would be; a ValueError here changes nothing
    return new


def write(group, values):
    """Merge, validate, write server.yaml atomically. Returns the new document."""
    loaded = document()
    new = merge(loaded, group, values)
    path = OPS / 'server.yaml'
    atomic(path, yaml.safe_dump(new, sort_keys=False), 0o600)
    return new


def apply(host, ledger, group, before, after):
    """The consequence of a change, applied now and described. Certificates: the edge takes the new mode; a
    switch to public forgets the edge's own certificates so every name asks the public issuer at once.
    Mail: the relay is redeployed with the new settings. The rest apply from their next use."""
    from . import server_record
    note = ''
    if group == 'certificates':
        from .host import reconcile_edge
        reconcile_edge(host)
        if tls_settings(after)['mode'] == 'public' and tls_settings(before)['mode'] != 'public':
            local = PROXY / 'data/caddy/certificates/local'
            if local.is_dir() and not local.is_symlink(): shutil.rmtree(local)
            if host.inspect('hosting-edge'): command(['docker', 'restart', 'hosting-edge'], timeout=60)
            note = 'The edge restarted and every name is asking Let\'s Encrypt for a certificate; each site\'s Domains section shows the outcome.'
        else:
            note = 'The edge took the new certificate mode.'
    elif group == 'mail':
        from .mail import settings as mail_settings, setup
        if mail_settings(after)['mode'] == 'off': note = 'Outbound mail is off; the relay stops at the next mail setup.'
        else:
            setup(host, ledger); note = 'The mail relay was redeployed with the new settings.'
    elif group == 'profile': note = 'The profile sets the defaults for sites created from now on; existing sites keep their values.'
    elif group == 'backups': note = 'Backup times and retention apply from the next run. A changed local path needs the folder moved with the services stopped.'
    elif group == 'updates': note = 'The PHP rebuild schedule applies from its next run.'
    try: server_record.write(ledger)
    except Exception: pass   # the record follows the settings; a failure to write it must not undo a saved setting
    return note


def surplus(ledger, values):
    """What a candidate backups policy would remove of the existing backups: the complete backups here and the
    repository copies, with counts and bytes, so the operator confirms before anything goes."""
    from . import retention, site_backup, remote_backup
    rule = retention.policy(merge(document(), 'backups', values))
    local = site_backup.local_surplus(ledger, rule['local'])
    remote = [item for config in remote_backup.destinations() for item in remote_backup.remote_surplus(ledger, config, rule['remote'])]
    return {'local': {'count': len(local), 'bytes': sum(j['bytes'] for j in local), 'ids': [j['id'] for j in local]},
            'remote': {'count': len(remote), 'bytes': sum(j['bytes'] for j in remote), 'ids': [j['id'] for j in remote]}}


def save(host, ledger, group, values):
    before = document()
    kept = 0
    if group == 'backups' and values.get('existing') == 'keep':
        # The operator wants the new counts from now on but the backups already here to stay: mark them kept
        # before the policy that would remove them is written. Manage on Recover lets them go later.
        from .site_backup import mark_kept
        found = surplus(ledger, values)
        idents = sorted(set(found['local']['ids']) | set(found['remote']['ids']))
        mark_kept(ledger, idents); kept = len(idents)
    after = write(group, {k: v for k, v in values.items() if k != 'existing'})
    note = apply(host, ledger, group, before, after)
    if kept: note = str(kept) + ' existing backup' + ('' if kept == 1 else 's') + ' marked kept; the new counts apply to the rest. ' + note
    return {'group': group, 'saved': read(after)[group], 'note': note}


def restore(host, ledger, recorded):
    """A recorded server's settings applied to this one: every managed group taken from the record, validated as
    one document, written, and the consequences applied (certificates, mail). Keys the record carries that this
    release does not manage are kept too, so nothing an operator set is lost on a rebuild."""
    if not isinstance(recorded, dict): raise ValueError('No settings recorded')
    before = document()
    new = copy.deepcopy(before)
    for key, value in recorded.items():
        if key in EXCLUDED: continue
        new[key] = copy.deepcopy(value)
    read(new)
    atomic(OPS / 'server.yaml', yaml.safe_dump(new, sort_keys=False), 0o600)
    notes = []
    for group in ('certificates', 'mail'):
        try: notes.append(apply(host, ledger, group, before, new))
        except Exception as exc: notes.append(group + ': ' + str(exc)[:300])
    return {'applied': sorted(k for k in recorded if k not in EXCLUDED), 'notes': notes}


EXCLUDED = ('schema',)
