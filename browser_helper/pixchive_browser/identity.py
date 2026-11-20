import re
import time
from urllib.parse import urlsplit

from .storage import pixiv_domain

COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}\Z")

# Read only bounded bootstrap data and boolean DOM evidence; never form values or page text.
IDENTITY_SCRIPT = r"""() => {
    const visible = selector => [...document.querySelectorAll(selector)].some(element => {
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && bounds.width > 1 && bounds.height > 1;
    });
    let known = false, authenticated = false;
    const record = user => {
        known = true;
        authenticated = !!user && /^[1-9][0-9]*$/.test(String(user.id || ''));
    };
    try {
        const meta = document.querySelector('meta#meta-global-data,meta[name="global-data"]');
        const content = meta?.getAttribute('content');
        if (content && content.length <= 1048576) {
            const data = JSON.parse(content);
            if (Object.hasOwn(data, 'userData')) record(data.userData);
        }
        const text = document.querySelector('script#__NEXT_DATA__')?.textContent;
        if (text && text.length <= 1048576) {
            const data = JSON.parse(text);
            const serialized = data?.props?.pageProps?.serverSerializedPreloadedState;
            if (typeof serialized === 'string' && serialized.length <= 1048576) {
                const state = JSON.parse(serialized);
                if (Object.hasOwn(state?.userData || {}, 'self')) record(state.userData.self);
            }
        }
    } catch (_) { /* Unknown bootstrap formats fail closed. */ }
    return {
        url: location.href,
        identity_known: known,
        authenticated,
        login_form: visible('input[type="password"], input[autocomplete="one-time-code"], '
            + 'form[action*="/login"]'),
        challenge: visible('iframe[src*="/recaptcha/"][src*="bframe"], '
            + 'iframe[src*="hcaptcha"][src*="challenge"], '
            + 'iframe[src*="challenges.cloudflare.com"], #challenge-running, #challenge-form')
    };
}"""


def session_cookie(cookies: list[dict], *, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    candidates = []
    for cookie in cookies:
        if cookie.get("name") != "PHPSESSID":
            continue
        domain = cookie.get("domain", "").lower()
        if domain not in {"www.pixiv.net", ".www.pixiv.net", ".pixiv.net"}:
            continue
        if not pixiv_domain(domain) or cookie.get("path") != "/" or not cookie.get("secure"):
            continue
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (int, float)) or (expires != -1 and expires <= now):
            continue
        value = cookie.get("value")
        if not isinstance(value, str) or not COOKIE_VALUE.fullmatch(value):
            continue
        candidates.append(value)
    # Conflicting host/domain cookies are ambiguous. Do not export an arbitrary account.
    return candidates[0] if candidates and len(set(candidates)) == 1 else None


def classify(snapshot: dict, cookie: str | None, http_status: int | None = None) -> str:
    try:
        parsed = urlsplit(snapshot.get("url", ""))
        if parsed.scheme != "https" or parsed.hostname != "www.pixiv.net":
            return "needs_login"
        if snapshot.get("login_form") or snapshot.get("challenge"):
            return "needs_login"
        if re.search(r"/(?:login|auth|captcha|challenge|two-factor)(?:/|$)", parsed.path):
            return "needs_login"
        if http_status is not None and http_status >= 400:
            return "error"
        if snapshot.get("identity_known") and snapshot.get("authenticated") and cookie:
            return "connected"
        if snapshot.get("identity_known"):
            return "needs_login"
    except (TypeError, ValueError):
        pass
    return "error"
