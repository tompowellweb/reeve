"""Two retention policies for complete site backups, one for the copies here and one for every restic
repository they are copied to, and one window for database dumps.

Database dumps are short-lived because every complete site backup carries a fresh dump. Complete site
backups thin to counts, in restic's words: the newest backup of each of the last N days that have one,
of the last N weeks, of the last N months. Counts bound what the operator is managing, copies times site
size, where ages bound only time. Zero turns a tier off; at least one daily is always kept so the hourly
copy has something to take. Final and imported backups, and any backup the operator chose to keep, are
never subject to either policy.
"""
import datetime

import yaml

from .compose_inspect import regular
from .host import OPS

DEFAULT = {'database_days': 2, 'local': {'daily': 2, 'weekly': 0, 'monthly': 0}, 'remote': {'daily': 7, 'weekly': 4, 'monthly': 12}}
TIERS = ('daily', 'weekly', 'monthly')
PROTECTED_KINDS = ('final', 'imported')


def counts_from_days(site):
    """The policy as it was written until 1.4.8, ages in days, as counts that keep about as much."""
    daily = max(1, int(site.get('daily_days', 7)), int(site.get('within_days', 2)))
    return {'daily': daily, 'weekly': int(site.get('weekly_days', 31)) // 7, 'monthly': int(site.get('monthly_days', 365)) // 30}


def policy(document=None):
    config = OPS / 'server.yaml'
    values = (document if document is not None else (yaml.safe_load(regular(config)) if config.exists() else {})).get('retention', {})
    if not isinstance(values, dict) or values.keys() - {'database_days', 'local', 'remote', 'site'}: raise ValueError('Invalid retention policy')
    if 'site' in values and not isinstance(values['site'], dict): raise ValueError('Invalid site retention policy')
    legacy = counts_from_days(values['site']) if 'site' in values else None
    result = {'database_days': values.get('database_days', DEFAULT['database_days'])}
    for scope in ('local', 'remote'):
        given = values.get(scope)
        if given is None: given = legacy if legacy is not None else DEFAULT[scope]
        if not isinstance(given, dict) or set(given) - set(TIERS): raise ValueError('Invalid ' + scope + ' retention policy')
        result[scope] = {**DEFAULT[scope], **given}
        for key, value in result[scope].items():
            if type(value) is not int or not 0 <= value <= 3650: raise ValueError('Invalid retention value for ' + scope + ' ' + key)
        if result[scope]['daily'] < 1: raise ValueError('Keep at least one daily backup ' + ('here' if scope == 'local' else 'in the repositories'))
    if type(result['database_days']) is not int or not 1 <= result['database_days'] <= 3650: raise ValueError('Database dumps must be kept at least one day')
    return result


def describe_counts(counts):
    parts = [str(counts[t]) + ' ' + t for t in TIERS if counts[t]]
    return ', '.join(parts) if parts else 'nothing'


def describe(rule):
    return ('database dumps ' + str(rule['database_days']) + ' days; complete site backups: here the newest of the last '
            + describe_counts(rule['local']) + '; in the repositories the newest of the last ' + describe_counts(rule['remote']))


def keep_site_backups(entries, now, counts):
    """entries: dicts with id, completed_at, kind and optionally kept. Returns the ids that survive.

    Counts are what the operator is bounding: for each tier, the newest backup of each of the last N
    days, ISO weeks or months that have one. Final, imported and kept backups always survive, as does
    a backup whose time is unknown.
    """
    keep = set()
    candidates = []
    for entry in entries:
        if entry['kind'] in PROTECTED_KINDS or entry.get('kept') or entry.get('completed_at') is None: keep.add(entry['id']); continue
        candidates.append(entry)
    candidates.sort(key=lambda e: e['completed_at'], reverse=True)
    for tier in TIERS:
        wanted = counts.get(tier, 0); seen = set()
        for entry in candidates:
            if len(seen) >= wanted: break
            moment = datetime.datetime.fromtimestamp(entry['completed_at'])
            bucket = moment.date().isoformat() if tier == 'daily' else '%d-W%02d' % moment.isocalendar()[:2] if tier == 'weekly' else moment.strftime('%Y-%m')
            if bucket in seen: continue
            seen.add(bucket); keep.add(entry['id'])
    return keep


def keep_dumps(entries, now, days):
    """entries: dicts with id, site_id, created. Keeps everything within the window and the newest per site."""
    keep = set(); newest = {}
    for entry in entries:
        if now - entry['created'] <= days * 86400: keep.add(entry['id'])
        if entry['site_id'] not in newest or entry['created'] > newest[entry['site_id']]['created']: newest[entry['site_id']] = entry
    keep.update(e['id'] for e in newest.values())
    return keep
