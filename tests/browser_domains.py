"""Disposable multi-domain browser journey on the private VM."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--output", default="/var/lib/hosting-browser/results")
    args = parser.parse_args()
    base = "http://127.0.0.1:8088"
    name = "m2-domains"
    original, preview, live = [name + suffix + ".hosting.test" for suffix in ("", "-preview", "-live")]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium",
            args=["--host-resolver-rules=MAP *.hosting.test 127.0.0.1", "--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        page.goto(base)
        page.get_by_label("Password").fill(Path(args.password_file).read_text().strip())
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_url(base + "/")
        rows = page.request.get(base + "/api/sites").json()
        if not any(row["name"] == name for row in rows):
            page.get_by_role("link", name="Create site", exact=True).click()
            page.get_by_label("Site name", exact=True).fill(name)
            page.get_by_label("Primary domain", exact=True).fill(original)
            page.get_by_label("Additional domains (optional)", exact=True).fill(preview + "\n" + live)
            page.get_by_role("button", name="Create site", exact=True).click()
            page.wait_for_url(base + "/sites/" + name)
        for _ in range(60):
            rows = page.request.get(base + "/api/sites").json()
            row = next(row for row in rows if row["name"] == name)
            if row["state"] == "succeeded":
                break
            assert row["state"] not in ("failed", "recovery-needed"), row
            time.sleep(1)
        assert row["state"] == "succeeded", row
        probe = context.new_page()
        for domain in row["domains"]:
            response = probe.goto("https://" + domain)
            assert response.status == 200 and name in probe.title()
            assert probe.url == "https://" + domain + "/"
        page.goto(base + "/sites/" + name)
        page.get_by_role("button", name="Edit domains", exact=True).click()
        page.get_by_label("Additional domains (optional)", exact=True).fill("m2-php84.hosting.test")
        page.get_by_role("button", name="Save domains", exact=True).click()
        assert "already reserved by another site" in page.get_by_role("alert").inner_text()
        page.get_by_role("button", name="Edit domains", exact=True).click()
        page.get_by_label("Primary domain", exact=True).fill(live)
        page.get_by_label("Additional domains (optional)", exact=True).fill(preview)
        page.get_by_role("button", name="Save domains", exact=True).click()
        page.wait_for_url(base + "/sites/" + name)
        page.close()
        page = context.new_page()
        for _ in range(60):
            row = next(row for row in page.request.get(base + "/api/sites").json() if row["name"] == name)
            if row["domain_job"] and row["domain_job"]["state"] == "succeeded":
                break
            assert not row["domain_job"] or row["domain_job"]["state"] not in ("failed", "recovery-needed"), row
            time.sleep(1)
        assert row["domain"] == live and row["domains"] == [live, preview], row
        for domain in row["domains"]:
            assert probe.goto("https://" + domain).status == 200
            assert name in probe.title() and probe.url == "https://" + domain + "/"
        page.goto(base + "/sites/" + name)
        with page.expect_popup() as opened:
            page.get_by_role("link", name="Open site").click()
        opened.value.wait_for_load_state()
        assert opened.value.url == "https://" + live + "/"
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "m2-domains.png"), full_page=True)
        (output / "m2-domains.json").write_text(json.dumps(row, indent=2))
        print(json.dumps({"site": row, "checks": "multiple Create names, independent HTTPS hosts, conflict rejection, primary switch, alias removal, page closure, Open site"}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
