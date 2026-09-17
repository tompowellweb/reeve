"""Root-only hosting implementation. Inputs come only from the validated ledger."""
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from .core import DEFAULTS, validate_create

ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
       "DOCKER_CONFIG": "/srv/ops/panel/worker/docker-config"}
OPS = Path("/srv/ops")
SITES = Path("/srv/sites")
PROXY = OPS / "proxy"


def backup_root():
    """Where local backups live: `backups.local_path` in server.yaml, `/srv/backups` by default.

    An absolute path with plain components; the installer creates it and the worker's modules
    take their staging folders from it. Moving it is an operator step (stop the services, move
    the folder, change the setting, start), because artifacts are found by name under it."""
    default = Path('/srv/backups')
    config = OPS / 'server.yaml'
    try:
        values = (yaml.safe_load(config.read_text()) or {}).get('backups', {}) if config.exists() else {}
    except (OSError, yaml.YAMLError):
        return default  # the web process cannot read the private settings; it never touches the folder
    if not isinstance(values, dict) or values.keys() - {'local_path'}: raise ValueError('Invalid backups settings in server.yaml')
    path = values.get('local_path', str(default))
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*', path):
        raise ValueError('backups.local_path must be an absolute path of plain components')
    return Path(path)


BACKUPS = backup_root()


def command(args, timeout=120):
    # A disk file bounds memory, a child file-size limit bounds noisy helper output.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(["/usr/bin/prlimit", "--fsize=1048576:1048576", "--", *[str(x) for x in args]],
                                stdout=output, stderr=subprocess.STDOUT,
                                env=ENV, cwd="/", start_new_session=True)
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise RuntimeError(f"{args[0]} timed out; retry reconciles retained resources") from None
        output.seek(0)
        text = output.read(1048576).decode(errors="replace")
    if code:
        raise RuntimeError(f"{args[0]} failed ({code}): {text[-1200:]}")
    return text


def trusted(path, directory=False):
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"Unsafe ownership, permissions or symlink: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Expected directory: {path}")


def atomic(path, data, mode=0o600):
    path = Path(path)
    trusted(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        trusted(path)
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def preflight():
    mount = json.loads(command(["findmnt", "-J", "-M", "/srv"]))["filesystems"][0]
    if mount["fstype"] != "xfs" or not ({"prjquota", "pquota"} & set(mount["options"].split(","))):
        raise RuntimeError("/srv must be a mounted XFS filesystem with project quotas")
    state = command(["xfs_quota", "-x", "-c", "state -p", "/srv"])
    if "Accounting: ON" not in state or "Enforcement: ON" not in state:
        raise RuntimeError("XFS project quota enforcement is not active")
    info = json.loads(command(["docker", "info", "--format", "{{json .}} "]))
    if info["Driver"] != "overlay2" or info["DockerRootDir"] != "/srv/docker":
        raise RuntimeError("Require the tested Docker overlay2 engine under /srv/docker")
    for path in (Path("/srv"), OPS, SITES):
        trusted(path, directory=True)
    command(["docker", "compose", "version"])
    return {"mount": mount, "engine": info["ServerVersion"], "free_bytes": os.statvfs("/srv").f_bavail * os.statvfs("/srv").f_frsize}


def quota_record(project):
    output = command(["xfs_quota", "-x", "-c", "report -p -n -b", "/srv"])
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] == f"#{project}":
            return {"used_bytes": int(fields[1]) * 1024, "hard_bytes": int(fields[3]) * 1024}
    raise RuntimeError(f"No quota record for project {project}")


def project_id(path):
    output = command(["xfs_io", "-r", "-c", "stat", path])
    match = re.search(r"fsxattr.projid\s*=\s*(\d+)", output)
    if not match:
        raise RuntimeError("Cannot inspect XFS project identity")
    return int(match.group(1)), output


def verify_quota(path, project, mb, inherit=True):
    observed, output = project_id(path)
    flags = re.search(r"fsxattr.xflags\s*=\s*(0x[0-9a-fA-F]+)", output)
    if observed != project or (inherit and (not flags or not int(flags.group(1), 16) & 0x200)):
        raise RuntimeError(f"XFS quota identity/inheritance missing on {path}")
    record = quota_record(project)
    if record["hard_bytes"] != mb * 1048576:
        raise RuntimeError(f"Incorrect hard quota on {path}")
    return record


def apply_quota(path, project, mb):
    command(["xfs_quota", "-x", "-c", f"project -s -p {path} {project}", "/srv"])
    command(["xfs_quota", "-x", "-c", f"limit -p bsoft=0 bhard={mb}m {project}", "/srv"])
    verify_quota(path, project, mb)


NGINX = r'''worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 256; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;
    server_tokens off;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    server {
        listen 8080;
        root /site;
        index index.html;
        disable_symlinks on;
        location = /__hosting_health { access_log off; return 200 "healthy\n"; }
        location ~ (^|/)\. { return 404; }
        location ~* \.(php[s0-9]*|phtml|phar|inc|sql|sqlite[0-9]*|env|ini|conf|ya?ml|toml|log|bak|old|orig|swp|key|pem)([./~]|$) { return 404; }
        location ~* (^|/)(Dockerfile|Containerfile|compose\.ya?ml|docker-compose(?:\.[^/]*)?\.ya?ml|hosting\.ya?ml|composer\.(json|lock)|package(-lock)?\.json)(/|$) { return 404; }
        location / { try_files $uri $uri.html $uri/ =404; }
    }
}
'''



def static_nginx():
    """The static template with the site's own rules file included before the generic locations."""
    # Behind Caddy, redirects must not carry nginx's own scheme and listen port.
    return NGINX.replace("index index.html;", "index index.html;\n        absolute_redirect off;\n        include /etc/hosting/site.nginx.conf;")


def container_limits(data):
    # Explicit zero overrides the engine's historical 1 GiB overlay2 default.
    # The real layer project is checked before publication even when unlimited.
    limits = {"storage_opt": {"size": f"{data['layer_mb']}m" if data.get("layer_mb") is not None else "0"},
              "pids_limit": data.get("pids_limit") or -1}
    if data.get("memory_mb") is not None:
        limits["mem_limit"] = f"{data['memory_mb']}m"
    if data.get("cpus") is not None:
        limits["cpus"] = data["cpus"]
    return limits


class Host:
    def __init__(self):
        trusted(OPS / "server.yaml")
        self.config = yaml.safe_load((OPS / "server.yaml").read_text())
        if self.config["schema"] != 1:
            raise RuntimeError("Unsupported server configuration schema")
        from .profile import settings as profile_settings
        profile = profile_settings()
        # The profile's site memory cap applies unless server.yaml sets its own value (null is "unset").
        chosen = {k: v for k, v in self.config.get("defaults", {}).items() if v is not None}
        settings = {**DEFAULTS, **({"memory_mb": profile["site_memory_mb"]} if profile["site_memory_mb"] else {}), **chosen}
        validated = validate_create({"name": "policy-check", "domain": "policy-check.hosting.test", **settings})
        self.defaults = {key: validated.get(key) for key in DEFAULTS}

    def inspect(self, name):
        output = command(["docker", "ps", "-aq", "--filter", f"name=^{name}$"])
        if not output.strip():
            return None
        return json.loads(command(["docker", "inspect", name]))[0]

    def health(self, row):
        if json.loads(row["payload"]).get("runtime") == "compose":
            from .compose_adopt import health
            return health(row)
        try:
            container = self.inspect("hosting-site-" + row["name"])
            status = container["State"] if container else {}
            result = {"container": status.get("Status", "absent"),
                    "application": status.get("Health", {}).get("Status", "unknown"),
                    "quota": quota_record(row["project"]) if container else None,
                    "pids_limit": container["HostConfig"].get("PidsLimit") if container else None}
            if json.loads(row["payload"]).get("runtime") == "php":
                php = self.inspect("hosting-php-" + row["name"])
                result["php_container"] = php["State"]["Status"] if php else "absent"
                result["php_version"] = command(["docker", "exec", "hosting-php-" + row["name"], "php", "-n", "-r", "echo PHP_VERSION;"], timeout=3).strip() if php and php["State"]["Running"] else "unavailable"
                if not php or php["State"].get("Health", {}).get("Status") != "healthy":
                    result["application"] = "unhealthy"
            return result
        except Exception:
            return {"container": "unknown", "application": "unknown", "quota": None}

    def create(self, row, step):
        if json.loads(row['payload']).get('package_id'):
            from .package_deploy import perform
            return perform(self, row, step)
        if json.loads(row["payload"]).get("runtime") == "compose":
            raise ValueError("Compose sites deploy through the package path; host-adopted projects were retired.")
        data = validate_create(json.loads(row["payload"]))
        is_php = data.get("runtime") == "php"
        name, uid, project = data["name"], row["uid"], row["project"]
        root = SITES / name
        preflight()
        step("identity and folders")
        # A normal OS user may never accidentally be reused as a site identity.
        try:
            pwd.getpwuid(uid)
        except KeyError:
            pass
        else:
            raise ValueError("Reserved site UID collides with a host account")
        if not root.exists():
            if root.is_symlink():
                raise ValueError("Site root is a symlink")
            # Never attach an already allocated project to new data.
            try:
                existing = quota_record(project)
            except RuntimeError:
                existing = None
            if existing and (existing["used_bytes"] or existing["hard_bytes"]):
                raise ValueError("Reserved project ID already has an unmanaged quota")
            root.mkdir(mode=0o711)
        trusted(root, directory=True)
        root.chmod(0o711)
        marker = root / ".hosting-operation"
        if not marker.exists():
            if any(root.iterdir()):
                raise ValueError("Unmanaged content at reserved site path; retained for inspection")
            atomic(marker, row["id"])
        trusted(marker)
        if marker.read_text() != row["id"]:
            raise ValueError("Site belongs to another operation")

        step("site data quota")
        # Apply recursively only before exposing the content directory to the site identity.
        if not (root / "html").exists():
            apply_quota(root, project, data["data_mb"])
        verify_quota(root, project, data["data_mb"])
        for folder in ("html", "logs", "volumes"):
            path = root / folder
            if path.is_symlink():
                raise ValueError(f"Unexpected link: {path}")
            if not path.exists():
                path.mkdir(mode=0o700)
                os.chown(path, uid, uid)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                raise ValueError(f"Incorrect ownership: {path}")
        conf = root / "conf"
        conf.mkdir(mode=0o700, exist_ok=True)
        trusted(conf, directory=True)
        metadata = dict(data, schema=1, uid=uid, gid=uid, quota_project=project,
                        operation_id=row["id"], backups="not configured")
        metadata.setdefault("runtime", "static")
        if is_php:
            step("PHP configuration and image")
            from .php_site import prepare
            site_image = prepare(root, data, row, metadata, NGINX)
        else:
            from .php_site import keep_file
            atomic(root / "hosting.yaml", yaml.safe_dump(metadata))
            atomic(conf / "nginx.conf", static_nginx(), 0o644)
            keep_file(conf / "site.nginx.conf", "# Site-specific nginx locations and rewrite rules. Preserved on retry.\n")
        index = root / "html" / ("index.php" if is_php else "index.html")
        # New content only: O_EXCL and O_NOFOLLOW prevent replacement and symlink traversal.
        try:
            fd = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                os.fchown(stream.fileno(), uid, uid)
                content = f"<!doctype html><title>{name}</title><h1>{name} is ready</h1>"
                stream.write(f"<?php echo '{content}';\n" if is_php else content + "\n")

        step("isolated network")
        network = f"hosting-ingress-{name}"
        needed_networks = [network]
        if is_php:
            needed_networks.append(f"hosting-backend-{name}")
        for needed in needed_networks:
            networks = command(["docker", "network", "ls", "--filter", f"name=^{needed}$", "-q"]).strip()
            if networks:
                details = json.loads(command(["docker", "network", "inspect", needed]))[0]
                if details.get("Labels", {}).get("hosting.operation") != row["id"] or not details["Internal"]:
                    raise ValueError("Unmanaged network collision")
            else:
                command(["docker", "network", "create", "--internal", "--label", f"hosting.operation={row['id']}", needed])
        if is_php:
            from .mail import attach as attach_mail
            attach_mail(self, row, [data["domain"], *data.get("aliases", [])])

        step("container and layer quota")
        container_name = f"hosting-site-{name}"
        prior = self.inspect(container_name)
        if prior and prior["Config"].get("Labels", {}).get("hosting.operation") != row["id"]:
            raise ValueError("Unmanaged container collision")
        if is_php:
            php_prior = self.inspect("hosting-php-" + name)
            if php_prior and php_prior["Config"].get("Labels", {}).get("hosting.operation") != row["id"]:
                raise ValueError("Unmanaged PHP container collision")
        project_members = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project=hosting-site-{name}"]).split()
        for member in project_members:
            item = json.loads(command(["docker", "inspect", member]))[0]
            if item["Config"].get("Labels", {}).get("hosting.operation") != row["id"]:
                raise ValueError("Unmanaged Compose project collision")
        compose = {
            "name": "hosting-site-" + name,
            "services": {"web": {
                "image": self.config["nginx_image"], "container_name": container_name,
                "labels": {"hosting.operation": row["id"]},
                "user": f"{uid}:{uid}", "entrypoint": ["nginx"], "command": ["-g", "daemon off;"],
                "restart": "unless-stopped", "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                **container_limits(data),
                "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}},
                "volumes": [f"{root}/html:/site:ro", f"{conf}/nginx.conf:/etc/nginx/nginx.conf:ro",
                            f"{conf}/site.nginx.conf:/etc/hosting/site.nginx.conf:ro"],
                "networks": {"ingress": {"aliases": ["web-" + name]}},
                "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/__hosting_health"],
                                "interval": "2s", "timeout": "2s", "retries": 10}}},
            "networks": {"ingress": {"external": True, "name": network}}}
        if is_php:
            from .php_site import compose_services
            compose_services(compose, root, data, row, site_image, needed_networks[1])
        atomic(root / "compose.yml", yaml.safe_dump(compose))
        atomic(root / "compose.hosting.yml", "# Managed template: settings currently live in compose.yml\nservices: {}\n")
        command(["docker", "compose", "-f", root / "compose.yml", "up", "-d", "--wait", "--wait-timeout", "60"])
        for service in compose["services"].values():
            if data.get("pids_limit") is None:
                # Compose drops -1 on this engine, allowing systemd's default TasksMax.
                # Explicit runtime update disables that per-container inherited cap.
                command(["docker", "update", "--pids-limit", "-1", service["container_name"]])
                if command(["docker", "exec", service["container_name"], "cat", "/sys/fs/cgroup/pids.max"]).strip() != "max":
                    raise RuntimeError("Container unexpectedly inherits a process cap")
            live = self.inspect(service["container_name"])
            layer = Path(live["GraphDriver"]["Data"]["UpperDir"])
            if not layer.is_relative_to("/srv/docker/overlay2"):
                raise RuntimeError("Unexpected container layer location")
            layer_project, _ = project_id(layer)
            if data.get("layer_mb") is not None:
                verify_quota(layer, layer_project, data["layer_mb"], inherit=False)
            elif quota_record(layer_project)["hard_bytes"] != 0:
                raise RuntimeError("Writable layer unexpectedly inherits a hard quota")
            limits = live["HostConfig"]
            if data.get("memory_mb") is None and limits["Memory"] != 0:
                raise RuntimeError("Container unexpectedly has a memory cap")
            if data.get("cpus") is None and (limits["NanoCpus"] or limits["CpuQuota"] > 0):
                raise RuntimeError("Container unexpectedly has a CPU cap")
            if data.get("pids_limit") is None and limits.get("PidsLimit") not in (None, 0, -1):
                raise RuntimeError("Container unexpectedly has a process cap")
            if live["HostConfig"].get("PortBindings") or live["Config"]["User"] != f"{uid}:{uid}":
                raise RuntimeError("Unexpected container exposure or identity")

        if data.get("database"):
            from .database_site import provision
            provision(self, row, data["database"], step)

        step("proxy validation and publication")
        domains = [data["domain"], *data.get("aliases", [])]
        self.publish(row, network, domains)
        if is_php:
            from .requests_site import apply
            step("trusted proxy and HTTPS loopback")
            apply(self,row,"php",publish=False)
        step("HTTPS verification")
        self.verify_domains(domains)

    def verify_domains(self, domains):
        for domain in domains:
            command(["curl", "--noproxy", "*", "--fail", "--silent", "--show-error", "--retry", "5", "--retry-all-errors",
                     "--cacert", PROXY / "data/caddy/pki/authorities/local/root.crt",
                     "--resolve", f"{domain}:443:127.0.0.1", f"https://{domain}/__hosting_health"], timeout=45)

    def change_domains(self, row, domains):
        if json.loads(row["payload"]).get("runtime") == "compose":
            from .compose_adopt import change_domains
            return change_domains(self, row, domains)
        from .core import validate_domains
        domains = validate_domains(domains)
        root = SITES / row["name"]
        trusted(root, directory=True)
        trusted(root / "hosting.yaml")
        metadata = yaml.safe_load((root / "hosting.yaml").read_text())
        if metadata.get("operation_id") != row["id"]:
            raise ValueError("Site metadata belongs to another operation")
        if self.health(row)["application"] != "healthy":
            raise RuntimeError("Site must be healthy before publishing a domain change")
        self.publish(row, "hosting-ingress-" + row["name"], domains)
        self.verify_domains(domains)
        if metadata.get('web_settings'):
            from .requests_site import apply
            apply(self,row,metadata['web_settings']['profile'],domains=domains,publish=False)
        metadata.update(domain=domains[0], aliases=domains[1:])
        atomic(root / "hosting.yaml", yaml.safe_dump(metadata))

    def unpublish(self, row):
        return unpublish(self, row)

    def publish(self, row, network, domains=None, upstream=None):
        routes_path = PROXY / "routes.json"
        trusted(PROXY, directory=True)
        routes = json.loads(routes_path.read_text())
        domains = domains or [row["domain"]]
        for domain in domains:
            if domain in routes and routes[domain]["operation"] != row["id"]:
                raise ValueError("Domain belongs to another managed route")
        routes = {domain: item for domain, item in routes.items() if item["operation"] != row["id"]}
        for domain in domains:
            routes[domain] = {"upstream": (upstream or f"web-{row['name']}:8080"), "operation": row["id"], "network": network}
        live = self.inspect("hosting-edge")
        if not live or live["Config"].get("Labels", {}).get("hosting.managed") != "edge-v1":
            raise RuntimeError("Managed edge is absent; run installer edge setup")
        if network not in live["NetworkSettings"]["Networks"]:
            command(["docker", "network", "connect", network, "hosting-edge"])
        candidate = render_routes(routes)
        conf = PROXY / "conf"
        atomic(conf / "candidate.Caddyfile", candidate, 0o644)
        command(["docker", "exec", "hosting-edge", "caddy", "validate", "--config", "/etc/caddy/candidate.Caddyfile", "--adapter", "caddyfile"])
        old = (conf / "Caddyfile").read_text()
        atomic(conf / "previous.Caddyfile", old, 0o644)
        # Durable boot config precedes reload. An interrupted operation stays recovery-needed;
        # retry validates/reloads it. Every route present here already has verified storage/health.
        atomic(conf / "Caddyfile", candidate, 0o644)
        try:
            command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
        except Exception:
            atomic(conf / "Caddyfile", old, 0o644)
            command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
            raise
        atomic(routes_path, json.dumps(routes, indent=2))
        # Edge network membership survives daemon restart; its generated Compose must also
        # include every committed network for explicit recreation by the installer.
        write_edge_compose(self.config, routes)


def reconcile_edge(host):
    """The edge's configuration as this release renders it. Routes do not change here; the text around
    them can (the per-hostname access logs arrived after some edges were built). A difference is
    validated, written and reloaded exactly as a route change is; no edge, nothing to do."""
    routes_path = PROXY / "routes.json"
    if not routes_path.exists(): return {'reconciled': False, 'reason': 'no routes'}
    trusted(PROXY, directory=True)
    routes = json.loads(routes_path.read_text())
    conf = PROXY / "conf"
    candidate = render_routes(routes)
    current = (conf / "Caddyfile").read_text() if (conf / "Caddyfile").exists() else ''
    if candidate == current: return {'reconciled': False, 'reason': 'current'}
    live = host.inspect("hosting-edge")
    if not live or not live["State"].get("Running"): return {'reconciled': False, 'reason': 'edge not running'}
    atomic(conf / "candidate.Caddyfile", candidate, 0o644)
    command(["docker", "exec", "hosting-edge", "caddy", "validate", "--config", "/etc/caddy/candidate.Caddyfile", "--adapter", "caddyfile"])
    atomic(conf / "previous.Caddyfile", current, 0o644)
    atomic(conf / "Caddyfile", candidate, 0o644)
    try:
        command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
    except Exception:
        atomic(conf / "Caddyfile", current, 0o644)
        command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
        raise
    return {'reconciled': True, 'routes': len(routes)}


def unpublish(host, row):
    """Remove every route owned by this site's operation; the edge keeps serving everyone else."""
    routes_path = PROXY / "routes.json"
    trusted(PROXY, directory=True)
    routes = json.loads(routes_path.read_text())
    remaining = {domain: item for domain, item in routes.items() if item["operation"] != row["id"]}
    if remaining == routes:
        return
    live = host.inspect("hosting-edge")
    if not live or live["Config"].get("Labels", {}).get("hosting.managed") != "edge-v1":
        raise RuntimeError("Managed edge is absent; run installer edge setup")
    candidate = render_routes(remaining)
    conf = PROXY / "conf"
    atomic(conf / "candidate.Caddyfile", candidate, 0o644)
    command(["docker", "exec", "hosting-edge", "caddy", "validate", "--config", "/etc/caddy/candidate.Caddyfile", "--adapter", "caddyfile"])
    old = (conf / "Caddyfile").read_text()
    atomic(conf / "previous.Caddyfile", old, 0o644)
    atomic(conf / "Caddyfile", candidate, 0o644)
    try:
        command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
    except Exception:
        atomic(conf / "Caddyfile", old, 0o644)
        command(["docker", "exec", "hosting-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
        raise
    atomic(routes_path, json.dumps(remaining, indent=2))
    write_edge_compose(host.config, remaining)


def render_routes(routes):
    text = '{\n skip_install_trust\n servers {\n  protocols h1 h2\n }\n}\nhttp:// {\n respond "server is up" 200\n}\n'
    for domain, item in sorted(routes.items()):
        # One rolling JSON access log per hostname under the edge's own data: 20 MiB × 5 files, 30 days,
        # the input for fail2ban and for reading what a site received. Caddy's process log stays on stdout.
        text += f'\n{domain} {{\n tls internal\n log {{\n  output file /data/logs/{domain}.log {{\n   roll_size 20MiB\n   roll_keep 5\n   roll_keep_for 720h\n  }}\n  format json\n }}\n request_header -X-Forwarded-*\n reverse_proxy {item["upstream"]} {{\n  header_up -Forwarded\n  header_up X-Forwarded-For {{http.request.remote.host}}\n  header_up X-Forwarded-Proto https\n  header_up X-Forwarded-Host {{http.request.host}}\n  header_up X-Forwarded-Port 443\n  header_up X-Real-IP {{http.request.remote.host}}\n }}\n}}\n'
    return text


def write_edge_compose(config, routes):
    networks = {"edge": {"name": "hosting-edge-egress"}}
    for item in routes.values():
        networks[item["network"]] = {"external": True, "name": item["network"]}
    compose = {"name": "hosting-edge", "services": {"caddy": {
        "image": config["caddy_image"], "container_name": "hosting-edge", "labels": {"hosting.managed": "edge-v1"},
        "user": "20010:20010", "restart": "unless-stopped", "read_only": True,
        "cap_drop": ["ALL"], "cap_add": ["NET_BIND_SERVICE"], "security_opt": ["no-new-privileges:true"],
        "mem_limit": "256m", "cpus": 1, "pids_limit": 128,
        "ports": ["80:80", "443:443"], "networks": list(networks),
        "volumes": [f"{PROXY}/conf:/etc/caddy:ro", f"{PROXY}/data:/data", f"{PROXY}/config:/config"],
        "tmpfs": ["/tmp:size=16m,mode=1777"],
        "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}}}, "networks": networks}
    atomic(PROXY / "compose.yml", yaml.safe_dump(compose))
