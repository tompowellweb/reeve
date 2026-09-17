#!/bin/sh
# The relay's generated configuration is mounted read-only under /run/mail; Postfix wants its
# files under /etc/postfix, so they are copied in at start. The queue is a bind mount.
set -eu
cp /run/mail/main.cf /etc/postfix/main.cf
cp /run/mail/master.cf /etc/postfix/master.cf
rm -rf /etc/postfix/hosting && mkdir -p /etc/postfix/hosting && cp /run/mail/maps/* /etc/postfix/hosting/
chmod 644 /etc/postfix/main.cf /etc/postfix/master.cf /etc/postfix/hosting/*
# The log lives in the persisted spool, so the mail page's history survives restarts and setups.
mkdir -p /var/spool/postfix/hosting-log && chown postfix:postfix /var/spool/postfix/hosting-log && chmod 755 /var/spool/postfix/hosting-log
exec postfix start-fg
