"""Explicit, repeatable transition from inspected proof to managed edge."""
import json
import os
import shutil
from pathlib import Path

from .host import Host, PROXY, atomic, apply_quota, command, preflight, render_routes, trusted, write_edge_compose


def setup(take_over_proof=False):
    preflight()
    host = Host()
    PROXY.mkdir(mode=0o700, exist_ok=True)
    trusted(PROXY, directory=True)
    for folder in ("conf", "data", "config"):
        (PROXY / folder).mkdir(mode=0o755, exist_ok=True)
    routes_path = PROXY / "routes.json"
    if routes_path.exists():
        routes = json.loads(routes_path.read_text())
    else:
        routes = {}
        proof = host.inspect("hosting-edge-proof-caddy-1")
        if proof:
            if not take_over_proof:
                raise RuntimeError("Proof edge exists. Inspect it, then explicitly use --take-over-proof.")
            if proof["Config"]["Labels"].get("com.docker.compose.project") != "hosting-edge-proof":
                raise RuntimeError("Proof identity mismatch")
            for suffix in ("a", "b"):
                backend = host.inspect(f"hosting-edge-proof-site_{suffix}-1")
                if not backend:
                    raise RuntimeError("Expected preserved proof backend is absent")
                networks = backend["NetworkSettings"]["Networks"]
                if len(networks) != 1:
                    raise RuntimeError("Unexpected proof network layout")
                routes[f"{suffix}.hosting.test"] = {"upstream": f"proof-web-{suffix}:8080",
                    "network": next(iter(networks)), "operation": "preserved-proof"}
            # Copy, never move, historical CA state. Repeating interrupted setup can safely
            # merge this source until the managed route manifest has been committed.
            for folder in ("data", "config"):
                shutil.copytree(Path("/srv/ops/caddy-proof") / folder, PROXY / folder, dirs_exist_ok=True)
            atomic(PROXY / "proof-transition.json", json.dumps({"schema": 1, "source": "/srv/ops/caddy-proof",
                "old_container": proof["Id"], "restoration": "Stop hosting-edge; docker start hosting-edge-proof-caddy-1"}, indent=2))
        elif take_over_proof:
            raise RuntimeError("Expected proof edge is absent; inspect before continuing")
        atomic(routes_path, json.dumps(routes, indent=2))
    for folder, project, size in (("data", 900100, 128), ("config", 900101, 16)):
        path = PROXY / folder
        apply_quota(path, project, size)
        for parent, dirs, files in os.walk(path):
            os.chown(parent, 20010, 20010)
            for filename in files:
                os.chown(Path(parent) / filename, 20010, 20010)
    conf = PROXY / "conf"
    if not (conf / "Caddyfile").exists():
        atomic(conf / "Caddyfile", render_routes(routes), 0o644)
    write_edge_compose(host.config, routes)
    command(["docker", "pull", host.config["caddy_image"]])
    command(["docker", "pull", host.config["nginx_image"]])
    command(["docker", "run", "--rm", "--network", "none", "--user", "20010:20010", "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE",
        "--security-opt", "no-new-privileges:true", "-v", f"{conf}:/etc/caddy:ro", "-v", f"{PROXY}/data:/data", "-v", f"{PROXY}/config:/config",
        host.config["caddy_image"], "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
    proof = host.inspect("hosting-edge-proof-caddy-1")
    stopped = proof and proof["State"]["Running"]
    if stopped:
        if not take_over_proof and not (PROXY / "proof-transition.json").exists():
            raise RuntimeError("Unexpected proof still owns web ports")
        command(["docker", "stop", "hosting-edge-proof-caddy-1"])
    try:
        command(["docker", "compose", "-f", PROXY / "compose.yml", "up", "-d"])
    except Exception:
        if stopped:
            command(["docker", "start", "hosting-edge-proof-caddy-1"])
        raise
