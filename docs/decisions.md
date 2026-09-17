# Decisions

Rules the code follows on purpose. Most were learned from a real failure on a test server; each
is kept because removing it would bring that failure back. A change that breaks one of these
needs a better reason than tidiness.

## Filesystem and quotas

- Staging contents move into a destination assigned the same XFS project quota; the quota root
  is never renamed into the sites folder.
- Unlimited process limits accept Docker's `0` and `-1` representations and verify the kernel's
  `pids.max`.
- The local backup root is one folder named in the settings; artifacts are found by name under
  it, so moving it is a stop, move, edit, start.

## Jobs and retries

- Deployment retries preserve existing files, built images and containers; a restore never
  replays after completion.
- Every module's startup recovery runs under one guard in the worker; the half-minute backup
  work is one module-level function with a test. A lost import once stopped every schedule
  silently for a morning.
- Content operations, imports and site commands are never replayed automatically.
- A request id belongs to one set of inputs; the same id with different inputs is refused.

## Containers and the edge

- Runtime HTTP checks send the site hostname and allow a bounded warm-up.
- Package overlays add no capability hardening: a Compose application runs as its author wrote
  it, and only host hazards are refused.
- Archive steps never run under the 1 MiB helper output guard.
- Files and directories bind-mounted into running containers are updated in place or inside a
  mounted directory, never replaced by rename: a renamed file is a stale inode inside the
  container. Static sites carry their rules file; a restore that brings it back recreates the
  web container; static redirects use `absolute_redirect off`.
- Site rules are validated with `nginx -t` as the site identity before the file changes, and any
  failure after the change restores the previous text.
- PHP limits are the only source of `php.ini` and of nginx's body size and FastCGI timeout;
  every PHP template carries the body size.
- The edge's configuration is reconciled with the release's at worker start, validated and
  reloaded as a route change is.

## Databases

- The database inventory is saved before image preparation. A managed restore stages the dump
  under the database identity.
- The MySQL and MariaDB restore client selects the site database; a content import refuses a
  dump that selects another database.
- A MySQL site on PHP older than 7.2 gets a native-password user and server default, and only
  the 8.0 series.
- The binary log is off; recovery is the panel's dumps.

## Backups

- Backup imports validate the dump type before claiming the upload; downloads require a session.
- Retention tiers are age-bounded, the same locally and remotely.
- Remote command output is bounded by reading, never by a process file-size limit; site backups
  copy first. A remote copy cycle clears stale repository locks first.
- Quiesce markers are written before the first stop and only running containers are restarted.
- Delete removes every network labelled with the site operation and takes a final backup first.
- A restore to a new site clears the placeholder page before filling the content folder.
- An uploaded content archive that is a tar by its first header is read as a tar even when a
  `.zip` member sits near its end.
- A destination connect probes the host key once (a modern sshd penalises repeated
  unauthenticated connections) and its askpass helper reads the password beside itself, so the
  candidate trial works before the files become current.

## Access

- The SFTP server is SFTPGo, whose banner names the version that runs; the port stays open and
  a customer updates when they want. Generated key directories are chmod 755 explicitly, and the
  test suite runs under `umask 077` because the worker does.
- The web process cannot read the private settings; it shows what the worker's summary says.
- Secrets are shown once (a repository password, a database credential on request, an agent
  token) and never appear in routine status, logs or screenshots.

## Product

- One mechanism, not variations: script the base case; a rare case gets a binding point.
- A Compose application must not be worse off than manual hosting: accept it as supplied.
- Prefer an exercise to a feature: a change to backups, restore or recovery is proven by running
  the failure it guards against.
- Never install a tree that is not committed and tested; the installer takes a commit.
- Nothing private in the repository: no customer names, addresses, keys or data.
