"""What certificate the edge serves for each of a site's names, for the Domains section. This reports; it trusts nothing."""
import re
import socket
import subprocess
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
