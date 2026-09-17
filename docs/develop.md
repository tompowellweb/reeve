# Develop

## Running the tests

```sh
uv sync --locked
umask 077; uv run --locked pytest -q
```

The suite runs in a few seconds and touches nothing outside temporary folders: commands are
faked, the ledger is a temporary SQLite file. Run it under `umask 077`, as the worker runs,
because file modes are part of what is tested. `uv.lock` pins the development environment;
`requirements.lock` pins, with hashes, what the installer puts on a server.

Browser acceptance scripts (`tests/browser_*.py`, Playwright) and whole-machine checks
(`tests/verify_*.py`) run on a test server as its browser identity or as root and alter only
named disposable fixtures. They are tooling, not the suite.

## Delivering a change

1. Commit and push. The installer takes a commit, never a working copy.
2. On the server: `git pull --ff-only`, then
   `sudo python3 install.py --commit <full hash>`.
3. The installer refuses a release whose worker cannot read the current ledger schema and keeps
   the previous release under `/opt/reeve/releases/` for rollback: check out the previous commit
   and install it. Rollback does not rewind content or job history.

A schema change bumps the ledger's version list in `config/capabilities.json` and the checks in
`reeve/core.py` and `install.py`, and says in its commit which older releases stop being rollback
targets. An additive table (traffic, remote copies) needs no bump: older workers ignore it.

## Rules for changing it

- **One mechanism, not variations.** Script the base case; a rare case gets a binding point, not
  a branch. A new operation that can fail half-way is a durable job with validate, apply, verify
  and rollback, following `reeve/php_settings.py`, not an ad hoc sequence.
- **The worker owns privilege.** The web process runs unprivileged and asks the worker over the
  socket with a fixed message shape; it never runs Docker, never reads site data. New worker
  operations are declared in the field table at the top of `reeve/worker.py`.
- **Files bind-mounted into running containers are updated in place**, never replaced by rename.
- **Every startup recovery runs under the guard** in `startup_recovery()`; a module that fails
  to recover is reported and leaves its job for review, and the worker still starts.
- **Nothing private in the tree.** No customer names, addresses, hostnames, keys or dumps. Test
  data uses example names and documentation addresses.
- **Prefer an exercise to a feature.** A change that touches backups, restore or recovery is
  proven by running the failure it guards against, on a test machine, before it is called done.

## Layout

- `reeve/`: the package. `worker.py` (the socket server and the loop), `web.py` and the `*_web.py`
  modules (routes), `core.py` (the ledger), `host.py` (the site and edge implementation), one
  module per feature, `templates/` and `static/`.
- `templates/`: Containerfiles and scripts for the PHP images, toolboxes, content tools and mail.
- `systemd/`: the three units and the timer. `config/`: the capabilities record and the settings
  example. `scripts/`: bootstrap and restore for a clean machine. `install.py`: the installer.
- `tests/`: the suite and the acceptance tooling.
