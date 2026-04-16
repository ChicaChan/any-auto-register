from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

import requests

DEFAULT_API_URL = "http://127.0.0.1:8011"
DEFAULT_APP_KEY = "grok2api"
DEFAULT_ADMIN_USERNAME = "admin"


def _get_config(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _verify_endpoints(api_url: str) -> list[str]:
    base = api_url.rstrip("/")
    return [
        f"{base}/admin/api/verify",
        f"{base}/v1/admin/verify",
    ]


def _is_local_api_url(api_url: str) -> bool:
    host = (urlparse(api_url).hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _build_headers(app_key: str, admin_username: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {app_key}",
    }
    normalized_admin_username = str(
        admin_username
        or _get_config("grok2api_admin_username", "")
        or DEFAULT_ADMIN_USERNAME
    ).strip()
    if normalized_admin_username:
        headers["X-Admin-Username"] = normalized_admin_username
    return headers


def verify_grok2api(
    api_url: str | None = None,
    app_key: str | None = None,
    admin_username: str | None = None,
) -> Tuple[bool, str]:
    api_url = str(api_url or _get_config("grok2api_url", "")).strip()
    app_key = str(app_key or _get_config("grok2api_app_key", "")).strip()

    if not api_url:
        return False, "grok2api URL 未配置"
    if not app_key:
        return False, "grok2api App Key 未配置"

    last_error = ""
    for verify_url in _verify_endpoints(api_url):
        try:
            resp = requests.get(
                verify_url,
                headers=_build_headers(app_key, admin_username=admin_username),
                timeout=10,
            )
        except Exception as e:
            last_error = f"grok2api 连接失败: {e}"
            continue

        if resp.status_code == 200:
            return True, "grok2api 鉴权正常"
        if resp.status_code in (404, 405):
            continue
        return False, f"grok2api 鉴权失败: HTTP {resp.status_code} - {resp.text[:200]}"

    return False, last_error or "未找到兼容的 grok2api 管理接口"


def ensure_grok2api_ready() -> Tuple[bool, str]:
    api_url = _get_config("grok2api_url", DEFAULT_API_URL)
    app_key = _get_config("grok2api_app_key", DEFAULT_APP_KEY)
    admin_username = _get_config("grok2api_admin_username", DEFAULT_ADMIN_USERNAME)

    ok, msg = verify_grok2api(
        api_url=api_url,
        app_key=app_key,
        admin_username=admin_username,
    )
    if ok:
        return True, msg
    if not _is_local_api_url(api_url):
        return False, msg

    from services.external_apps import list_status, start, stop

    try:
        status = next((item for item in list_status() if item["name"] == "grok2api"), None)
        if status and not status.get("repo_exists"):
            return False, "grok2api 未安装，请先到“设置 → 插件”里手动安装"
        running = bool(status and status.get("running"))

        if running:
            stop("grok2api")
        start("grok2api")
    except Exception as e:
        return False, f"{msg}; 自动重启 grok2api 失败: {e}"

    return verify_grok2api(
        api_url=api_url,
        app_key=app_key,
        admin_username=admin_username,
    )
