"""Managed PHP template additions, preserving site-owned configuration on retry."""
import json
from pathlib import Path

import yaml

from .host import SITES, atomic, command, trusted
from .php_runtime import ensure


def keep_file(path, contents, mode=0o644):
    if path.exists() or path.is_symlink():
        trusted(path)
    else:
        atomic(path, contents, mode)


def nginx(base, settings=None):
    # Specific protection rules precede the PHP handler. Existing .php files go only
    # to FastCGI; no fallback ever serves them as text when FPM is stopped.
    from .php_settings import defaults, nginx_directives
    limits = nginx_directives(settings or defaults({}))
    text = base.replace("index index.html;", "index index.php index.html;\n        " + limits['body'] + "\n        fastcgi_connect_timeout 2s;\n        include /etc/hosting/site.nginx.conf;")
    text = text.replace('location = /__hosting_health { access_log off; return 200 "healthy\\n"; }', '''location = /__hosting_health {
            access_log off;
            fastcgi_pass php:9000;
            fastcgi_param REQUEST_METHOD GET;
            fastcgi_param SCRIPT_NAME /__hosting_fpm_ping;
            fastcgi_param SCRIPT_FILENAME /__hosting_fpm_ping;
        }''')
    text = text.replace('        location ~* \\.(php[s0-9]*', '''        location ~* (^|/)(wp-config\\.php|settings\\.php)(/|$) { return 404; }
        location ~* ^/(uploads|wp-content/uploads|sites/[^/]+/files)/.*\\.php { return 404; }
        location ~ \\.php$ {
            try_files $uri =404;
            include fastcgi_params;
            fastcgi_param SCRIPT_FILENAME /site$fastcgi_script_name;
            fastcgi_param HTTPS on;
            fastcgi_param SERVER_PORT 443;
            fastcgi_param HTTP_PROXY "";
            ''' + limits['timeout'] + '''
            fastcgi_pass php:9000;
        }
        location ~* \\.(php[s0-9]*''')
    return text.replace('try_files $uri $uri.html $uri/ =404;', 'try_files $uri $uri/ /index.php?$query_string;')


def prepare(root, data, row, metadata, nginx_base):
    conf = root / "conf"
    prior = yaml.safe_load((root / "hosting.yaml").read_text()) if (root / "hosting.yaml").exists() else {}
    runtime = prior.get("php_runtime") or ensure(data["php_version"])
    if not runtime:
        raise RuntimeError("This PHP branch is not built; run the installer runtime build")
    actual = command(["docker", "image", "inspect", runtime["image"], "--format", "{{.Id}} "]).strip()
    if actual != runtime["image_id"]:
        raise RuntimeError("Pinned PHP base image changed; restore its recorded artifact before retrying")
    # Pin the selected runtime before building, including interruption before the build completes.
    metadata["php_runtime"] = {k: v for k, v in runtime.items() if k != "packages"}
    atomic(root / "hosting.yaml", yaml.safe_dump(metadata))
    keep_file(conf / "Containerfile", "ARG PHP_BASE\nFROM ${PHP_BASE}\n")
    keep_file(conf / ".dockerignore", "*\n!Containerfile\n")
    keep_file(conf / "site.nginx.conf", "# Site-specific nginx locations and rewrite rules. Preserved on retry.\n")
    keep_file(root / ".env", "# Site-specific environment. Preserved on retry.\n", 0o600)
    from .php_settings import budget, effective, render_ini
    workers = budget(data)[0]
    settings = effective(metadata, data)
    keep_file(conf / "php.ini", render_ini(settings))
    keep_file(conf / "php-fpm.conf", "[global]\npid=/tmp/php-fpm.pid\nerror_log=/proc/self/fd/2\ndaemonize=no\ninclude=/etc/hosting/pool.conf\n")
    keep_file(conf / "pool.conf", f'''[site]
user={row['uid']}
group={row['uid']}
listen=9000
pm=ondemand
pm.max_children={workers}
pm.process_idle_timeout=10s
pm.max_requests=500
catch_workers_output=yes
clear_env=no
chdir=/site
security.limit_extensions=.php
ping.path=/__hosting_fpm_ping
ping.response=healthy
''')
    atomic(conf / "nginx.conf", nginx(nginx_base, settings), 0o644)
    site_image = "hosting-php-site:" + row["id"]
    # Builds execute inside Docker's build environment, never uploaded commands on the host.
    command(["docker", "build", "--build-arg", "PHP_BASE=" + runtime["image"], "--tag", site_image,
             "--file", conf / "Containerfile", conf], timeout=600)
    site_id = command(["docker", "image", "inspect", site_image, "--format", "{{.Id}} "]).strip()
    metadata["php_image"] = {"name": site_image, "id": site_id}
    atomic(root / "hosting.yaml", yaml.safe_dump(metadata))
    return site_image


def compose_services(compose, root, data, row, site_image, backend):
    web = compose["services"]["web"]
    if data.get("memory_mb") is not None:
        web["mem_limit"] = "32m"
    if data.get("cpus") is not None:
        web["cpus"] = round(data["cpus"] / 4, 4)
    web["networks"]["backend"] = {}
    php = {key: web[key] for key in ("user", "restart", "cap_drop", "security_opt", "pids_limit", "storage_opt", "logging", "labels")}
    php.update({"image": site_image, "container_name": "hosting-php-" + row["name"],
        "env_file": [str(root / ".env")],
        "volumes": [f"{root}/html:/site", f"{root}/conf/php-fpm.conf:/etc/hosting/php-fpm.conf:ro",
            f"{root}/conf/pool.conf:/etc/hosting/pool.conf:ro",
            f"{root}/conf/php.ini:/etc/php/{data['php_version']}/fpm/conf.d/99-hosting.ini:ro",
            f"{root}/conf/php.ini:/etc/php/{data['php_version']}/cli/conf.d/99-hosting.ini:ro",
            *([shim] if (shim := __import__('reeve.mail', fromlist=['shim_mount']).shim_mount()) else [])],
        "networks": {"backend": {"aliases": ["php"]}},
        "healthcheck": {"test": ["CMD-SHELL", "SCRIPT_NAME=/__hosting_fpm_ping SCRIPT_FILENAME=/__hosting_fpm_ping REQUEST_METHOD=GET cgi-fcgi -bind -connect 127.0.0.1:9000 | grep -q healthy"],
                        "interval": "2s", "timeout": "2s", "retries": 10}})
    if data.get("memory_mb") is not None:
        php["mem_limit"] = f"{data['memory_mb'] - 32}m"
    if data.get("cpus") is not None:
        php["cpus"] = round(data["cpus"] * 3 / 4, 4)
    compose["services"]["php"] = php
    web["depends_on"] = {"php": {"condition": "service_healthy"}}
    compose["networks"]["backend"] = {"external": True, "name": backend}


def add_shim_mount(host, row):
    """Mount the relay's sendmail shim into an existing site's PHP container; True when it was recreated."""
    from .mail import shim_mount
    shim = shim_mount()
    root = SITES / row['name']
    compose_path = root / 'compose.yml'
    if not shim or not compose_path.exists(): return False
    trusted(compose_path)
    compose = yaml.safe_load(compose_path.read_text())
    php = compose.get('services', {}).get('php')
    if not php: return False
    if shim in php.get('volumes', []):
        # Mounted already: recreate only if the container still sees an older copy (a bind mount keeps
        # the inode it was created with).
        try: inside = command(['docker', 'exec', php['container_name'], 'cat', '/usr/local/bin/hosting-sendmail'])
        except RuntimeError: inside = None
        if inside == Path(shim.split(':')[0]).read_text(): return False
    else:
        php.setdefault('volumes', []).append(shim)
        atomic(compose_path, yaml.safe_dump(compose))
    command(['docker', 'compose', '-f', str(compose_path), 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'php'], timeout=120)
    if php.get('pids_limit') == -1: command(['docker', 'update', '--pids-limit', '-1', php['container_name']])
    return True
