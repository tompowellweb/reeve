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
server. Apply the same retention policy locally and remotely; retain final and imported
backups. Changes to this path require an actual restore exercise on a test machine.

## Keep changes traceable

Deploy committed, tested releases and check database schema compatibility before updates or
rollback. Keep private customer data and credentials out of the repository and routine logs.
