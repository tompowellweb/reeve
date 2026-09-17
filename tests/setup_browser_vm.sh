#!/bin/sh
# Disposable browser identity on the test VM, isolated from the operator's trust store.
set -eu
test "$(id -u)" = 0
test -f /srv/ops/panel/worker/acceptance-password
if ! id hosting-browser >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/hosting-browser --shell /usr/sbin/nologin hosting-browser
fi
python3 -m venv /opt/hosting-browser
/opt/hosting-browser/bin/pip install playwright==1.62.0 greenlet==3.5.5 pyee==13.0.1 typing_extensions==4.16.0
install -d -m 0700 -o hosting-browser -g hosting-browser /var/lib/hosting-browser/.pki/nssdb
if ! test -f /var/lib/hosting-browser/.pki/nssdb/cert9.db; then
  runuser -u hosting-browser -- certutil -N --empty-password -d sql:/var/lib/hosting-browser/.pki/nssdb
fi
install -m 0644 /srv/ops/proxy/data/caddy/pki/authorities/local/root.crt /var/lib/hosting-browser/hosting-root.crt
runuser -u hosting-browser -- certutil -A -d sql:/var/lib/hosting-browser/.pki/nssdb -n hosting-local-ca -t 'C,,' -i /var/lib/hosting-browser/hosting-root.crt
install -m 0600 -o hosting-browser -g hosting-browser /srv/ops/panel/worker/acceptance-password /var/lib/hosting-browser/acceptance-password
echo 'Browser user prepared; normal operator trust stores unchanged.'
