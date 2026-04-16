from platforms.grok.core import (
    BROWSER_LAUNCH_ARGS,
    DEFAULT_FAILURE_HOLD_SECONDS,
    GrokRegister,
    SIGNUP_URL,
    TURNSTILE_CHALLENGE_PATCH_SCRIPT,
    TURNSTILE_MOUSE_PATCH_SCRIPT,
)


class DummyPage:
    def __init__(self, result=None):
        self.result = result
        self.calls = []
        self.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        self.title = "Grok"

    def run_js(self, script, *args, **kwargs):
        self.calls.append((script, args, kwargs))
        return self.result


def test_grok_signup_url_uses_grok_redirect():
    assert SIGNUP_URL == "https://accounts.x.ai/sign-up?redirect=grok-com"


def test_grok_browser_uses_incognito():
    assert BROWSER_LAUNCH_ARGS == ["--incognito"]


def test_turnstile_patch_script_overrides_mouse_screen_coordinates():
    assert "MouseEvent.prototype" in TURNSTILE_MOUSE_PATCH_SCRIPT
    assert "screenX" in TURNSTILE_MOUSE_PATCH_SCRIPT
    assert "screenY" in TURNSTILE_MOUSE_PATCH_SCRIPT
    assert "window.dtp = 1;" in TURNSTILE_CHALLENGE_PATCH_SCRIPT


def test_read_turnstile_token_checks_turnstile_api_before_hidden_input():
    page = DummyPage(result="mock-token")

    token = GrokRegister._read_turnstile_token(page)

    assert token == "mock-token"
    assert "turnstile.getResponse" in page.calls[0][0]
    assert "cf-turnstile-response" in page.calls[0][0]


def test_apply_turnstile_mouse_patch_injects_script():
    page = DummyPage()

    applied = GrokRegister._apply_turnstile_mouse_patch(page)

    assert applied is True
    assert page.calls[0][0] == TURNSTILE_CHALLENGE_PATCH_SCRIPT


def test_turnstile_extension_assets_exist():
    ext = GrokRegister._turnstile_extension_path()
    assert ext.is_dir()
    assert (ext / "manifest.json").is_file()
    assert (ext / "script.js").is_file()


def test_constructor_sets_incognito_and_extension():
    register = GrokRegister(headless=True)

    assert "--incognito" in register.co.arguments
    assert "--window-size=1280,800" in register.co.arguments
    assert not any(arg.startswith("--user-agent=") for arg in register.co.arguments)
    assert str(GrokRegister._turnstile_extension_path()) in register.co.extensions
    assert register.keep_browser_open_on_failure is False
    assert register.failure_hold_seconds == 0


def test_constructor_defaults_hold_when_headed():
    register = GrokRegister(headless=True, keep_browser_open_on_failure=True)

    assert register.keep_browser_open_on_failure is True
    assert register.failure_hold_seconds == DEFAULT_FAILURE_HOLD_SECONDS


def test_dump_debug_artifacts_writes_text_fallback_when_html_missing(tmp_path, monkeypatch):
    register = GrokRegister(headless=True)
    monkeypatch.setattr(register, "_debug_output_dir", lambda: tmp_path)

    class DebugPage(DummyPage):
        html = ""

        def get_screenshot(self, path=None, **kwargs):
            with open(path, "wb") as fh:
                fh.write(b"png")

        def run_js(self, script, *args, **kwargs):
            self.calls.append((script, args, kwargs))
            if "document.documentElement.outerHTML" in script:
                return ""
            if "document.body ? document.body.innerText" in script:
                return "Blocked due to abusive traffic patterns"
            return super().run_js(script, *args, **kwargs)

    saved = register._dump_debug_artifacts(DebugPage(), "probe")

    assert any(path.endswith(".png") for path in saved)
    assert any(path.endswith(".html") for path in saved)
    assert any(path.endswith(".txt") for path in saved)
    text_file = next(path for path in saved if path.endswith(".txt"))
    assert "Blocked due to abusive traffic patterns" in open(text_file, encoding="utf-8").read()
