# Engineering decisions

These constraints explain the main design choices. Keep detailed failure cases beside the
relevant code and regression tests.

## Keep privilege in the worker

The web process requests named operations with validated fields. It cannot run host commands
or read site data. New features must preserve that boundary.

## Make interruptions recoverable

Record operations before changing state. Preserve enough information to verify, retry or
roll back where supported. Never automatically replay imports or site commands: they may
already have changed application data. A recovery failure in one module must not prevent
the worker from starting.

## Preserve filesystem guarantees

Move staged content into a destination with the correct XFS project assignment; do not
rename a staging quota root into place. For a file bind-mounted into a running container,
an atomic rename leaves the container reading the old inode. Update it in place or mount
the parent directory and replace files within that directory.

## Respect application requirements

Compose hosting should preserve the application's declared build and runtime behaviour
within the host restrictions. Do not silently upgrade its images or apply managed-site
restrictions that break it.

## Prove restoration

Use engine-native database dumps and complete site backups that can restore on a clean
server. Keep them by count, with their own policy for the copies on the server and for the
repositories it copies to, since a repository deduplicates and a plain copy does not; retain
final, imported and operator-kept backups, and never let a copy here go before every
destination holds it. Changes to this path require an actual restore exercise on a test machine.

A newer release must always restore a backup written by an older one, so a server can be
rebuilt on the current release from its last backup and resume. Any change to the backup
format is proved by restoring an old backup before it ships.

## Record the operator's change first

What the operator asked for is recorded and visible the moment it is done: a domain change is
the names and the routes. Everything that follows, certificates, container restarts, checks,
is reported as status and can never undo the change. A step that gates on something outside
the server's control, such as DNS or a certificate authority, is a status too, with what it
said and a way to ask again.

## Bound helpers by reading, never by limits on the child

Helper output is bounded by how much the worker reads. A file-size limit on a child process
caps every file it writes, which broke git's first clone once the repository held images.
Git and archive steps run without it. The panel is never restarted inside a request it is
serving; the restart is scheduled to run after the reply.

## Keep XFS project quotas

The per-site disk limit is a project quota over the site's folder: it ignores file ownership,
which a site with root-owned configuration and database files under the image's user needs.
ext4 project quotas would change nothing, and btrfs subvolume quotas were rejected because
copy-on-write costs the databases too much.

## Never lock the operator out

Secure access is taken in steps that prove the path first: the tunnel is enabled without
blocking anything, the lockdown can only be taken from over the tunnel after a live
handshake, and it reverts by itself unless confirmed from there. A single-use unlock token,
logged by the edge and acted on by the worker, opens SSH to one address for a while.

## Keep changes traceable

Deploy committed, tested releases and check database schema compatibility before updates or
rollback. Keep private customer data and credentials out of the repository and routine logs.
