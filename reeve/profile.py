"""The server profile: one word in server.yaml that sets the defaults for the size of the box.

`profile: small | standard | large`. A clean install is `standard`. A profile is only a set of
defaults, every one overridable per site as before: the PHP memory a request may take and the
workers a site's pool may run, the memory cap a new site gets, and the usage a new database gets
(see `database_site.USAGES`). MariaDB is offered first whatever the profile. The home page shows
the profile beside the hostname. Nothing here is a limit on the number of sites: a small box is
expected to host a few, and the defaults keep each one modest.
"""
import yaml

from .host import OPS

PROFILES = {
    'small': {'label': 'small (2 to 4 GB)', 'php_memory_limit_mb': 256, 'php_workers': 3, 'site_memory_mb': 768, 'database_usage': 'light'},
    'standard': {'label': 'standard (8 to 16 GB)', 'php_memory_limit_mb': 512, 'php_workers': 8, 'site_memory_mb': None, 'database_usage': 'standard'},
    'large': {'label': 'large (32 GB and up)', 'php_memory_limit_mb': 512, 'php_workers': 8, 'site_memory_mb': None, 'database_usage': 'high'},
}


def name():
    """The configured profile; `standard` when unset. The web process cannot read the private
    settings and only displays what the worker's summary says, so it sees the default."""
    config = OPS / 'server.yaml'
    try:
        value = (yaml.safe_load(config.read_text()) or {}).get('profile', 'standard') if config.exists() else 'standard'
    except (OSError, yaml.YAMLError):
        return 'standard'
    if value not in PROFILES: raise ValueError('profile must be small, standard or large')
    return value


def settings():
    return {'name': name(), **PROFILES[name()]}
