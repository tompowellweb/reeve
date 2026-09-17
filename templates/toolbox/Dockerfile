# SITE_BASE is the selected site's pinned PHP image, or Debian trixie for static sites.
ARG SITE_BASE
FROM ${SITE_BASE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server bash git unzip 7zip ca-certificates \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
# Add your own packages and scanner installation here.
# The panel supplies WP-CLI and Composer when PHP is available, and the SSH public key at startup.
