"""Real browser journey against the private installed panel, using disposable sites."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8088")
    parser.add_argument("--output", default="/tmp/reeve-browser")
    parser.add_argument("--executable")
    parser.add_argument("--open-site", action="store_true")
    parser.add_argument("--view-only", action="store_true")
    parser.add_argument("--default-limits", action="store_true")
    parser.add_argument("--php-version")
    parser.add_argument("--database", choices=("mysql", "mariadb", "postgres"))
    parser.add_argument("--database-series")
    parser.add_argument("--alias", action="append", default=[])
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=args.executable,
            args=["--host-resolver-rules=MAP *.hosting.test 127.0.0.1", "--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1280, "height": 950})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("request", lambda req: print({"url": req.url, "origin": req.headers.get("origin"), "referer": req.headers.get("referer")}, flush=True) if req.method == "POST" else None)
        page.goto(args.base)
        page.get_by_label("Password").fill(Path(args.password_file).read_text().strip())
        page.get_by_role("button", name="Sign in", exact=True).click()
        try:
            page.wait_for_url(args.base + "/", timeout=10000)
        except Exception:
            page.screenshot(path=str(output / "login-failure.png"))
            print(page.locator("body").inner_text(), flush=True)
            raise
        if not args.view_only:
            page.get_by_role("link", name="Create site", exact=True).click()
            page.get_by_label("Site name", exact=True).fill(args.name)
            page.get_by_label("Primary domain", exact=True).fill(args.name + ".hosting.test")
            if args.alias:
                page.get_by_label("Additional domains (optional)", exact=True).fill("\n".join(args.alias))
            if args.php_version:
                page.get_by_label("Site type", exact=True).select_option("php")
                page.get_by_label("PHP branch", exact=True).select_option(args.php_version)
            if args.database:
                page.get_by_label('Database (optional)', exact=True).select_option(args.database)
                if args.database_series:
                    page.locator('#db-series-' + args.database).select_option(args.database_series)
            if not args.default_limits:
                page.get_by_text("Advanced limits", exact=True).click()
                page.get_by_label("Site data (MiB)", exact=True).fill("16")
                page.get_by_label("Writable layer (MiB)", exact=True).fill("16")
                if args.php_version:
                    page.get_by_label("Memory (MiB)", exact=True).fill("256")
            page.get_by_role("button", name="Create site", exact=True).click()
            page.wait_for_url(args.base + "/sites/" + args.name)
            # Close the submitting page: the job must proceed independently.
            page.close()
            page = context.new_page()
        for _ in range(900):
            selected = next(x for x in page.request.get(args.base + "/api/sites").json() if x["name"] == args.name)
            state = selected["state"]
            if state in ("succeeded", "recovery-needed", "failed"):
                break
            time.sleep(1)
        page.goto(args.base + "/sites/" + args.name)
        page.screenshot(path=str(output / (args.name + ".png")), full_page=True)
        result = page.request.get(args.base + "/api/sites").json()
        selected = next(x for x in result if x["name"] == args.name)
        print(json.dumps({"site": selected, "browser_errors": errors}, indent=2))
        assert state == "succeeded", selected
        assert selected["health"]["application"] == "healthy"
        assert not errors
        if args.database:
            assert selected['database']['health'] == 'healthy', selected
            assert selected['database']['engine'] == args.database
            page.get_by_role('button', name='Show database credentials', exact=True).click()
            assert 'site' in page.locator('body').inner_text()
            # Never screenshot/log the credential page.
            page.goto(args.base + '/sites/' + args.name)
        if args.default_limits:
            settings = json.loads(selected["payload"])
            assert settings["data_mb"] == 1024
            assert all(key not in settings for key in ("memory_mb", "layer_mb", "cpus", "pids_limit"))
            assert "Unlimited / Unlimited" in page.locator("body").inner_text()
        if args.open_site:
            with page.expect_popup() as popup:
                page.get_by_role("link", name="Open site").click()
            site = popup.value
            site.wait_for_load_state()
            assert args.name in site.title()
            assert site.url == "https://" + args.name + ".hosting.test/"
            print("HTTPS browser navigation passed with certificate verification enabled")
        browser.close()


if __name__ == "__main__":
    main()
