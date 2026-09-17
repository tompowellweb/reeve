"""Build and inspect branch-pinned Surý runtimes on demand."""
import hashlib
import json
from pathlib import Path

from .host import OPS, atomic, command, preflight, trusted

CATALOG = OPS / "panel/worker/php-runtimes.json"
REQUIRED_EXTENSIONS = {"bcmath", "curl", "gd", "imagick", "intl", "mbstring", "mysqli", "pdo_mysql",
    "pgsql", "pdo_pgsql", "sqlite3", "pdo_sqlite", "soap", "dom", "xml", "zip", "Zend OPcache", "apcu", "redis", "memcached"}


def catalog():
    if not CATALOG.exists():
        return {}
    trusted(CATALOG)
    data = json.loads(CATALOG.read_text())
    if data["schema"] != 1:
        raise RuntimeError("Unsupported PHP runtime catalog")
    return data["runtimes"]


def build(branches=None):
    preflight()
    context = Path(__file__).resolve().parent.parent / "templates/php-image"
    recipe = hashlib.sha256(b"".join(path.name.encode() + path.read_bytes() for path in sorted(context.iterdir()))).hexdigest()
    runtimes = catalog()
    from .versions import require
    for branch in (branches if branches is not None else sorted(runtimes)):
        require(branch)
        previous = runtimes.get(branch)
        if previous and previous["recipe"] == recipe:
            try:
                observed = command(["docker", "image", "inspect", previous["image"], "--format", "{{.Id}}"])
                if observed.strip() == previous["image_id"]:
                    print(f"PHP {branch}: retained verified image", flush=True)
                    continue
            except RuntimeError:
                pass
        tag = f"hosting-php:{branch}-{recipe[:16]}"
        print(f"Building PHP {branch} from trixie and Surý", flush=True)
        command(["docker", "build", "--build-arg", f"PHP_VERSION={branch}", "--tag", tag,
                 "--file", context / "Containerfile", context], timeout=900)
        image_id = command(["docker", "image", "inspect", tag, "--format", "{{.Id}} "]).strip()
        # This check runs as an unprivileged numeric user, with no application mounts/network.
        info = json.loads(command(["docker", "run", "--rm", "--network", "none", "--user", "30000:30000", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges:true", "--entrypoint", "php", tag, "-r",
             'echo json_encode(array("php_version" => PHP_VERSION, "extensions" => get_loaded_extensions()));']))
        if not info["php_version"].startswith(branch + ".") or not REQUIRED_EXTENSIONS.issubset(info["extensions"]):
            raise RuntimeError(f"PHP {branch} runtime version/extensions do not meet the template contract")
        packages = command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", tag,
                            "/usr/share/hosting-runtime/packages.tsv"])
        runtimes[branch] = {"image": tag, "image_id": image_id, "recipe": recipe, **info,
                            "packages": packages.splitlines()}
        atomic(CATALOG, json.dumps({"schema": 1, "runtimes": runtimes}, indent=2))
        print(f"PHP {info['php_version']}: {len(info['extensions'])} extensions verified", flush=True)
    return {branch: {k: v for k, v in item.items() if k != "packages"} for branch, item in runtimes.items()}


def ensure(branch):
    existing = catalog().get(branch)
    if existing:
        observed = command(["docker", "image", "inspect", existing["image"], "--format", "{{.Id}}"])
        if observed.strip() == existing["image_id"]:
            return existing
        raise RuntimeError("Recorded PHP runtime image changed; restore the pinned artifact")
    build([branch])
    return catalog()[branch]
