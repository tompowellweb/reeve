"""The short preparation instructions shared by the panel and agent endpoint."""
PROMPT = """Supply one Compose project that already runs with `docker compose up` in a folder.

1. Archive (ZIP or tar) the folder: compose.yaml or docker-compose.yml, its files and
   configuration, relative paths kept. Use registry images or include each Dockerfile
   with its build context. No image tarballs, no raw database volumes.
2. Say which service serves HTTP and on which container port; give a site name and domain.
3. Optional: put logical database dumps in dumps/, named after the database service:
   dumps/<service>.sql or .sql.gz for MySQL/MariaDB, dumps/<service>.dump or .sql for
   PostgreSQL. The panel restores them once into the fresh database before the application
   starts. Include the application's own dump; do not substitute seed data.
4. Set application settings any host would set (allowed hostnames, public URL) for the
   given domain, served over HTTPS.

The panel strips published ports and container names, routes Caddy to the chosen
service, keeps everything else as written, and refuses only host hazards: privileged,
host namespaces, Docker socket, devices, cap_add, mounts outside the project, external
volumes. Images run as they ship. Recognised database images get scheduled native
dumps; other engines are backed up as files only.
"""


def instructions():
    from .application_package import UPLOAD_LIMIT, EXPANDED_LIMIT, ENTRY_LIMIT
    return {'version': 1, 'prompt': PROMPT,
            'submission_url': '/api/v1/imports', 'status_url': '/api/v1/imports/{id}',
            'format': 'ZIP or tar archive; no manifest required',
            'fields': ['name', 'domain', 'service', 'port'],
            'capabilities': {'receive': True, 'inspect_package': True, 'deploy': False,
                             'operator_deploy': 'any Compose project; hazard checks only; dumps/ restored once',
                             'whole_site_backup': False, 'restore': False},
            'limits': {'upload_bytes': UPLOAD_LIMIT, 'expanded_bytes': EXPANDED_LIMIT,
                       'entries': ENTRY_LIMIT}}
