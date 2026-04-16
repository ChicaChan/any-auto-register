from platforms.grok import grok2api_upload
from services import grok2api_runtime


class DummyAccount:
    def __init__(self, token: str = "sso-token", email: str = "test@example.com"):
        self.email = email
        self.token = ""
        self.extra = {"sso": token}


class StoredAccountLike:
    def __init__(self, token: str = "stored-sso-token", email: str = "stored@example.com"):
        self.email = email
        self.token = ""
        self.extra_json = f'{{"sso":"{token}"}}'

    def get_extra(self):
        return {"sso": "stored-sso-token"}


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_upload_to_grok2api_uses_modern_admin_add_route(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, **kwargs):
        calls.append((url, headers, json, kwargs))
        if url.endswith("/admin/api/tokens/add"):
            return FakeResponse(200, {"status": "success", "count": 1, "skipped": 0})
        raise AssertionError(url)

    monkeypatch.setattr(grok2api_upload.cffi_requests, "post", fake_post)

    ok, msg = grok2api_upload.upload_to_grok2api(
        DummyAccount(),
        api_url="http://127.0.0.1:8011",
        app_key="grok2api",
        pool_name="ssoBasic",
    )

    assert ok is True
    assert "新增 1 个 token" in msg
    assert len(calls) == 1
    url, headers, payload, kwargs = calls[0]
    assert url == "http://127.0.0.1:8011/admin/api/tokens/add"
    assert headers["Authorization"] == "Bearer grok2api"
    assert payload == {"tokens": ["sso-token"], "pool": "basic", "tags": []}
    assert kwargs["impersonate"] == "chrome110"


def test_upload_to_grok2api_reads_sso_from_account_model_like_object(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, **kwargs):
        calls.append((url, headers, json, kwargs))
        if url.endswith("/admin/api/tokens/add"):
            return FakeResponse(200, {"status": "success", "count": 1, "skipped": 0})
        raise AssertionError(url)

    monkeypatch.setattr(grok2api_upload.cffi_requests, "post", fake_post)

    ok, msg = grok2api_upload.upload_to_grok2api(
        StoredAccountLike(),
        api_url="http://127.0.0.1:8011",
        app_key="grok2api",
    )

    assert ok is True
    assert "token" in msg
    assert calls[0][2]["tokens"] == ["stored-sso-token"]


def test_upload_to_grok2api_falls_back_to_legacy_route(monkeypatch):
    get_calls = []
    post_calls = []

    def fake_get(url, headers=None, **kwargs):
        get_calls.append((url, headers, kwargs))
        if url.endswith("/v1/admin/tokens"):
            return FakeResponse(200, {"tokens": {"ssoBasic": []}})
        raise AssertionError(url)

    def fake_post(url, headers=None, json=None, **kwargs):
        post_calls.append((url, headers, json, kwargs))
        if url.endswith("/admin/api/tokens/add"):
            return FakeResponse(404, text="not found")
        if url.endswith("/v1/admin/tokens"):
            return FakeResponse(200, {"status": "success"})
        raise AssertionError(url)

    monkeypatch.setattr(grok2api_upload.cffi_requests, "get", fake_get)
    monkeypatch.setattr(grok2api_upload.cffi_requests, "post", fake_post)

    ok, msg = grok2api_upload.upload_to_grok2api(
        DummyAccount(),
        api_url="http://127.0.0.1:8011",
        app_key="grok2api",
        pool_name="basic",
    )

    assert ok is True
    assert msg == "导入成功"
    assert get_calls[0][0] == "http://127.0.0.1:8011/v1/admin/tokens"
    assert get_calls[0][1]["X-Admin-Username"] == "admin"
    assert post_calls[1][0] == "http://127.0.0.1:8011/v1/admin/tokens"
    legacy_payload = post_calls[1][2]
    assert "ssoBasic" in legacy_payload
    assert legacy_payload["ssoBasic"][0]["token"] == "sso-token"
    assert legacy_payload["ssoBasic"][0]["quota"] == 80


def test_verify_grok2api_supports_modern_route(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        if url.endswith("/admin/api/verify"):
            return FakeResponse(200, {"status": "success"})
        raise AssertionError(url)

    monkeypatch.setattr(grok2api_runtime.requests, "get", fake_get)

    ok, msg = grok2api_runtime.verify_grok2api(
        api_url="http://127.0.0.1:8011",
        app_key="grok2api",
    )

    assert ok is True
    assert msg == "grok2api 鉴权正常"
    assert calls == [
        (
            "http://127.0.0.1:8011/admin/api/verify",
            {
                "Authorization": "Bearer grok2api",
                "X-Admin-Username": "admin",
            },
        )
    ]


def test_verify_grok2api_falls_back_to_legacy_route(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/admin/api/verify"):
            return FakeResponse(404, text="not found")
        if url.endswith("/v1/admin/verify"):
            return FakeResponse(200, {"status": "success"})
        raise AssertionError(url)

    monkeypatch.setattr(grok2api_runtime.requests, "get", fake_get)

    ok, msg = grok2api_runtime.verify_grok2api(
        api_url="http://127.0.0.1:8011",
        app_key="grok2api",
    )

    assert ok is True
    assert msg == "grok2api 鉴权正常"
    assert calls == [
        "http://127.0.0.1:8011/admin/api/verify",
        "http://127.0.0.1:8011/v1/admin/verify",
    ]


def test_ensure_grok2api_ready_does_not_restart_local_service_for_remote_url(monkeypatch):
    monkeypatch.setattr(
        grok2api_runtime,
        "_get_config",
        lambda key, default="": {
            "grok2api_url": "http://49.51.198.31:18001",
            "grok2api_app_key": "remote-key",
        }.get(key, default),
    )
    monkeypatch.setattr(
        grok2api_runtime,
        "verify_grok2api",
        lambda api_url=None, app_key=None, admin_username=None: (False, "grok2api 鉴权失败"),
    )

    ok, msg = grok2api_runtime.ensure_grok2api_ready()

    assert ok is False
    assert msg == "grok2api 鉴权失败"
