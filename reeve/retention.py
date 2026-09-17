"""One retention policy for local and off-machine copies.

Database dumps are short-lived because every complete site backup carries a fresh dump.
Complete site backups keep everything recent, then thin to daily, weekly and monthly survivors.
The same keep set is computed for the local store and for the remote repository, so both
tiers agree on what survives; final and imported backups are never subject to it.
"""
import datetime

import yaml

from .compose_inspect import regular
from .host import OPS

DEFAULT = {'database_days': 2, 'site': {'within_days': 2, 'daily_days': 7, 'weekly_days': 31, 'monthly_days': 365}}
PROTECTED_KINDS = ('final', 'imported')


def policy():
    config = OPS / 'server.yaml'
    values = yaml.safe_load(regular(config)).get('retention', {}) if config.exists() else {}
    if not isinstance(values, dict) or values.keys() - DEFAULT.keys(): raise ValueError('Invalid retention policy')
    site = {**DEFAULT['site'], **(values.get('site') or {})}
    if set(site) - set(DEFAULT['site']): raise ValueError('Invalid site retention policy')
    result = {'database_days': values.get('database_days', DEFAULT['database_days']), 'site': site}
    for key, value in [('database_days', result['database_days']), *site.items()]:
        if type(value) is not int or not 0 <= value <= 3650: raise ValueError('Invalid retention value for ' + key)
    if result['database_days'] < 1: raise ValueError('Database dumps must be kept at least one day')
    return result


def describe(rule):
    site = rule['site']
    return ('database dumps ' + str(rule['database_days']) + ' days; complete site backups: all within '
            + str(site['within_days']) + ' days, daily to ' + str(site['daily_days']) + ' days, weekly to '
            + str(site['weekly_days']) + ' days, monthly to ' + str(site['monthly_days']) + ' days')


def keep_site_backups(entries, now, site_rule):
    """entries: dicts with id, completed_at, kind. Returns the ids that survive.

    Ages are what the operator thinks in: everything for `within_days`, one per day up to
    `daily_days`, one per ISO week up to `weekly_days`, one per month up to `monthly_days`,
    nothing older. The newest backup in each bucket is the survivor.
    """
    keep = set()
    candidates = []
    for entry in entries:
        if entry['kind'] in PROTECTED_KINDS or entry.get('completed_at') is None: keep.add(entry['id']); continue
        candidates.append(entry)
    candidates.sort(key=lambda e: e['completed_at'], reverse=True)
    seen = set()
    for entry in candidates:
        age = now - entry['completed_at']
        moment = datetime.datetime.fromtimestamp(entry['completed_at'])
        if age <= site_rule['within_days'] * 86400: keep.add(entry['id']); continue
        if age <= site_rule['daily_days'] * 86400: bucket = ('day', moment.date().isoformat())
        elif age <= site_rule['weekly_days'] * 86400: bucket = ('week', '%d-W%02d' % moment.isocalendar()[:2])
        elif age <= site_rule['monthly_days'] * 86400: bucket = ('month', moment.strftime('%Y-%m'))
        else: continue
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
