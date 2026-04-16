"""grok2api 自动导入。"""

from __future__ import annotations

import json
import logging
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8011"
DEFAULT_APP_KEY = "grok2api"
DEFAULT_ADMIN_USERNAME = "admin"
LEGACY_DEFAULT_POOL = "ssoBasic"
MODERN_DEFAULT_POOL = "basic"
DEFAULT_QUOTAS = {
    "basic": 80,
    "ssoBasic": 80,
    "super": 140,
    "ssoSuper": 140,
}
MODERN_POOL_ALIASES = {
    "basic": "basic",
    "ssobasic": "basic",
    "super": "super",
    "ssosuper": "super",
    "auto": "auto",
}
LEGACY_POOL_ALIASES = {
    "basic": "ssoBasic",
    "ssobasic": "ssoBasic",
    "super": "ssoSuper",
    "ssosuper": "ssoSuper",
}


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "")
    except Exception:
        return ""


def _normalize_quota(pool_name: str, quota) -> int:
    if quota not in (None, ""):
        try:
            return int(quota)
        except Exception:
            pass
    return DEFAULT_QUOTAS.get(pool_name, DEFAULT_QUOTAS[LEGACY_DEFAULT_POOL])


def _normalize_pool_name(pool_name: str | None, *, modern: bool) -> str:
    raw = str(pool_name or _get_config_value("grok2api_pool") or "").strip()
    if not raw:
        return MODERN_DEFAULT_POOL if modern else LEGACY_DEFAULT_POOL

    key = raw.lower()
    if modern:
        return MODERN_POOL_ALIASES.get(key, raw)
    return LEGACY_POOL_ALIASES.get(key, raw)


def _get_account_extra(account) -> dict[str, Any]:
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        return extra

    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            resolved = getter()
        except Exception:
            resolved = None
        if isinstance(resolved, dict):
            return resolved

    raw_extra = getattr(account, "extra_json", None)
    if isinstance(raw_extra, str) and raw_extra.strip():
        try:
            resolved = json.loads(raw_extra)
        except Exception:
            resolved = None
        if isinstance(resolved, dict):
            return resolved

    return {}


def _extract_sso(account) -> str:
    extra = _get_account_extra(account)
    token = (
        extra.get("sso")
        or extra.get("sso_token")
        or extra.get("sso_rw")
        or getattr(account, "token", "")
    )
    token = str(token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def build_grok2api_payload(
    account,
    pool_name: str | None = None,
    quota=None,
) -> dict:
    token = _extract_sso(account)
    if not token:
        raise ValueError("账号缺少 sso token")

    normalized_pool_name = _normalize_pool_name(pool_name, modern=False)
    email = getattr(account, "email", "")
    payload = {
        normalized_pool_name: [
            {
                "token": token,
                "status": "active",
                "quota": _normalize_quota(
                    normalized_pool_name,
                    quota or _get_config_value("grok2api_quota"),
                ),
                "tags": [],
                "note": f"auto-import:{email}" if email else "auto-import",
            }
        ]
    }
    return payload


def _build_modern_add_payload(account, pool_name: str | None = None) -> dict[str, Any]:
    token = _extract_sso(account)
    if not token:
        raise ValueError("账号缺少 sso token")
    return {
        "tokens": [token],
        "pool": _normalize_pool_name(pool_name, modern=True),
        "tags": [],
    }


def _request_options() -> dict:
    return {
        "proxies": None,
        "verify": False,
        "timeout": 30,
        "impersonate": "chrome110",
    }


def _build_headers(app_key: str, admin_username: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {app_key}",
        "Content-Type": "application/json",
    }
    normalized_admin_username = str(
        admin_username
        or _get_config_value("grok2api_admin_username")
        or DEFAULT_ADMIN_USERNAME
    ).strip()
    if normalized_admin_username:
        headers["X-Admin-Username"] = normalized_admin_username
    return headers


def _build_token_item(account, pool_name: str | None = None, quota=None) -> tuple[str, dict]:
    payload = build_grok2api_payload(account, pool_name=pool_name, quota=quota)
    normalized_pool_name = next(iter(payload.keys()))
    return normalized_pool_name, payload[normalized_pool_name][0]


def _response_message(resp, default_message: str) -> str:
    try:
        detail = resp.json()
    except Exception:
        detail = None

    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            value = str(detail.get(key, "") or "").strip()
            if value:
                return value

    body = str(getattr(resp, "text", "") or "").strip()
    return f"{default_message} - {body[:200]}" if body else default_message


def _load_existing_legacy_tokens(api_url: str, headers: dict) -> dict | None:
    resp = cffi_requests.get(
        f"{api_url.rstrip('/')}/v1/admin/tokens",
        headers=headers,
        **_request_options(),
    )
    if resp.status_code in (404, 405):
        return None
    if resp.status_code != 200:
        raise RuntimeError(_response_message(resp, f"读取现有 tokens 失败: HTTP {resp.status_code}"))

    data = resp.json()
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        raise RuntimeError("读取现有 tokens 失败: 响应格式异常")
    return tokens


def _merge_token(existing_tokens: dict, pool_name: str, token_item: dict) -> dict:
    merged: dict = {}
    new_token = str(token_item.get("token", "") or "").strip()

    for existing_pool_name, pool_tokens in existing_tokens.items():
        merged[existing_pool_name] = list(pool_tokens) if isinstance(pool_tokens, list) else []

    pool_list = merged.setdefault(pool_name, [])
    replaced = False

    for index, existing_item in enumerate(pool_list):
        if not isinstance(existing_item, dict):
            continue
        existing_token = str(existing_item.get("token", "") or "").strip()
        if existing_token == new_token:
            updated_item = dict(existing_item)
            updated_item.update(token_item)
            pool_list[index] = updated_item
            replaced = True
            break

    if not replaced:
        pool_list.append(token_item)

    return merged


def _upload_modern(account, api_url: str, headers: dict, pool_name: str | None = None) -> Tuple[bool, str] | None:
    resp = cffi_requests.post(
        f"{api_url.rstrip('/')}/admin/api/tokens/add",
        headers=headers,
        json=_build_modern_add_payload(account, pool_name=pool_name),
        **_request_options(),
    )
    if resp.status_code in (404, 405):
        return None
    if resp.status_code not in (200, 201):
        return False, _response_message(resp, f"导入失败: HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        data = {}

    count = int((data or {}).get("count") or 0)
    skipped = int((data or {}).get("skipped") or 0)
    if count > 0:
        return True, f"导入成功：新增 {count} 个 token"
    if skipped > 0:
        return True, f"token 已存在，跳过 {skipped} 个"
    return True, "导入成功"


def _upload_legacy(account, api_url: str, headers: dict, pool_name: str | None = None, quota=None) -> Tuple[bool, str]:
    normalized_pool_name, token_item = _build_token_item(account, pool_name=pool_name, quota=quota)
    existing_tokens = _load_existing_legacy_tokens(api_url, headers)
    if existing_tokens is None:
        return False, "未找到兼容的 grok2api 管理接口"

    payload = _merge_token(existing_tokens, normalized_pool_name, token_item)
    resp = cffi_requests.post(
        f"{api_url.rstrip('/')}/v1/admin/tokens",
        headers=headers,
        json=payload,
        **_request_options(),
    )
    if resp.status_code in (200, 201):
        return True, "导入成功"
    return False, _response_message(resp, f"导入失败: HTTP {resp.status_code}")


def upload_to_grok2api(
    account,
    api_url: str | None = None,
    app_key: str | None = None,
    admin_username: str | None = None,
    pool_name: str | None = None,
    quota=None,
) -> Tuple[bool, str]:
    """上传 Grok 账号到 grok2api 管理接口。"""
    if not api_url:
        api_url = _get_config_value("grok2api_url") or DEFAULT_API_URL
    if not app_key:
        app_key = _get_config_value("grok2api_app_key") or DEFAULT_APP_KEY

    api_url = str(api_url or "").strip()
    app_key = str(app_key or "").strip()
    if not api_url:
        return False, "grok2api URL 未配置"
    if not app_key:
        return False, "grok2api App Key 未配置"

    headers = _build_headers(app_key, admin_username=admin_username)

    try:
        modern_result = _upload_modern(account, api_url, headers, pool_name=pool_name)
        if modern_result is not None:
            return modern_result
        return _upload_legacy(account, api_url, headers, pool_name=pool_name, quota=quota)
    except Exception as e:
        logger.error(f"grok2api 导入异常: {e}")
        return False, f"导入异常: {e}"
