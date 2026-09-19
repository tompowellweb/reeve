# Develop

Reeve uses Python 3.13 and uv. Run the following from the repository root.

## Tests

```sh
uv sync --locked
umask 077
uv run --locked pytest -q
```

The unit suite uses temporary files and simulated host commands. The restrictive umask
matches the worker and catches permission errors. `uv.lock` pins the development environment;
`requirements.lock` pins the packages installed on servers.

Browser scripts in `tests/browser_*.py` and machine checks in `tests/verify_*.py` need a
prepared test server and can change it. Read the selected script's requirements before running it.

## Where to work

| Path | Purpose |
| --- | --- |
| `reeve/worker.py` | Worker requests, scheduling and startup recovery |
| `reeve/web.py`, `reeve/*_web.py` | Web routes |
| `reeve/core.py`, `reeve/host.py` | Job storage and host operations |
| `reeve/settings.py`, `reeve/secure.py` | Server settings and secure access |
| `reeve/server_record.py`, `reeve/restoration.py` | The server record and multi-site recovery |
| `reeve/certificates.py`, `reeve/site_logs.py` | Certificate status and log reading |
| `reeve/templates/`, `reeve/static/` | Interface templates and assets |
| `templates/` | Container build recipes and helper scripts |
| `install.py`, `reeve/setup.py`, `systemd/` | Installation and services |
| `config/`, `tests/` | Configuration and verification |

Feature modules live under `reeve/`. Follow `reeve/php_settings.py` for an example of a
recorded operation with validation and rollback. Register new worker requests in the field
table in `reeve/worker.py`, and put startup recovery under the existing recovery guard.
See [Design](design.md) and [Engineering decisions](decisions.md) for the constraints.

## Prepare a release

1. Run relevant tests. Exercise host changes on a test server; backup or restore changes
   require a restore exercise.
2. Commit the tested changes. For a release, tag the commit `vX.Y.Z` and push the commit and tag.
   Never move a published tag; a mistake gets the next number.
3. Install the selected commit on the test server with `sudo python3 install.py`, or install
   the published version with `sudo reeve update --to <version>`.
4. Check service health and the behaviour changed by the release.

The installer rejects a dirty checkout unless `--allow-modified` is supplied, which marks
the installation as modified and carries changed tracked files only, not new untracked ones.
Use committed releases for deployment; commit before installing on a test machine.

Throwaway VMs are the usual proof: a Debian cloud image with a cloud-init seed, the tree copied
in, `install.py`, the change exercised through the panel or the command line, the VM destroyed.
Secure access needs a second VM as the WireGuard client.

## Schema changes and rollback

The installer checks whether a release can read the existing worker database. A schema change
must review `config/capabilities.json`, `reeve/core.py` and `install.py`, and document any
versions that can no longer be used for rollback. Additive tables that older workers can
safely ignore do not require a schema bump.

Use `sudo reeve update --to <previous-version>` to return to a compatible release.
This changes the software; it does not rewind site content or job history.
