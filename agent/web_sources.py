"""网页工具后端；工具名稳定，厂商协议集中在这里。"""

import asyncio
import json
import logging
import os
from urllib.parse import urlsplit

import httpx

from config.manager import ConfigManager

logger = logging.getLogger(__name__)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(value, name, limit=5000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} 必须是非空字符串，最多 {limit} 字符")
    return value.strip()


def _integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} 必须是 {lower}–{upper} 的整数")
    return value


def _strings(value, name, upper, limit=5000):
    if not isinstance(value, list) or not 1 <= len(value) <= upper:
        raise ValueError(f"{name} 必须是含 1–{upper} 项的字符串数组")
    return [_text(item, name, limit) for item in value]


def _config():
    manager = ConfigManager()
    if manager._raw_config is None:
        manager._load_config()
    return (manager._raw_config or {}).get("web_tools", {}) or {}


def _bounded_result(data, limit, *, full_content=False):
    """正文与摘录择一，再裁内容和结果；保留有效 JSON、来源和必要状态。"""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("invalid response")
    result = {"results": []}
    for key in ("warnings", "errors"):
        if data.get(key):
            result[key] = data[key]
    if data.get("truncated") is True:
        result["truncated"] = True
    for item in data["results"]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("invalid result")
        entry = {key: item[key] for key in ("url", "title", "publish_date", "excerpts", "full_content")
                 if key in item}
        if "excerpts" in entry and (not isinstance(entry["excerpts"], list)
                                    or any(not isinstance(s, str) for s in entry["excerpts"])):
            raise ValueError("invalid excerpts")
        if entry.get("full_content") is not None and not isinstance(entry["full_content"], str):
            raise ValueError("invalid content")
        if full_content and entry.get("full_content"):
            entry.pop("excerpts", None)
        else:
            # search / full_content=false 只返回摘录；正文缺失时也保留摘录兜底。
            entry.pop("full_content", None)
        entry = {key: value for key, value in entry.items() if value not in (None, "", [])}
        result["results"].append(entry)
    while len(_encode(result)) > limit:
        result["truncated"] = True
        candidates = []
        for item in result["results"]:
            if item.get("full_content"):
                candidates.append((len(item["full_content"]), item, "full_content"))
            candidates.extend((len(s), item["excerpts"], i)
                              for i, s in enumerate(item.get("excerpts", [])) if s)
        if candidates:
            length, container, key = max(candidates, key=lambda c: c[0])
            container[key] = container[key][:length // 2]
        elif result["results"]:
            result["results"].pop()
            result["omitted_results"] = result.get("omitted_results", 0) + 1
        else:
            for key in ("warnings", "errors"):
                if key in result:
                    del result[key]
                    break
            else:
                break
    return _encode(result)


def _validate_urls(args):
    urls = _strings(args.get("urls"), "urls", 5, 8192)
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
            raise ValueError("urls 只接受无用户名/密码的 HTTP(S) 公开网页链接")
        parsed.port
    return urls


def _validate_search_args(args):
    mode = args.get("mode", "fast")
    if mode not in ("fast", "turbo", "basic", "advanced"):
        raise ValueError("mode 必须是 fast/turbo/basic/advanced")
    queries = _strings(args.get("queries"), "queries", 5, 500)
    max_results = _integer(args.get("max_results", 5), "max_results", 1, 10)
    objective = _text(args["objective"], "objective") if "objective" in args else None
    return queries, objective, max_results, mode


def _validate_fetch_args(args):
    urls = _validate_urls(args)
    full = args.get("full_content", True)
    if type(full) is not bool:
        raise ValueError("full_content 必须是布尔值")
    objective = _text(args["objective"], "objective") if "objective" in args else None
    return urls, objective, full


async def _post_json(client, url, headers, body, label):
    """固定端点 POST JSON，限制 2 MiB 响应体积，不回显上游错误正文或密钥。"""
    async with client.stream("POST", url, headers=headers, json=body) as response:
        if response.status_code != 200:
            status = response.status_code
            messages = {
                401: "API Key 无效",
                402: "API 额度或余额不足",
                403: "API 权限不足或目标页面被拒绝",
                429: "限流或额度不足",
            }
            return None, {"error": messages.get(status, f"{label} API 请求失败"),
                          "status": status, "retryable": status in (408, 429) or status >= 500}
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 2 * 1024 * 1024:
                return None, {"error": f"{label} 响应超过 2 MiB 上限"}
    return json.loads(chunks), None


class _BaseWebProvider:
    label = ""
    default_api_key_env = ""

    async def request(self, operation, spec, api_key, timeout, output_limit):
        # 固定端点、不跟随重定向、不自动重试，不回显错误正文或密钥。
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                    return await self._execute(client, operation, spec, api_key, output_limit)
        except (TimeoutError, httpx.TimeoutException):
            return _encode({"error": f"{self.label} 请求超时；未自动重试", "retryable": True})
        except httpx.HTTPError:
            return _encode({"error": f"{self.label} 网络请求失败；未自动重试", "retryable": True})
        except (ValueError, UnicodeError, TypeError):
            return _encode({"error": f"{self.label} 返回了无效响应"})


class ParallelProvider(_BaseWebProvider):
    label = "Parallel"
    default_api_key_env = "PARALLEL_API_KEY"

    def prepare(self, operation, args):
        if operation == "search":
            queries, objective, max_results, mode = _validate_search_args(args)
            body = {"search_queries": queries, "mode": mode, "max_chars_total": 25000,
                    "advanced_settings": {"max_results": max_results,
                                          "excerpt_settings": {"max_chars_per_result": 5000}}}
            if objective is not None:
                body["objective"] = objective
            return body
        urls, objective, full = _validate_fetch_args(args)
        body = {"urls": urls, "max_chars_total": 25000, "advanced_settings": {
            "excerpt_settings": {"max_chars_per_result": 5000},
            "full_content": {"max_chars_per_result": 25000} if full else False}}
        if objective is not None:
            body["objective"] = objective
        return body

    async def _execute(self, client, operation, body, api_key, output_limit):
        data, err = await _post_json(
            client, f"https://api.parallel.ai/v1/{operation}",
            {"x-api-key": api_key}, body, self.label,
        )
        if err is not None:
            return _encode(err)
        if isinstance(data, dict):
            metadata = {key: data[key] for key in ("search_id", "extract_id", "usage") if key in data}
            if metadata:
                logger.info("Parallel %s metadata: %s", operation, _encode(metadata))
        full_content = operation == "extract" and body["advanced_settings"]["full_content"] is not False
        return _bounded_result(data, output_limit, full_content=full_content)


def _collect_search_results(responses, max_results, map_item):
    results, seen_urls = [], set()
    for data, err in responses:
        if err is not None:
            return None, err
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("invalid response")
        for item in data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ValueError("invalid result")
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            results.append(map_item(item))
            if len(results) >= max_results:
                return results, None
    return results, None


class TavilyProvider(_BaseWebProvider):
    label = "Tavily"
    default_api_key_env = "TAVILY_API_KEY"

    def prepare(self, operation, args):
        if operation == "search":
            queries, _objective, max_results, mode = _validate_search_args(args)
            return {
                "queries": queries,
                "max_results": max_results,
                "search_depth": "advanced" if mode == "advanced" else "basic",
            }
        urls, _objective, full = _validate_fetch_args(args)
        return {"urls": urls, "full_content": full}

    @staticmethod
    def _map_search_item(item):
        entry = {"url": item["url"]}
        if isinstance(item.get("title"), str):
            entry["title"] = item["title"]
        if isinstance(item.get("published_date"), str):
            entry["publish_date"] = item["published_date"]
        if isinstance(item.get("content"), str) and item["content"]:
            entry["excerpts"] = [item["content"]]
        return entry

    async def _execute(self, client, operation, spec, api_key, output_limit):
        headers = {"Authorization": f"Bearer {api_key}"}
        if operation == "search":
            bodies = [{"query": q, "max_results": spec["max_results"],
                       "search_depth": spec["search_depth"],
                       "include_answer": False, "include_raw_content": False}
                      for q in spec["queries"]]
            responses = await asyncio.gather(*(
                _post_json(client, "https://api.tavily.com/search", headers, body, self.label)
                for body in bodies
            ))
            results, err = _collect_search_results(responses, spec["max_results"], self._map_search_item)
            if err is not None:
                return _encode(err)
            return _bounded_result({"results": results}, output_limit, full_content=False)

        body = {"urls": spec["urls"], "format": "markdown", "extract_depth": "basic"}
        data, err = await _post_json(client, "https://api.tavily.com/extract", headers, body, self.label)
        if err is not None:
            return _encode(err)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("invalid response")
        full = spec["full_content"]
        results = []
        for item in data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ValueError("invalid result")
            entry = {"url": item["url"]}
            raw_content = item.get("raw_content")
            if isinstance(raw_content, str) and raw_content:
                entry["full_content" if full else "excerpts"] = raw_content if full else [raw_content]
            results.append(entry)
        normalized = {"results": results}
        failed = data.get("failed_results")
        if isinstance(failed, list) and failed:
            errors = [{"url": f.get("url", ""), "error": f.get("error", "extract failed")}
                      for f in failed if isinstance(f, dict)]
            if errors:
                normalized["errors"] = errors
        return _bounded_result(normalized, output_limit, full_content=full)


class DeepSeekProvider(_BaseWebProvider):
    label = "DeepSeek"
    default_api_key_env = "DEEPSEEK_API_KEY"

    def prepare(self, operation, args):
        if operation == "search":
            queries, objective, max_results, _mode = _validate_search_args(args)
            return {
                "queries": [q[:128] for q in queries],
                "ori_question": objective[:128] if objective is not None else None,
                "max_results": max_results,
            }
        urls, _objective, full = _validate_fetch_args(args)
        return {"urls": urls, "full_content": full}

    @staticmethod
    def _map_search_item(item):
        entry = {"url": item["url"]}
        if isinstance(item.get("title"), str):
            entry["title"] = item["title"]
        if isinstance(item.get("date"), str):
            entry["publish_date"] = item["date"]
        excerpt = item.get("summary") or item.get("snippet")
        if isinstance(excerpt, str) and excerpt:
            entry["excerpts"] = [excerpt]
        return entry

    async def _execute(self, client, operation, spec, api_key, output_limit):
        headers = {"Authorization": f"Bearer {api_key}"}
        if operation == "search":
            bodies = []
            for q in spec["queries"]:
                body = {"query": q, "count": spec["max_results"], "need_content": False}
                if spec["ori_question"]:
                    body["ori_question"] = spec["ori_question"]
                bodies.append(body)
            responses = await asyncio.gather(*(
                _post_json(client, "https://api.deepseek.com/v1/tools/search", headers, body, self.label)
                for body in bodies
            ))
            results, err = _collect_search_results(responses, spec["max_results"], self._map_search_item)
            if err is not None:
                return _encode(err)
            return _bounded_result({"results": results}, output_limit, full_content=False)

        responses = await asyncio.gather(*(
            _post_json(client, "https://api.deepseek.com/v1/tools/fetch", headers, {"url": u}, self.label)
            for u in spec["urls"]
        ))
        full = spec["full_content"]
        results, errors, first_err = [], [], None
        for u, (data, err) in zip(spec["urls"], responses):
            if err is not None:
                if first_err is None:
                    first_err = err
                errors.append({"url": u, "error": err["error"]})
                continue
            if not isinstance(data, dict):
                raise ValueError("invalid response")
            entry = {"url": u}
            if isinstance(data.get("title"), str):
                entry["title"] = data["title"]
            content = data.get("content")
            if isinstance(content, str) and content:
                entry["full_content" if full else "excerpts"] = content if full else [content]
            results.append(entry)
        if not results and first_err is not None:
            return _encode(first_err)
        normalized = {"results": results}
        if errors:
            normalized["errors"] = errors
        return _bounded_result(normalized, output_limit, full_content=full)


_PROVIDERS = {
    "parallel": ParallelProvider,
    "tavily": TavilyProvider,
    "deepseek": DeepSeekProvider,
}


async def execute_web_tool(operation, args):
    try:
        config = _config()
    except Exception:
        return _encode({"error": "读取 web_tools 配置失败"})
    try:
        provider_class = _PROVIDERS.get(config.get("provider", "parallel"))
        if provider_class is None:
            supported = "/".join(_PROVIDERS)
            raise ValueError(f"web_tools.provider 不受支持，目前可用 {supported}")
        provider = provider_class()
        spec = provider.prepare(operation, args)
        limit = _integer(config.get("max_output_chars", 40000), "max_output_chars", 2000, 100000)
        timeout = config.get("timeout_seconds", 60)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("timeout_seconds 必须大于 0 且不超过 120")
        raw_env = config.get("api_key_env")
        if raw_env is None or (isinstance(raw_env, str) and not raw_env.strip()):
            env_name = provider.default_api_key_env
        else:
            env_name = _text(raw_env, "api_key_env", 100)
    except ValueError as error:
        return _encode({"error": str(error)})
    except (TypeError, AttributeError):
        return _encode({"error": "网页工具参数或 web_tools 配置无效"})
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        return _encode({"error": f"网页工具 API Key 未配置，请设置 {env_name}"})
    return await provider.request(operation, spec, api_key, timeout, limit)
