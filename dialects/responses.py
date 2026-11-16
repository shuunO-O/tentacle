"""
Responses API 方言

覆盖范围：DeepSeek Responses API（当前 chat 角色在用）/ OpenAI Responses API。

消息转换：Chat Completions 风格 full_messages -> Responses input items
响应解析：response 对象 -> ChatCompletionMessage（复用工具执行主干）

provider quirks（同一方言内的供应商差异）：
- deepseek: 工具调用必须显式传 tool_choice="auto"，否则模型默认不调用工具（含 web_search）

图片输入（cfg.vision 门控）：file_id 直接引用优先（省请求体），无 file_id 时经
context.image_resolver 转 base64 data URL 兜底；vision=False 替换 "[图片]" 占位。
"""

import logging
import json
from typing import Callable

from agent.protocol import Context, Event, EventType, ModelConfig
from config.providers import compatibility
from dialects.base import AdapterResult, DialectAdapter, NormalizedUsage

logger = logging.getLogger(__name__)


def _output_items(items):
    def plain(item):
        return item.model_dump(mode='json', exclude_none=True) if hasattr(item, 'model_dump') else vars(item)
    return json.loads(json.dumps(items, default=plain))


class ResponsesDialect(DialectAdapter):
    """Responses API 方言适配器"""

    name = "responses"

    async def call(
        self,
        client,
        cfg: ModelConfig,
        full_messages: list[dict],
        tools,
        context: Context,
        emit: Callable,
    ) -> AdapterResult:
        system_prompt = ""
        rest = full_messages
        if full_messages and full_messages[0].get("role") == "system":
            system_prompt = full_messages[0].get("content") or ""
            rest = full_messages[1:]

        create_params = {
            "model": cfg.name,
            "input": await self._to_responses_input(rest, cfg, context),
        }
        if system_prompt:
            create_params["instructions"] = system_prompt
        # 已提供的定义一律作为本地 function，包括厂商无关的 web_search。
        response_tools = [self._to_responses_tool(t) for t in (tools or [])]
        # 仅在没有本地同名定义时兼容旧的原生搜索配置。
        # 不能放在 `if tools:` 内：旧配置可能只有原生搜索。
        if "web_search" in (cfg.tool_names or []) and not any(
            t.get("type") == "function" and t.get("name") == "web_search" for t in response_tools
        ):
            if not any(t.get("type") == "web_search" for t in response_tools):
                response_tools.append({"type": "web_search"})
        if response_tools:
            create_params["tools"] = response_tools
            if compatibility(cfg.provider).responses_auto_tool_choice:
                # provider quirk：DeepSeek Responses API 不传 tool_choice 时模型默认不调用工具
                create_params["tool_choice"] = "auto"
        if cfg.temperature is not None:
            create_params["temperature"] = cfg.temperature
        if cfg.response_format is not None:
            # response_format={"type":"json_object"} -> text={"format":{...}}
            create_params["text"] = {"format": cfg.response_format}
        if cfg.reasoning_effort:
            create_params["reasoning"] = {"effort": cfg.reasoning_effort}
        if cfg.provider == 'openai' or cfg.provider_type == 'openai':
            create_params['store'] = False
            create_params['include'] = ['reasoning.encrypted_content']

        # user_id 通过原生顶层 user 参数传递（KV cache 隔离）
        effective_user_id = (getattr(context, "user_id", None) or cfg.user_id)
        if effective_user_id:
            create_params["user"] = effective_user_id

        if cfg.stream:
            create_params["stream"] = True
            stream = await client.responses.create(**create_params)
            try:
                return await self._process_responses_stream(stream, context, emit)
            finally:
                await stream.close()

        response = await client.responses.create(**create_params)
        if getattr(response, "status", None) == "failed":
            err = getattr(response, "error", None)
            raise RuntimeError(f"Responses API 返回 failed: {getattr(err, 'message', '') or err}")
        if getattr(response, "status", None) == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            logger.warning(f"[responses] 响应不完整: {reason}")
        message, usage, web_search_calls = self._parse_response_output(response)
        return AdapterResult(message=message, usage=usage, web_search_calls=web_search_calls,
                             response_model=getattr(response, 'model', ''))

    async def _to_responses_input(self, messages: list[dict], cfg: ModelConfig, context: Context) -> list[dict]:
        """
        Chat Completions 消息列表 -> Responses input items

        - user 消息：content 字符串或数组（text 块 -> input_text，image 块 -> input_image）
        - assistant 消息：正文（若有）+ tool_calls（转 function_call items）
        - tool 消息：转 function_call_output item
        """
        items = []
        native_searches = {item.get('id') for msg in messages for item in msg.get('responses_output', [])
                           if item.get('type') == 'web_search_call'}
        for msg in messages:
            # 非消息 item（web_search_call 等）直接透传
            if msg.get("type") == "web_search_call":
                if msg.get('id') not in native_searches:
                    items.append(msg)
                continue

            role = msg.get("role")
            content = msg.get("content")

            if role == "user":
                if isinstance(content, list):
                    converted = await self._convert_content_blocks(content, cfg, context)
                    items.append({"role": "user", "content": converted})
                else:
                    items.append({"role": "user", "content": content or ""})

            elif role == "assistant":
                if msg.get('responses_output'):
                    items.extend(msg['responses_output'])
                    continue
                # 正文（若有）单独成 assistant message item
                if isinstance(content, str) and content:
                    items.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") in ("text", "input_text")
                    )
                    if text:
                        items.append({"role": "assistant", "content": text})
                # tool_calls 转为 function_call items
                for tc in msg.get("tool_calls") or []:
                    func = tc.get("function") or {}
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get("id") or "",
                        "name": func.get("name") or "",
                        "arguments": func.get("arguments") or "{}",
                    })

            elif role == "tool":
                raw_output = msg.get("content") or ""
                # 工具结果支持内容块数组（如截图：{"type":"image","file_id":...}），
                # 转成 Responses 的 function_call_output 时透传为 input_image 内容块
                if isinstance(raw_output, list):
                    output = await self._convert_content_blocks(raw_output, cfg, context)
                else:
                    output = raw_output
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": output,
                })

            elif role == "system":
                # Responses API 用 instructions 承载首条 system；
                # 历史中部的 system（少见）转为 system message item 透传
                items.append({"role": "system", "content": content or ""})

            else:
                logger.warning(f"[responses] 未知消息角色，已跳过: {role}")

        return items

    async def _convert_content_blocks(
        self, blocks: list, cfg: ModelConfig, context: Context,
    ) -> list[dict]:
        """
        内部内容块 -> Responses 输入内容块

        内部格式：
        - {"type": "text", "text": ...}          -> {"type": "input_text", ...}
        - {"type": "image", "local_id"/"file_id"} -> {"type": "input_image", ...}
        - {"type": "image_url", "image_url": {url, detail}} -> {"type": "input_image", "image_url": ...}

        图片双通道（cfg.vision 门控）：
        - 有 file_id 直接引用（FileManager 惰性刷新已保证新鲜，省请求体）
        - 无 file_id 经 context.image_resolver 转 base64 data URL（通用兜底）
        - vision=False 或解析失败 -> "[图片]" 占位文本
        """
        resolver = getattr(context, "image_resolver", None)
        out = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("text", "input_text"):
                out.append({"type": "input_text", "text": block.get("text", "")})
            elif btype in ("audio", "video"):
                label = "音频" if btype == "audio" else "视频"
                out.append({"type": "input_text", "text": f"[{label}：当前接口不支持此输入]"})
            elif btype in ("image", "input_image", "image_url"):
                if not cfg.vision:
                    out.append({"type": "input_text", "text": "[图片]"})
                    continue
                img = None
                file_resolver = getattr(context, 'image_file_resolver', None)
                if compatibility(cfg.provider).chat_file_ids and file_resolver:
                    file_id = await file_resolver(block, ('deepseek_files',))
                    if file_id:
                        img = {'type': 'input_image', 'file_id': file_id}
                        if block.get('detail'):
                            img['detail'] = block['detail']
                if img is None:
                    img = await self._image_to_input_image(block, resolver)
                if img is not None:
                    out.append(img)
                else:
                    out.append({"type": "input_text", "text": "[图片不可用]"})
                    logger.warning("[responses] 图片块无法解析为 input_image，已替换占位文本")
        return out

    @staticmethod
    async def _image_to_input_image(block: dict, resolver) -> dict | None:
        """
        内部图片块 -> Responses input_image

        优先级：file_id 直接引用 > 块内 image_url 透传 > resolver 转 base64。
        file_id 是与上传端点绑定的缓存字段（FileManager 惰性刷新保证与当前
        files.base_url 一致），无 file_id 时走 base64 兜底通道。
        """
        detail = block.get("detail")

        # image_url 块（外部 URL 或已是 data URL）直接透传
        if block.get("type") == "image_url" and isinstance(block.get("image_url"), dict):
            url = block["image_url"].get("url", "")
            if not url:
                return None
            img = {"type": "input_image", "image_url": url}
            if detail or block["image_url"].get("detail"):
                img["detail"] = detail or block["image_url"]["detail"]
            return img

        # image/input_image 块内嵌字符串 image_url 的形态
        if isinstance(block.get("image_url"), str) and block["image_url"]:
            img = {"type": "input_image", "image_url": block["image_url"]}
            if detail:
                img["detail"] = detail
            return img

        # base64 兜底：经 resolver 读本地副本（需 FileManager 已配置）
        if resolver is None:
            return None
        try:
            data_url = await resolver(block)
        except Exception as e:
            logger.warning(f"[responses] 图片 base64 解析失败: {e}")
            return None
        if not data_url:
            return None
        img = {"type": "input_image", "image_url": data_url}
        if detail:
            img["detail"] = detail
        return img

    def _to_responses_tool(self, tool: dict) -> dict:
        """
        Chat Completions 工具定义（嵌套） -> Responses 工具定义（扁平）
        """
        func = tool.get("function", {})
        name = func.get("name", "")
        return {
            "type": "function",
            "name": name,
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {"type": "object", "properties": {}}),
        }

    @staticmethod
    def _web_search_to_dict(item) -> dict:
        """
        将 web_search_call item（SDK 类型或 raw dict）归一化为 plain dict

        DeepSeek 实际返回的 action 有两种形态（与 SDK 类型定义不同，以实测为准）：
        - {"type": "search", "queries": ["...", ...]}   多个搜索词数组
        - {"type": "open_page", "url": "https://..."}   打开网页读取
        status 可能是 completed / failed / in_progress
        """
        if isinstance(item, dict):
            return {k: v for k, v in item.items() if v is not None}

        def _action_to_dict(action) -> dict:
            if isinstance(action, dict):
                return {k: v for k, v in action.items() if v is not None}
            d: dict = {"type": getattr(action, "type", "") or ""}
            # search: queries 数组（SDK 类型是单数 query，服务端实测是复数 queries）
            if hasattr(action, "queries") and getattr(action, "queries", None):
                d["queries"] = list(action.queries)
            if hasattr(action, "query") and getattr(action, "query", None):
                d["queries"] = [action.query]
            if getattr(action, "url", None):
                d["url"] = action.url
            return d

        d: dict = {
            "type": "web_search_call",
            "id": getattr(item, "id", "") or "",
            "status": getattr(item, "status", "") or "",
        }
        action = getattr(item, "action", None)
        if action:
            d["action"] = _action_to_dict(action)
        return d

    def _parse_response_output(self, response) -> tuple:
        """
        解析非流式 response 对象，转回 ChatCompletionMessage

        输出 item 映射：
        - message item 的 output_text 块      -> content
        - reasoning item 的 reasoning_text 块 -> reasoning_content
        - function_call item                  -> tool_calls
        - web_search_call item                -> web_search_calls（服务端联网搜索）

        Returns:
            tuple: (message, usage, web_search_calls)
        """
        full_content = ""
        full_reasoning = ""
        tool_calls_chunks: dict[int, dict] = {}
        web_search_calls: list[dict] = []

        for item in getattr(response, "output", None) or []:
            itype = getattr(item, "type", None)
            if itype == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "output_text" and getattr(part, "text", None):
                        full_content += part.text
            elif itype == "reasoning":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "text", None):
                        full_reasoning += part.text
            elif itype == "function_call":
                idx = len(tool_calls_chunks)
                tool_calls_chunks[idx] = {
                    "id": getattr(item, "call_id", "") or "",
                    "type": "function",
                    "function": {
                        "name": getattr(item, "name", "") or "",
                        "arguments": getattr(item, "arguments", "{}") or "{}",
                    },
                }
            elif itype == "web_search_call":
                web_search_calls.append(self._web_search_to_dict(item))

        message = self._build_message(full_content, full_reasoning, tool_calls_chunks)
        object.__setattr__(message, 'responses_output', _output_items(getattr(response, 'output', None) or []))
        usage = self._convert_usage(getattr(response, "usage", None))
        return message, usage, web_search_calls

    async def _process_responses_stream(self, stream, context: Context, emit: Callable) -> AdapterResult:
        """
        处理 Responses API 流式事件

        注意：SDK 对未知事件类型会 fallback 成其他类型（type 字段保留原值），
        因此事件匹配必须用 getattr(event, "type") 字符串比较，不能用 isinstance。

        事件映射：
        - response.output_text.delta      -> TEXT_DELTA
        - response.reasoning_text.delta   -> THINKING_DELTA
        - response.output_item.done(function_call) -> 收集工具调用
        - response.web_search_call.*      -> WEB_SEARCH 事件 + 收集 item
        - response.completed              -> usage
        - response.failed                 -> 抛错（由外层 ERROR 兜底）
        """
        full_content = ""
        full_reasoning = ""
        tool_calls_chunks: dict[int, dict] = {}
        web_search_calls: list[dict] = []
        usage = None
        output_items, completed_output = [], None
        response_model = ''

        async for event in stream:
            etype = getattr(event, "type", "")
            response_model = getattr(getattr(event, 'response', None), 'model', None) or response_model

            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", None) or ""
                full_content += delta
                await emit(Event(
                    type=EventType.TEXT_DELTA,
                    data={"content": delta},
                    context=context,
                ))

            elif etype in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                delta = getattr(event, "delta", None) or ""
                full_reasoning += delta
                await emit(Event(
                    type=EventType.THINKING_DELTA,
                    data={"content": delta},
                    context=context,
                ))

            elif etype == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is not None:
                    output_items.append(item)
                if getattr(item, "type", None) == "function_call":
                    idx = len(tool_calls_chunks)
                    tool_calls_chunks[idx] = {
                        "id": getattr(item, "call_id", "") or "",
                        "type": "function",
                        "function": {
                            "name": getattr(item, "name", "") or "",
                            "arguments": getattr(item, "arguments", "{}") or "{}",
                        },
                    }
                elif getattr(item, "type", None) == "web_search_call":
                    ws = self._web_search_to_dict(item)
                    web_search_calls.append(ws)
                    # output_item.done 携带完整 action（queries/url），派发最终状态
                    await emit(Event(
                        type=EventType.WEB_SEARCH,
                        data={
                            "query": web_search_summary(ws),
                            "status": ws.get("status", "completed"),
                            "id": ws.get("id", ""),
                        },
                        context=context,
                    ))

            elif etype.startswith("response.web_search_call."):
                # 流式搜索状态事件（in_progress / searching / completed）
                # 注意：实测这些事件只有 item_id，没有 item 对象（与 SDK 类型定义不符）
                status = etype.split(".")[-1]  # in_progress / searching / completed
                await emit(Event(
                    type=EventType.WEB_SEARCH,
                    data={
                        "query": "",
                        "status": status,
                        "id": getattr(event, "item_id", "") or "",
                    },
                    context=context,
                ))

            elif etype == "response.completed":
                usage = self._convert_usage(getattr(getattr(event, "response", None), "usage", None))
                completed_output = getattr(getattr(event, 'response', None), 'output', None)

            elif etype == "response.incomplete":
                resp = getattr(event, "response", None)
                usage = self._convert_usage(getattr(resp, "usage", None))
                reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
                logger.warning(f"[responses] 流式响应不完整: {reason}")

            elif etype == "response.failed":
                resp = getattr(event, "response", None)
                err = getattr(resp, "error", None)
                raise RuntimeError(f"Responses API 流式失败: {getattr(err, 'message', '') or err}")

        message = self._build_message(full_content, full_reasoning, tool_calls_chunks)
        raw = _output_items(completed_output if completed_output is not None else output_items)
        if full_content and not any(item.get('type') == 'message' for item in raw):
            raw.append({'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': full_content}]})
        if raw:
            object.__setattr__(message, 'responses_output', raw)
        return AdapterResult(message=message, usage=usage, web_search_calls=web_search_calls,
                             response_model=response_model)

    @staticmethod
    def _convert_usage(usage) -> NormalizedUsage | None:
        """
        Responses usage（input_tokens/output_tokens）-> NormalizedUsage

        缓存命中取 input_tokens_details.cached_tokens，
        思考 token 取 output_tokens_details.reasoning_tokens（OpenAI 形态）。
        """
        if usage is None:
            return None
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", None)
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens
        cached_tokens = 0
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", 0) or 0
        reasoning_tokens = 0
        out_details = getattr(usage, "output_tokens_details", None)
        if out_details is not None:
            reasoning_tokens = getattr(out_details, "reasoning_tokens", 0) or 0
        return NormalizedUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
        )


def web_search_summary(ws: dict) -> str:
    """
    从 web_search_call dict 提取人类可读摘要（供事件展示）

    AgentLoop 主干派发非流式 WEB_SEARCH 事件时也使用本函数。
    """
    action = ws.get("action") or {}
    queries = action.get("queries") or []
    if queries:
        # 过滤掉服务端注入的 ws_call_id 调试串
        return "; ".join(q for q in queries if not q.startswith("ws_call_id="))
    if action.get("url"):
        return action["url"]
    return ""
