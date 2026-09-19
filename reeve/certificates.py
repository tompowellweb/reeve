"""What certificate the edge serves for each of a site's names, for the Domains section. This reports; it trusts nothing."""
import json
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as Late
from datetime import datetime

LOCAL_ISSUER = "Caddy Local Authority"


def served(domain, address="127.0.0.1", port=443, timeout=4):
    """Handshake with the edge as `domain` and return openssl's issuer and expiry lines for the certificate it serves,
    or None when it serves none (no route, or a public certificate still being obtained)."""
    try:
        client = subprocess.run(["openssl", "s_client", "-connect", f"{address}:{port}", "-servername", domain],
                                input="", capture_output=True, text=True, timeout=timeout)
        if client.returncode != 0 or "BEGIN CERTIFICATE" not in client.stdout: return None
        info = subprocess.run(["openssl", "x509", "-noout", "-issuer", "-enddate"], input=client.stdout,
                              capture_output=True, text=True, timeout=timeout)
        return info.stdout if info.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def describe(text):
    """Pure: openssl's lines to a state (public, internal, none) and a sentence for the operator."""
    if not text:
        return {"state": "none", "text": "No certificate yet: the edge has no route for this name or is still obtaining one"}
    issuer = (re.search(r"^issuer=(.*)$", text, re.M) or [None, ""])[1]
    ends = re.search(r"^notAfter=(.*)$", text, re.M)
    expires = ""
    if ends:
        try: expires = datetime.strptime(ends[1].strip(), "%b %d %H:%M:%S %Y %Z").strftime("%-d %b %Y")
        except ValueError: expires = ends[1].strip()
    if LOCAL_ISSUER in issuer:
        return {"state": "internal", "text": "This server's own certificate, so browsers warn. With public certificates on, "
                                            "Let's Encrypt issues one once the name points at this server and port 80 is reachable"}
    organisation = re.search(r"\bO\s*=\s*([^,/]+)", issuer)
    who = organisation[1].strip() if organisation else (issuer.strip() or "Unknown issuer")
    return {"state": "public", "text": f"{who} certificate" + (f", expires {expires}" if expires else "")}


def resolves(domain, wait=1.5):
    """The addresses the name resolves to now, bounded so a slow resolver cannot hold the page."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            infos = pool.submit(socket.getaddrinfo, domain, 443, proto=socket.IPPROTO_TCP).result(timeout=wait)
            return sorted({info[4][0] for info in infos})
        except (OSError, Late):
            return []


def status(domain):
    result = describe(served(domain))
    result["addresses"] = resolves(domain)
    return result


# ---- the edge's own account of issuance (worker side, root)

def issuance_log(lines):
    """Pure: from the edge's process log (JSON lines), the latest issuance outcome per name: the last error the
    public issuer gave and the last certificate obtained, with their times."""
    result = {}
    for line in lines:
        try: event = json.loads(line)
        except ValueError: continue
        name, logger, msg = event.get("identifier"), event.get("logger"), event.get("msg")
        if not name or logger not in ("tls.obtain", "http.acme_client"): continue
        entry = result.setdefault(name, {"last_error": "", "error_at": None, "obtained": "", "obtained_at": None, "_detail": ""})
        if logger == "tls.obtain" and msg == "obtaining certificate":
            entry["_detail"] = ""   # a new attempt: the first challenge's verdict is the informative one
        elif logger == "http.acme_client" and msg == "validating authorization" and not entry["_detail"]:
            entry["_detail"] = str((event.get("problem") or {}).get("detail", ""))[:600]
        elif logger == "tls.obtain" and msg == "could not get certificate from issuer":
            entry.update(last_error=entry["_detail"] or str(event.get("error", ""))[:600], error_at=event.get("ts"))
        elif logger == "tls.obtain" and msg == "certificate obtained successfully":
            entry.update(obtained=str(event.get("issuer", "")), obtained_at=event.get("ts"))
    for entry in result.values(): entry.pop("_detail")
    return result


def edge_issuance(names):
    """What the edge's log says about each name (root: reads the container's log)."""
    from .host import command
    try: text = command(["docker", "logs", "--tail", "2000", "hosting-edge"], timeout=30)
    except RuntimeError: return {name: None for name in names}
    found = issuance_log(text.splitlines())
    return {name: found.get(name) for name in names}


def request_public(name, routed, wait=45):
    """Ask the edge for a public certificate for `name` now instead of at the local certificate's renewal: forget the
    local certificate it holds for the name and restart the edge, which obtains again, public issuer first. Returns
    the certificate served afterwards and the log's latest word on the name."""
    from .host import PROXY, command, tls_settings
    if tls_settings()["mode"] != "public": raise ValueError("Public certificates are off (tls.mode is internal in server.yaml)")
    if name not in routed: raise ValueError("The edge does not route this name")
    if not re.fullmatch(r"[a-z0-9.-]+", name): raise ValueError("Invalid name")
    folder = PROXY / "data/caddy/certificates/local" / name
    if folder.is_dir() and not folder.is_symlink(): shutil.rmtree(folder)
    command(["docker", "restart", "hosting-edge"], timeout=60)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        current = describe(served(name))
        if current["state"] == "public": break
        time.sleep(3)
    else:
        current = describe(served(name))
    return {**current, "issuance": edge_issuance([name])[name]}
