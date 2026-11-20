"""Optional offline container smoke check; uses only a new tmpfs key and synthetic pages.

Run in this helper image as UID 1000, with /run/browser and /tmp tmpfs and a NEW /state
volume. No existing key, profile, application mount, network, or user account is used.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import time
import traceback
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from pixchive_browser.config import HOME, LOGIN, Settings
from pixchive_browser.identity import IDENTITY_SCRIPT, classify
from pixchive_browser.service import BrowserService


async def check_quiet_manual_login(service: BrowserService) -> None:
    context = service.session.context
    page = context.pages[0]
    # A known origin without a live page exercises Playwright's visitOrigins snapshot path.
    origin_page = await context.new_page()
    await origin_page.goto("https://accounts.pixiv.net/synthetic-quiet-origin")
    await origin_page.evaluate(
        "localStorage.setItem('synthetic-quiet-origin', 'offline-storage-sentinel')"
    )
    await origin_page.close()
    assert all(not item.url.startswith("https://accounts.pixiv.net/") for item in context.pages)
    await page.set_content(
        '<meta id="meta-global-data" content=\'{"userData":null}\'>'
        '<input id="synthetic-manual-input" autocomplete="username">'
        '<input type="password">'
    )
    input_field = page.locator("#synthetic-manual-input")
    await input_field.fill("synthetic-unfinished-login")
    before_pages = tuple(context.pages)
    before_encrypted = service.settings.state_file.read_bytes()
    created_pages = []
    navigations = []

    def on_page(created):
        created_pages.append(created)

    def on_navigation(frame):
        navigations.append(frame)

    context.on("page", on_page)
    page.on("framenavigated", on_navigation)

    def force_due():
        now = time.monotonic()
        service._last_visit = now - service.settings.reauth_seconds - 1
        service._last_save = now - service.settings.save_seconds - 1

    try:
        with (
            patch.object(context, "storage_state", wraps=context.storage_state) as snapshots,
            patch.object(context, "cookies", wraps=context.cookies) as cookie_reads,
            patch.object(page, "evaluate", wraps=page.evaluate) as inspections,
            patch.object(service.store, "save", wraps=service.store.save) as saves,
        ):
            assert (await service.open())["login_pending"] is True
            for _ in range(5):
                async with service.view():
                    assert (await service.open())["login_pending"] is True
                    for _ in range(20):
                        force_due()
                        await service.tick()
                assert service.viewers == 0
                for _ in range(20):
                    force_due()
                    await service.tick()
                assert (await service.reauth())["login_pending"] is True
                assert await input_field.input_value() == "synthetic-unfinished-login"
                await asyncio.sleep(0)
            snapshots.assert_not_called()
            cookie_reads.assert_not_called()
            inspections.assert_not_called()
            saves.assert_not_called()
            assert not created_pages and not navigations
            assert tuple(context.pages) == before_pages
            assert service.settings.state_file.read_bytes() == before_encrypted
            assert service.status()["login_pending"] is True
            print(
                "PASS manual input survives 200 due ticks and five view reconnects; "
                "no pages, snapshots, inspections, saves or navigation",
                flush=True,
            )

            # Synthetic user completion: replace the form with authenticated bootstrap evidence.
            await page.set_content(
                '<meta id="meta-global-data" content=\'{"userData":{"id":"123"}}\'>'
            )
            result = await service.complete()
            assert result["state"] == "connected" and result["login_pending"] is True
            completion_generation = result["login_generation"]
            cookie_reads.assert_awaited_once()
            inspections.assert_awaited_once()
            snapshots.assert_awaited_once_with(indexed_db=True)
            saves.assert_called_once()
            await asyncio.sleep(0)
            completed_pages = list(created_pages)
            completed_navigations = list(navigations)
            for _ in range(20):
                async with service.view():
                    force_due()
                    await service.tick()
                await service.tick()
            snapshots.assert_awaited_once()
            saves.assert_called_once()
            assert created_pages == completed_pages and navigations == completed_navigations
            # Stand-in for the application's successful check-only validation of this fake account.
            assert service.credential() == "synthetic-rotated"
            result = await service.resume(completion_generation)
            assert result["state"] == "connected" and result["login_pending"] is False
            cookie_reads.assert_awaited_once()
            inspections.assert_awaited_once()
            snapshots.assert_awaited_once()
            saves.assert_called_once()
            assert created_pages == completed_pages and navigations == completed_navigations
            force_due()
            await service.tick()
            assert snapshots.await_count > 1 and saves.call_count > 1
            assert len(navigations) > len(completed_navigations)
            assert service.state == "connected" and service.credential() == "synthetic-rotated"
            print(
                "PASS explicit completion inspects/saves once and stays pending; "
                "validation then resume makes no browser calls and automatic ticks recover",
                flush=True,
            )
    finally:
        context.remove_listener("page", on_page)
        page.remove_listener("framenavigated", on_navigation)


async def check() -> None:
    os.umask(0o077)
    root = Path("/run/browser")
    for directory in ("home", "tmp", "cache", "config", "xdg"):
        (root / directory).mkdir(mode=0o700, exist_ok=True)
    key = root / "synthetic-test.key"
    key.write_bytes(Fernet.generate_key())
    probe = Path("/state/.synthetic-write-check")
    probe.write_bytes(b"isolated helper volume permission check")
    assert probe.stat().st_uid == os.getuid() == 1000
    probe.unlink()
    print("PASS new named volume writable as UID 1000 without root entrypoint", flush=True)
    settings = Settings(key_file=key, managed_navigation=False, derived_keys_file=None)
    service = BrowserService(settings)
    second = None
    try:
        async with service.lock:
            await service._ensure_locked()
        print("PASS nonroot sandbox Chromium launch", flush=True)
        context = service.session.context
        rotated = False

        async def home(route):
            content = '<meta id="meta-global-data" content=\'{"userData":{"id":"123"}}\'>'
            headers = {"Content-Type": "text/html"}
            if rotated:
                headers["Set-Cookie"] = (
                    "PHPSESSID=synthetic-rotated; Path=/; Secure; HttpOnly; SameSite=Lax"
                )
            await route.fulfill(status=200, headers=headers, body=content)

        await context.route("**/*", home)
        result = await service.seed("synthetic-seed")
        assert result["state"] == "connected"
        page = context.pages[0]
        assert page.url == HOME
        await page.evaluate("""async () => {
            localStorage.setItem('synthetic-sso', 'local-storage-sentinel');
            await new Promise((resolve, reject) => {
                const request = indexedDB.open('synthetic-auth', 1);
                request.onupgradeneeded = () => request.result.createObjectStore('tokens');
                request.onerror = reject;
                request.onsuccess = () => {
                    const db = request.result;
                    const tx = db.transaction('tokens', 'readwrite');
                    tx.objectStore('tokens').put('indexeddb-sentinel', 'token');
                    tx.oncomplete = () => { db.close(); resolve(); };
                    tx.onerror = reject;
                };
            });
        }""")
        rotated = True
        assert (await service.reauth())["state"] == "connected"
        assert service.credential() == "synthetic-rotated"
        encrypted = settings.state_file.read_bytes()
        assert b"synthetic-rotated" not in encrypted
        assert b"local-storage-sentinel" not in encrypted
        assert b"indexeddb-sentinel" not in encrypted
        assert settings.state_file.stat().st_uid == os.getuid()
        assert settings.state_file.stat().st_mode & 0o777 == 0o600
        print("PASS normal browser home, cookie rotation, encrypted named-volume write", flush=True)

        await page.set_content('<meta id="meta-global-data" content=\'{"userData":null}\'>')
        assert classify(await page.evaluate(IDENTITY_SCRIPT), "synthetic-cookie") == "needs_login"
        await page.set_content('<input type="password">')
        snapshot = await page.evaluate(IDENTITY_SCRIPT)
        assert classify(snapshot, "synthetic-cookie") == "needs_login"
        await page.set_content('<input autocomplete="one-time-code">')
        snapshot = await page.evaluate(IDENTITY_SCRIPT)
        assert classify(snapshot, "synthetic-cookie") == "needs_login"
        await page.set_content('<iframe src="https://recaptcha.test/recaptcha/bframe"></iframe>')
        snapshot = await page.evaluate(IDENTITY_SCRIPT)
        assert snapshot["challenge"]
        assert classify(snapshot, "synthetic-cookie") == "needs_login"
        next_state = json.dumps({"userData": {"self": {"id": "123"}}})
        bootstrap = json.dumps(
            {"props": {"pageProps": {"serverSerializedPreloadedState": next_state}}}
        )
        await page.set_content(
            '<script id="__NEXT_DATA__" type="application/json">' + bootstrap + "</script>"
        )
        assert classify(await page.evaluate(IDENTITY_SCRIPT), "synthetic-cookie") == "connected"
        print("PASS actual DOM guest/password/MFA/CAPTCHA and Next bootstrap decisions", flush=True)

        await check_quiet_manual_login(service)
        await service.close()
        await service.factory.reset()  # Force restore into a completely new live profile.
        second = BrowserService(settings)
        # Prevent startup navigation: install the offline route before the explicit home visit.
        async with second.view():
            async with second.lock:
                await second._ensure_locked()
            await second.session.context.route("**/*", home)
            assert (await second.reauth())["state"] == "connected"
        restored = await second.session.context.storage_state(indexed_db=True)
        origin = next(item for item in restored["origins"] if item["origin"] == HOME.rstrip("/"))
        assert origin["localStorage"] == [
            {"name": "synthetic-sso", "value": "local-storage-sentinel"}
        ]
        assert origin["indexedDB"][0]["name"] == "synthetic-auth"
        print("PASS real set_storage_state restores localStorage and IndexedDB", flush=True)

        # Preserve account SSO while expiring ONLY PHPSESSID. HOME deliberately stays
        # guest, so only the bounded official LOGIN fallback can exercise existing SSO.
        restored_context = second.session.context
        accounts_page = await restored_context.new_page()
        await accounts_page.goto("https://accounts.pixiv.net/synthetic-setup")
        await accounts_page.evaluate(
            "localStorage.setItem('account-sso', 'synthetic-account-state')"
        )
        await accounts_page.close()
        await restored_context.add_cookies(
            [
                {
                    "name": "synthetic-account-sso",
                    "value": "synthetic-sso-cookie",
                    "domain": "accounts.pixiv.net",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "PHPSESSID",
                    "value": "expired-synthetic-php",
                    "domain": "www.pixiv.net",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "expires": time.time() - 60,
                },
            ]
        )
        assert not any(
            item["name"] == "PHPSESSID" for item in await restored_context.cookies([HOME])
        )
        print("CHECK synthetic PHP expired; account SSO retained", flush=True)
        await restored_context.unroute("**/*", home)
        redirects = []

        async def sso_flow(route):
            url = route.request.url
            headers = await route.request.all_headers()
            if url == HOME:
                redirects.append("guest-home-without-redirect")
                assert "PHPSESSID" not in headers.get("cookie", "")
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<meta id="meta-global-data" content=\'{"userData":null}\'>',
                )
            elif url == LOGIN:
                redirects.append("official-login-with-preserved-sso")
                assert "synthetic-account-sso=synthetic-sso-cookie" in headers.get("cookie", "")
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="""<script>
                    if (localStorage.getItem('account-sso') === 'synthetic-account-state') {
                        setTimeout(() => location.replace(
                            'https://www.pixiv.net/?synthetic_sso=accepted'), 200);
                    }
                </script>""",
                )
            elif url == HOME + "?synthetic_sso=accepted":
                redirects.append("home-issued-new-php")
                await route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "text/html",
                        "Set-Cookie": (
                            "PHPSESSID=synthetic-new-php; Path=/; Secure; HttpOnly; SameSite=Lax"
                        ),
                    },
                    body='<meta id="meta-global-data" content=\'{"userData":{"id":"123"}}\'>',
                )
            else:
                await route.abort()

        await restored_context.route("**/*", sso_flow)
        # Emulate expiry observation, then a due 10-minute AUTOMATIC home visit.
        async with second.lock:
            await second._poll_locked()
        assert second.state == "needs_login" and second.credential() is None
        second._last_visit = time.monotonic() - settings.reauth_seconds - 1
        await second.tick()
        for _attempt in range(30):
            if second.state == "connected":
                break
            await asyncio.sleep(0.1)
            await second.tick()
        print(
            "CHECK SSO redirect stages=" + ",".join(redirects) + "; state=" + second.state,
            flush=True,
        )
        assert redirects == [
            "guest-home-without-redirect",
            "official-login-with-preserved-sso",
            "home-issued-new-php",
        ]
        assert second.state == "connected"
        assert second.credential() == "synthetic-new-php"
        assert second.store.load()["cookies"]
        print(
            "PASS expired PHP -> automatic guest HOME -> official LOGIN SSO -> new PHP/connected",
            flush=True,
        )

        await restored_context.unroute("**/*", sso_flow)

        def block_home(mode, visits):
            async def handler(route):
                visits.append(route.request.url)
                assert route.request.url == HOME
                await route.fulfill(
                    status=429 if mode == "429" else 200,
                    headers={"Content-Type": "text/html", "Retry-After": "120"},
                    body='<meta id="meta-global-data" content=\'{"userData":null}\'>'
                    + (
                        '<div id="challenge-form">Additional verification</div>'
                        if mode == "challenge"
                        else ""
                    ),
                )

            return handler

        for blocked in ("challenge", "429"):
            visits = []
            blocked_home = block_home(blocked, visits)
            await restored_context.route("**/*", blocked_home)
            result = await second.reauth()
            assert visits == [HOME]
            assert result["state"] == ("error" if blocked == "429" else "needs_login")
            assert second.credential() is None
            if blocked == "429":
                assert second.retry_at >= int(time.time()) + 120
                await second.reauth()
                assert visits == [HOME]
            await restored_context.unroute("**/*", blocked_home)
        assert any(
            item["name"] == "PHPSESSID" and item["value"] == "synthetic-new-php"
            for item in await restored_context.cookies([HOME])
        )
        print(
            "PASS guest challenge/429 never visits LOGIN; shared cooldown and cookies retained",
            flush=True,
        )

        await second.disconnect()
        assert not settings.state_file.exists()
        assert not settings.profile.exists()
        print("PASS disconnect removes live profile and encrypted snapshot", flush=True)
    finally:
        await service.close()
        if second is not None:
            await second.close()
        key.unlink(missing_ok=True)


def main() -> int:
    display = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _attempt in range(100):
            if Path("/tmp/.X11-unix/X99").exists():
                break
            time.sleep(0.05)
        asyncio.run(check())
        return 0
    except Exception as error:
        # Class names suffice for isolated launch diagnosis; no exception values or credentials.
        print(
            f"FAIL offline browser smoke ({type(error).__name__}); no credentials printed",
            flush=True,
        )
        for frame in traceback.extract_tb(error.__traceback__):
            print(f"DIAG {Path(frame.filename).name}:{frame.lineno}", flush=True)
        current = error
        for _level in range(5):
            message = str(current)
            for marker, label in (
                ("Operation not permitted", "OS denied namespace operation"),
                ("No usable sandbox", "No usable Chromium sandbox"),
                ("Executable doesn't exist", "Playwright browser executable missing"),
                ("Permission denied", "Filesystem permission denied"),
            ):
                if marker in message:
                    print("DIAG " + label, flush=True)
            current = current.__context__
            if current is None:
                break
        return 1
    finally:
        display.terminate()
        with contextlib.suppress(Exception):
            display.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
