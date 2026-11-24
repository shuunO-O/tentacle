"""按已保存的连接读取远端模型目录；不生成内容、不改写配置。"""

import asyncio
import json
import os

import httpx

from config.providers import ConfigurationError, PROVIDERS, automatic_identifier

MAX_BYTES = 2 * 1024 * 1024
MAX_MODELS = 1000
MAX_PAGES = 10


def _text(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def _adc_headers(provider):
    """仅由显式读取操作在线刷新服务账号令牌，代理策略与应用连接一致。"""
    from config.google_auth import load_credentials, make_refresh_request

    project = provider.get("project") or os.getenv(provider.get("project_env", "GOOGLE_CLOUD_PROJECT"), "")
    if not project:
        raise ConfigurationError("请先设置此连接的 Google Cloud 项目")
    credentials = load_credentials(provider.get("credentials_env", ""), project)
    with make_refresh_request(provider.get("proxy"), timeout=10.0) as request:
        credentials.refresh(request)
    headers = {"x-goog-user-project": project}
    credentials.apply(headers)
    return headers


async def _page(client, url, params):
    async with client.stream("GET", url, params=params) as response:
        status = response.status_code
        if status in (401, 403):
            raise ConfigurationError("供应商拒绝读取模型列表，请检查此连接的凭据和目录访问权限")
        if status in (404, 405, 501):
            raise ConfigurationError("此连接未提供受支持的模型列表接口；仍可手动添加模型")
        if status == 429:
            raise ConfigurationError("供应商模型目录请求过于频繁，请稍后再试")
        if status != 200:
            raise ConfigurationError(f"读取模型列表失败（HTTP {status}），请检查供应商服务")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ConfigurationError("供应商模型目录响应过大，请使用手动添加")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise ConfigurationError("供应商未返回有效的模型目录 JSON") from None
    if not isinstance(result, dict):
        raise ConfigurationError("供应商模型目录格式不受支持")
    return result


async def fetch_catalog(provider, *, transport=None):
    """向已保存的目录地址发 GET；不跟随重定向或服务端返回的 URL。"""
    kind = provider["type"]
    google = kind in {"google_developer", "google_vertex"}
    headers = {}
    if kind != "google_vertex":
        key = os.getenv(provider.get("api_key_env", ""), "")
        if not key:
            raise ConfigurationError("此连接的密钥环境变量尚未配置，请先在主机设置")
        headers["x-goog-api-key" if google else "Authorization"] = key if google else "Bearer " + key
    url = provider.get("models_url") or provider["base_url"].rstrip("/") + (
        "/publishers/google/models" if kind == "google_vertex" else "/models")
    params = httpx.QueryParams(httpx.URL(url).query)
    if google and "pageSize" not in params:
        params = params.set("pageSize", 100)
    result, seen_tokens, seen_names, truncated = [], set(), set(), False
    try:
        async with asyncio.timeout(30):
            if kind == "google_vertex":
                headers = await asyncio.to_thread(_adc_headers, provider)
            proxy = provider.get("proxy")
            async with httpx.AsyncClient(headers=headers, proxy=proxy or None, trust_env=proxy is None,
                                         timeout=15, follow_redirects=False, transport=transport) as client:
                for page in range(MAX_PAGES):
                    payload = await _page(client, url, params)
                    items = payload.get("publisherModels" if kind == "google_vertex" else "models" if google else "data")
                    if not isinstance(items, list):
                        raise ConfigurationError("供应商模型目录格式不受支持")
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name" if google else "id")
                        if not _text(name):
                            continue
                        prefix = "publishers/google/models/" if kind == "google_vertex" else "models/"
                        if google and name.startswith(prefix):
                            name = name[len(prefix):]
                        if not name or name in seen_names:
                            continue
                        if len(result) >= MAX_MODELS:
                            truncated = True
                            break
                        seen_names.add(name)
                        label = item.get("displayName", item.get("display_name"))
                        entry = {"name": name, "label": label if _text(label) else name}
                        context = item.get("inputTokenLimit")
                        if type(context) is int and 0 < context <= 100_000_000:
                            entry["context_window"] = context
                        methods = item.get("supportedGenerationMethods")
                        if isinstance(methods, list):
                            entry["methods"] = [method for method in methods[:30] if _text(method, 128)]
                        result.append(entry)
                    token = payload.get("nextPageToken") if google else (
                        items[-1].get("id") if payload.get("has_more") is True and items and isinstance(items[-1], dict) else None)
                    if not token or truncated:
                        break
                    if not _text(token, 4096) or token in seen_tokens or page == MAX_PAGES - 1:
                        truncated = True
                        break
                    seen_tokens.add(token)
                    params = params.set("pageToken" if google else "after", token)
    except ConfigurationError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ConfigurationError("读取模型列表超时，请稍后重试") from None
    except Exception:
        # 不返回异常正文：供应商、代理及 ADC 错误可能包含认证信息。
        raise ConfigurationError("无法读取模型列表，请检查连接、代理或 ADC 凭据") from None
    return {"models": sorted(result, key=lambda item: item["name"]), "truncated": truncated}


def import_candidates(raw, provider_id, result):
    """稳定且合法的本地 ID；已配置的远端模型不重复导入。"""
    configured = {m["name"]: key for key, m in raw["models"].items() if m["provider"] == provider_id}
    used = set(raw["models"])
    for model in result["models"]:
        candidate = configured.get(model["name"]) or automatic_identifier(
            "models", {"provider": provider_id, "name": model["name"]}, used)
        used.add(candidate)
        model.update(import_id=candidate, configured=model["name"] in configured)
    kind = raw["providers"][provider_id]["type"]
    return {**result, "provider": provider_id, "api_style": PROVIDERS[kind].api_style,
            "notice": ("这是 Google 发布模型目录，实际可用性取决于项目、区域和协议。" if kind == "google_vertex"
                       else "目录可能包含不同用途的模型，请核对协议和能力后导入。") +
                      " 未提供的图片能力和上下文窗口需在导入后核对。"}
