"""Grok (x.ai) registration flow based on DrissionPage."""

from __future__ import annotations

import html as html_lib
import os
import random
import shutil
import string
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError

from core.browser_runtime import ensure_browser_display_available, resolve_browser_headless


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
BROWSER_TIMEOUT_BASE = 1
BROWSER_WINDOW_WIDTH = 1280
BROWSER_WINDOW_HEIGHT = 800
EMAIL_STEP_TIMEOUT = 15
OTP_STEP_TIMEOUT = 180
PROFILE_STEP_TIMEOUT = 120
SSO_COOKIE_TIMEOUT = 120
ANTI_DETECTION_MIN_DELAY = 0.5
ANTI_DETECTION_MAX_DELAY = 2.0
BROWSER_LAUNCH_ARGS = ["--incognito"]
TURNSTILE_EXTENSION_NAME = "turnstilePatch"
TURNSTILE_MOUSE_PATCH_SCRIPT = """function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });"""
TURNSTILE_CHALLENGE_PATCH_SCRIPT = "window.dtp = 1;\n" + TURNSTILE_MOUSE_PATCH_SCRIPT
DEFAULT_FAILURE_HOLD_SECONDS = 45


def _rand_name(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=length)).capitalize()


def _rand_password(length: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length)) + ",,,aA1"


class GrokRegister:
    def __init__(
        self,
        captcha_solver=None,
        yescaptcha_key: str = "",
        proxy=None,
        log_fn=print,
        headless: bool = False,
        keep_browser_open_on_failure: Optional[bool] = None,
        failure_hold_seconds: Optional[int] = None,
    ):
        self.captcha_solver = captcha_solver
        self.key = yescaptcha_key
        self.proxy = proxy
        self.log = log_fn
        self.requested_headless = bool(headless)
        self.browser_headless = False
        self.browser: Chromium | None = None
        self.page = None
        self.co: ChromiumOptions | None = None

        if keep_browser_open_on_failure is None:
            keep_browser_open_on_failure = not self.requested_headless
        self.keep_browser_open_on_failure = bool(keep_browser_open_on_failure)
        if failure_hold_seconds is None:
            failure_hold_seconds = (
                DEFAULT_FAILURE_HOLD_SECONDS if self.keep_browser_open_on_failure else 0
            )
        self.failure_hold_seconds = max(0, int(failure_hold_seconds or 0))
        self._init_browser_config()

    @staticmethod
    def _turnstile_extension_path() -> Path:
        return (
            Path(__file__).resolve().parent
            / "extensions"
            / TURNSTILE_EXTENSION_NAME
        )

    @staticmethod
    def _browser_path_candidates() -> list[str]:
        candidates = [os.getenv("GROK_BROWSER_PATH", "").strip(), os.getenv("CHROME_PATH", "").strip()]
        if os.name == "nt":
            for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.getenv(env_name, "").strip()
                if base:
                    candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        elif sys.platform == "darwin":
            candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        else:
            candidates.extend(
                ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium-browser", "/usr/bin/chromium"]
            )
        for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(resolved)
        uniq: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            val = str(item or "").strip()
            if val and val not in seen:
                uniq.append(val)
                seen.add(val)
        return uniq

    @classmethod
    def _resolve_browser_path(cls) -> str:
        for candidate in cls._browser_path_candidates():
            if Path(candidate).exists():
                return candidate
        return ""

    def _init_browser_config(self) -> None:
        self.co = ChromiumOptions()
        self.co.auto_port()
        self.co.set_timeouts(base=BROWSER_TIMEOUT_BASE)
        headless, reason = resolve_browser_headless(
            self.requested_headless,
            default_headless=False,
            override_env_names=("GROK_HEADLESS", "PLAYWRIGHT_HEADLESS", "REGISTER_HEADLESS"),
        )
        ensure_browser_display_available(headless)
        self.browser_headless = bool(headless)
        self.log(f"browser mode: {'headless' if headless else 'headed'} ({reason})")

        extension_path = self._turnstile_extension_path()
        if not extension_path.exists():
            raise RuntimeError(f"missing turnstile extension: {extension_path}")
        self.co.add_extension(str(extension_path))

        browser_path = self._resolve_browser_path()
        if browser_path:
            self.co.set_browser_path(browser_path)
            self.log(f"browser path: {browser_path}")

        self.co.set_argument(f"--window-size={BROWSER_WINDOW_WIDTH},{BROWSER_WINDOW_HEIGHT}")
        for arg in BROWSER_LAUNCH_ARGS:
            self.co.set_argument(arg)
        if headless:
            self.co.set_argument("--headless=new")
        if self.proxy:
            self.co.set_proxy(self.proxy)

    def _wait_until(self, fn: Callable[[], bool], timeout: float = 30.0, interval: float = 0.5, desc: str = "") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return
            time.sleep(interval)
        raise TimeoutError(desc or "wait timeout")

    @staticmethod
    def _current_url(page) -> str:
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _body_text(page) -> str:
        try:
            return str(page.run_js("return document.body ? document.body.innerText : '';") or "")
        except Exception:
            return ""

    @staticmethod
    def _page_title(page) -> str:
        try:
            title = getattr(page, "title", "")
            if callable(title):
                title = title()
            return str(title or "")
        except Exception:
            return ""

    def _random_delay(
        self,
        min_delay: float = ANTI_DETECTION_MIN_DELAY,
        max_delay: float = ANTI_DETECTION_MAX_DELAY,
    ) -> None:
        time.sleep(random.uniform(min_delay, max_delay))

    def start_browser(self):
        self.log("starting DrissionPage Chrome")
        self.browser = Chromium(self.co)
        self.page = self.refresh_active_page()
        if self._current_url(self.page).startswith("chrome://settings/triggeredResetProfileSettings"):
            self.page = self.browser.new_tab(SIGNUP_URL)
        return self.page

    def stop_browser(self) -> None:
        if self.browser is not None:
            try:
                self.browser.quit()
            except Exception:
                pass
        self.browser = None
        self.page = None

    def refresh_active_page(self):
        if self.browser is None:
            raise RuntimeError("browser is not started")
        tabs = self.browser.get_tabs() or []
        preferred = None
        for tab in tabs:
            url = self._current_url(tab)
            if "accounts.x.ai" in url or "grok.com" in url or "x.ai" in url:
                preferred = tab
                break
        if preferred is None:
            for tab in tabs:
                if not self._current_url(tab).startswith("chrome://"):
                    preferred = tab
                    break
        if preferred is None:
            preferred = tabs[-1] if tabs else self.browser.new_tab()
        self.page = preferred
        if self._current_url(self.page).startswith("chrome://settings/triggeredResetProfileSettings"):
            self.page = self.browser.new_tab(SIGNUP_URL)
        return self.page

    @staticmethod
    def _apply_turnstile_mouse_patch(target) -> bool:
        try:
            target.run_js(TURNSTILE_CHALLENGE_PATCH_SCRIPT)
            return True
        except Exception:
            return False

    def _raise_if_cloudflare_gate(self, page) -> None:
        body = self._body_text(page).strip().lower()
        if "blocked due to abusive traffic patterns" in body:
            raise RuntimeError("grok signup blocked by cloudflare")
        if "attention required" in body and "cloudflare" in body:
            raise RuntimeError("grok signup landed on cloudflare challenge")

    @staticmethod
    def _debug_output_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "runtime-logs" / "grok-failures"

    def _dump_debug_artifacts(self, page, reason: str = "failure") -> list[str]:
        saved: list[str] = []
        try:
            out_dir = self._debug_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = out_dir / f"{stamp}-{reason}"
            current_url = self._current_url(page)
            page_title = self._page_title(page)
            body_text = self._body_text(page)

            screenshot_path = base.with_suffix(".png")
            try:
                page.get_screenshot(path=str(screenshot_path))
                saved.append(str(screenshot_path))
            except Exception:
                pass

            html_path = base.with_suffix(".html")
            html = ""
            try:
                html = str(page.html or "")
            except Exception:
                pass
            if not html:
                try:
                    html = str(page.run_js("return document.documentElement.outerHTML;") or "")
                except Exception:
                    html = ""
            if not html:
                fallback_text = (
                    f"reason: {reason}\n"
                    f"url: {current_url}\n"
                    f"title: {page_title}\n\n"
                    f"{body_text}"
                )
                html = f"<html><body><pre>{html_lib.escape(fallback_text)}</pre></body></html>"
            html_path.write_text(html, encoding="utf-8")
            saved.append(str(html_path))

            text_path = base.with_suffix(".txt")
            text_path.write_text(
                "\n".join(
                    [
                        f"reason: {reason}",
                        f"url: {current_url}",
                        f"title: {page_title}",
                        "",
                        body_text,
                    ]
                ),
                encoding="utf-8",
            )
            saved.append(str(text_path))
        except Exception as exc:
            self.log(f"debug artifact dump failed: {exc}")
        return saved

    def _hold_browser_for_debug(self) -> None:
        if self.browser_headless or not self.keep_browser_open_on_failure or self.failure_hold_seconds <= 0:
            return
        self.log(f"hold browser for debug: {self.failure_hold_seconds}s")
        time.sleep(self.failure_hold_seconds)

    def open_signup_page(self):
        self.log("step1: open signup page")
        page = self.refresh_active_page()
        try:
            page.get(SIGNUP_URL)
        except Exception:
            page = self.browser.new_tab(SIGNUP_URL)
            self.page = page
        self._random_delay()
        self._raise_if_cloudflare_gate(page)
        self.click_email_signup_button()
        return self.page

    def click_email_signup_button(self, timeout: int = 20) -> None:
        script = r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('邮箱注册') || text.includes('email');
});
if (!target) return false;
target.click();
return true;
"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            page = self.refresh_active_page()
            if bool(page.run_js(script)):
                self._random_delay(0.5, 1.2)
                return
            time.sleep(0.5)
        raise RuntimeError("email signup entry not found")

    def _submit_email(self, page, email: str) -> None:
        self.log(f"step2: submit email {email}")
        deadline = time.time() + EMAIL_STEP_TIMEOUT
        while time.time() < deadline:
            self._raise_if_cloudflare_gate(page)
            filled = page.run_js(
                """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
                """,
                email,
            )

            if filled == "not-ready":
                time.sleep(0.5)
                continue

            if filled != "filled":
                time.sleep(0.5)
                continue

            self._random_delay()
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '注册' || text.includes('注册');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                self.log(f"email submitted: {email}")
                return

            time.sleep(0.5)

        raise RuntimeError("email input or submit button not found")

    def _submit_otp(self, page, code: str) -> None:
        self.log(f"step3: submit otp {code}")
        if self.has_profile_form():
            return

        normalized = str(code or "").strip().upper()
        if not normalized:
            raise RuntimeError("otp is empty")

        deadline = time.time() + OTP_STEP_TIMEOUT
        while time.time() < deadline:
            self._raise_if_cloudflare_gate(page)
            if self.has_profile_form():
                return

            try:
                filled = page.run_js(
                    """
const rawCode = String(arguments[0] || '').trim().toUpperCase();
const compactCode = rawCode.replace(/[^A-Z0-9]/g, '');
const dashedCode = compactCode.length === 6 ? `${compactCode.slice(0, 3)}-${compactCode.slice(3)}` : rawCode;
const candidateCodes = Array.from(new Set([rawCode, dashedCode, compactCode].filter(Boolean)));

if (!compactCode) {
    return 'invalid-code';
}

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || compactCode.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < compactCode.length) {
    return 'not-ready';
}

if (input) {
    let accepted = false;
    let finalValue = '';
    const expectedLength = Number(input.maxLength || 0);

    for (const candidate of candidateCodes) {
        input.focus();
        input.click();
        setNativeValue(input, candidate);
        dispatchInputEvents(input, candidate);

        const normalizedValue = String(input.value || '').trim().toUpperCase();
        const normalizedCompact = normalizedValue.replace(/[^A-Z0-9]/g, '');
        const lengthOk = expectedLength <= 0 || normalizedValue.length === expectedLength || normalizedCompact.length === expectedLength;
        if (normalizedCompact === compactCode && lengthOk) {
            accepted = true;
            finalValue = normalizedValue;
            break;
        }
    }

    if (!accepted) {
        return 'aggregate-mismatch';
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (slots.length && filledSlots && filledSlots !== compactCode.length && filledSlots !== finalValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, compactCode.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = compactCode[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged.toUpperCase() === compactCode ? 'filled' : 'box-mismatch';
                    """,
                    normalized,
                )
            except PageDisconnectedError:
                self.refresh_active_page()
                if self.has_profile_form():
                    return
                time.sleep(1)
                continue

            if filled == "not-ready":
                if self.has_profile_form():
                    return
                time.sleep(0.5)
                continue

            if filled != "filled":
                time.sleep(0.5)
                continue

            self._random_delay(1, 2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || text === 'Confirmemail' || text.includes('Confirm');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                self.refresh_active_page()
                if self.has_profile_form():
                    return
                clicked = "disconnected"

            if clicked == "clicked":
                self._random_delay(2, 3)
                self.refresh_active_page()
                if self.has_profile_form():
                    return
                return

            if clicked == "no-button":
                current_url = self._current_url(self.page)
                if self.has_profile_form():
                    return
                if "sign-up" in current_url or "signup" in current_url:
                    return

            if clicked == "disconnected":
                time.sleep(1)
                continue

            time.sleep(0.5)

        if self.has_profile_form():
            return

        raise RuntimeError(f"otp input or confirm button not found (url: {self._current_url(self.page)})")

    def has_profile_form(self) -> bool:
        page = self.refresh_active_page()
        try:
            return bool(
                page.run_js(
                    """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
                    """
                )
            )
        except Exception:
            return False

    def _build_profile(
        self,
        given_name: str,
        family_name: str,
        password: str,
    ) -> dict[str, str]:
        return {
            "given_name": given_name,
            "family_name": family_name,
            "password": password,
        }

    def _fill_profile_and_submit(
        self,
        page,
        given_name: str,
        family_name: str,
        password: str,
        timeout: int = PROFILE_STEP_TIMEOUT,
    ) -> dict[str, str]:
        self.log(f"step4: fill profile and submit {given_name} {family_name}")
        deadline = time.time() + timeout
        turnstile_token = ""

        while time.time() < deadline:
            self._raise_if_cloudflare_gate(page)
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
                """,
                given_name,
                family_name,
                password,
            )

            if filled == "not-ready":
                time.sleep(0.5)
                continue

            if filled != "filled":
                time.sleep(0.5)
                continue

            values_ok = page.run_js(
                """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
                """,
                given_name,
                family_name,
                password,
            )

            if not values_ok:
                time.sleep(0.5)
                continue

            turnstile_state = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
                """
            )

            if turnstile_state == "pending" and not turnstile_token:
                try:
                    turnstile_token = self._solve_turnstile_on_page(page)
                except Exception:
                    turnstile_token = ""

                if turnstile_token:
                    synced = page.run_js(
                        """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                        """,
                        turnstile_token,
                    )
                    if synced:
                        self.log("turnstile token synced to profile form")

            self._random_delay(1, 2)

            try:
                submit_button = page.ele("tag:button@@text()=完成注册")
            except Exception:
                submit_button = None

            if not submit_button:
                clicked = page.run_js(
                    r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '完成注册' || text.includes('完成注册');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.focus();
submitButton.click();
return true;
                    """
                )
            else:
                challenge_value = page.run_js(
                    """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                    """
                )
                if challenge_value not in ("not-found", ""):
                    submit_button.click()
                    clicked = True
                else:
                    clicked = False

            if clicked:
                self.log(f"profile submitted: {given_name} {family_name}")
                return self._build_profile(given_name, family_name, password)

            time.sleep(0.5)

        raise RuntimeError("final register form or submit button not found")

    @staticmethod
    def _read_turnstile_token(page) -> str:
        return str(
            page.run_js(
                """
return (
    (typeof window.turnstile !== 'undefined' && typeof window.turnstile.getResponse === 'function' && window.turnstile.getResponse())
    || document.querySelector('input[id^="cf-chl-widget-"]')?.value
    || document.querySelector('input[name="cf-turnstile-response"]')?.value
    || ''
);
                """
            )
            or ""
        )

    @staticmethod
    def _read_turnstile_sitekey(page) -> str:
        return str(
            page.run_js(
                """
const byData = document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey');
if (byData) return byData;
for (const iframe of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
    try {
        const url = new URL(iframe.src, location.href);
        const key = url.searchParams.get('k');
        if (key) return key;
    } catch (error) {}
}
return '';
                """
            )
            or ""
        )

    @staticmethod
    def _inject_turnstile_token(page, token: str) -> bool:
        return bool(
            page.run_js(
                """
const token = arguments[0];
const selectors = ['input[id^="cf-chl-widget-"]', 'input[name="cf-turnstile-response"]', 'textarea[name="cf-turnstile-response"]'];
let touched = 0;
for (const sel of selectors) {
    document.querySelectorAll(sel).forEach((node) => {
        node.value = token;
        node.setAttribute('value', token);
        node.dispatchEvent(new Event('input', { bubbles: true }));
        node.dispatchEvent(new Event('change', { bubbles: true }));
        touched += 1;
    });
}
if (!touched) {
    const fallback = document.createElement('input');
    fallback.type = 'hidden';
    fallback.name = 'cf-turnstile-response';
    fallback.value = token;
    document.body.appendChild(fallback);
    touched += 1;
}
return touched > 0;
                """,
                token,
            )
        )

    def _solve_turnstile_by_solver(self, page) -> str:
        if not self.captcha_solver:
            return ""
        solver_name = type(self.captcha_solver).__name__.lower()
        if "manual" in solver_name:
            return ""
        sitekey = self._read_turnstile_sitekey(page)
        if not sitekey:
            return ""
        token = self.captcha_solver.solve_turnstile(self._current_url(page), sitekey)
        if token and self._inject_turnstile_token(page, token):
            return token
        return ""

    def _solve_turnstile_on_page(self, page) -> str:
        self.log("step5: solve turnstile")
        last_error = None
        try:
            page.run_js("try { turnstile.reset() } catch (e) {}")
        except Exception:
            pass

        for attempt in range(15):
            try:
                self._raise_if_cloudflare_gate(page)
                token = page.run_js(
                    "try { return turnstile.getResponse() } catch(e) { return null }"
                )
                if token:
                    self._inject_turnstile_token(page, token)
                    return str(token)
                challenge_solution = page.ele("@name=cf-turnstile-response")
                challenge_wrapper = challenge_solution.parent()
                challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
                challenge_iframe.run_js(TURNSTILE_CHALLENGE_PATCH_SCRIPT)
                challenge_body = challenge_iframe.ele("tag:body").shadow_root
                challenge_button = challenge_body.ele("tag:input")
                self.log(f"turnstile direct click #{attempt + 1}")
                challenge_button.click()
            except PageDisconnectedError:
                page = self.refresh_active_page()
                self.page = page
            except Exception as exc:
                last_error = str(exc)
            self._random_delay(0.5, 1.5)

        self._raise_if_cloudflare_gate(page)
        token = self._read_turnstile_token(page)
        if token:
            self._inject_turnstile_token(page, token)
            return token
        token = self._solve_turnstile_by_solver(page)
        if token:
            return token
        raise RuntimeError(last_error or "turnstile solve failed")

    @staticmethod
    def _cookie_name(cookie: Any) -> str:
        if isinstance(cookie, dict):
            return str(cookie.get("name", "") or "")
        return str(getattr(cookie, "name", "") or "")

    @staticmethod
    def _cookie_value(cookie: Any) -> str:
        if isinstance(cookie, dict):
            return str(cookie.get("value", "") or "")
        return str(getattr(cookie, "value", "") or "")

    @staticmethod
    def _cookie_domain(cookie: Any) -> str:
        if isinstance(cookie, dict):
            return str(cookie.get("domain", "") or "")
        return str(getattr(cookie, "domain", "") or "")

    @classmethod
    def _has_auth_cookies(cls, cookies: list[Any]) -> bool:
        return any(cls._cookie_name(cookie) in {"sso", "sso-rw"} for cookie in cookies)

    def _collect_all_cookies(self) -> list[Any]:
        pages_to_check: list[Any] = []
        if self.page is not None:
            pages_to_check.append(self.page)
        try:
            tabs = self.browser.get_tabs() if self.browser is not None else []
            for tab in tabs:
                if tab not in pages_to_check:
                    pages_to_check.append(tab)
        except Exception:
            pass

        out: list[Any] = []
        seen: set[tuple[str, str, str]] = set()
        for tab in pages_to_check:
            for kwargs in ({"all_domains": True, "all_info": True}, {"all_domains": False, "all_info": True}):
                try:
                    cookies = tab.cookies(**kwargs) or []
                except Exception:
                    continue
                for cookie in cookies:
                    key = (self._cookie_name(cookie), self._cookie_domain(cookie), self._cookie_value(cookie))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(cookie)
        return out

    def _submit_register(self, page) -> None:
        self.log("step6: submit final register")
        if not self._read_turnstile_token(page):
            self._solve_turnstile_on_page(page)
        page.run_js(
            r"""
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'));
const target = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('完成注册') || text.includes('createaccount') || text.includes('signup') || text.includes('register');
});
if (target) target.click();
return !!target;
            """
        )

    def _accept_tos_if_needed(self, page) -> None:
        if self._has_auth_cookies(self._collect_all_cookies()):
            return
        page.run_js(
            r"""
const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
for (const box of boxes.slice(0, 2)) {
    if (!box.checked) box.click();
}
const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
const nextBtn = buttons.find((node) => {
    const text = (node.innerText || node.textContent || node.value || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('继续') || text.includes('accept') || text.includes('continue') || text.includes('同意');
});
if (nextBtn) nextBtn.click();
return true;
            """
        )

    def _wait_for_auth_cookies(self, timeout: int = SSO_COOKIE_TIMEOUT) -> list[Any]:
        deadline = time.time() + timeout
        last_seen_names: set[str] = set()
        last_progress_log_at = 0.0
        home_refreshed = False

        while time.time() < deadline:
            try:
                self.refresh_active_page()
            except Exception:
                pass

            pages_to_check = []
            if self.page is not None:
                pages_to_check.append(self.page)

            try:
                tabs = self.browser.get_tabs() if self.browser is not None else []
                for tab in tabs:
                    if tab is not None and tab not in pages_to_check:
                        pages_to_check.append(tab)
            except Exception:
                pass

            if not pages_to_check:
                time.sleep(0.5)
                continue

            for tab in pages_to_check:
                for cookie_kwargs in (
                    {"all_domains": True, "all_info": True},
                    {"all_domains": False, "all_info": True},
                ):
                    try:
                        cookies = tab.cookies(**cookie_kwargs) or []
                    except PageDisconnectedError:
                        continue
                    except Exception:
                        continue

                    for item in cookies:
                        name = self._cookie_name(item).strip()
                        value = self._cookie_value(item).strip()
                        if name:
                            last_seen_names.add(name)
                        if name.lower() == "sso" and value:
                            return self._collect_all_cookies()

            now = time.time()
            if now - last_progress_log_at >= 10:
                remain = max(0, int(deadline - now))
                self.log(
                    f"waiting sso cookie, remain ~{remain}s, seen: {sorted(last_seen_names)[:12]}"
                )
                last_progress_log_at = now

            if not home_refreshed and now - (deadline - timeout) > 8 and self.page is not None:
                current_url = self._current_url(self.page)
                if "grok.com" in current_url or "x.ai" in current_url:
                    try:
                        self.page.get("https://grok.com")
                        home_refreshed = True
                    except Exception:
                        pass

            time.sleep(0.5)

        raise RuntimeError(f"sso cookie not found, seen cookies: {sorted(last_seen_names)}")

    @classmethod
    def _pick_cookie(cls, cookies: list[Any], name: str) -> str:
        domains = [".x.ai", "accounts.x.ai", ".grok.com", ".grokusercontent.com", ".grokipedia.com"]
        for domain in domains:
            for cookie in cookies:
                if cls._cookie_name(cookie) == name and cls._cookie_domain(cookie) == domain:
                    return cls._cookie_value(cookie)
        for cookie in cookies:
            if cls._cookie_name(cookie) == name:
                return cls._cookie_value(cookie)
        return ""

    def register(self, email: str, password: Optional[str] = None, otp_callback: Optional[Callable[[], str]] = None) -> dict[str, Any]:
        if not password:
            password = _rand_password()
        given_name = _rand_name()
        family_name = _rand_name()

        try:
            page = self.start_browser()
            self._apply_turnstile_mouse_patch(page)
            page = self.open_signup_page()
            self._submit_email(page, email)
            if otp_callback:
                self.log("waiting otp callback")
                code = otp_callback() or ""
            else:
                code = input("otp code: ").strip()
            if not code:
                raise RuntimeError("otp not available")
            page = self.refresh_active_page()
            self._submit_otp(page, code)
            self._wait_until(lambda: self.has_profile_form(), timeout=25, interval=0.5, desc="profile form not ready")
            page = self.refresh_active_page()
            self._fill_profile_and_submit(page, given_name, family_name, password)
            cookies = self._wait_for_auth_cookies()
            sso = self._pick_cookie(cookies, "sso")
            sso_rw = self._pick_cookie(cookies, "sso-rw")
            if not sso:
                raise RuntimeError("register finished but sso cookie missing")
            self.log("grok registration flow completed")
            return {
                "email": email,
                "password": password,
                "given_name": given_name,
                "family_name": family_name,
                "sso": sso,
                "sso_rw": sso_rw,
                "cookies": cookies,
            }
        except Exception:
            if self.page is not None:
                self._dump_debug_artifacts(self.page, "register-failure")
            self._hold_browser_for_debug()
            raise
        finally:
            self.stop_browser()
