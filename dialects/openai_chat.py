"""
OpenAI Chat Completions 方言

覆盖范围：DeepSeek / OpenAI 官方 / 各 OpenAI 兼容网关。

provider quirks（同一方言内的供应商差异）：
- deepseek: user_id 经 extra_body 传递（实现模块级 KV cache 隔离）
- 其他 provider: user_id 走顶层 user 参数（OpenAI 标准）

图片输入（cfg.vision 门控）：Chat Completions 无 file_id 图片通道，
vision=True 时图片块经 context.image_resolver 转 base64 data URL
（各家通用最小公分母）；vision=False 扁平化为文本并替换 "[图片]" 占位。
"""

import logging
from typing import Callable

from agent.protocol import Context, Event, EventType, ModelConfig
from config.providers import compatibility
from dialects.base import AdapterResult, DialectAdapter, NormalizedUsage

logger = logging.getLogger(__name__)


class OpenAIChatDialect(DialectAdapter):
    """Chat Completions 方言适配器"""

    name = "chat"

    async def call(
        self,
        client,
        cfg: ModelConfig,
        full_messages: list[dict],
        tools,
        context: Context,
        emit: Callable,
    ) -> AdapterResult:
        create_params = {
            "model": cfg.name,
            "messages": await self._prepare_messages(cfg, full_messages, context),
            "stream": cfg.stream,
        }
        if tools:
            create_params["tools"] = tools
        if cfg.temperature is not None:
            create_params["temperature"] = cfg.temperature
        if cfg.response_format is not None:
            create_params["response_format"] = cfg.response_format
        if cfg.stream:
            create_params["stream_options"] = {"include_usage": True}

        # Context 中的 user_id 优先级高于 ModelConfig 中的默认值
        effective_user_id = (getattr(context, "user_id", None) or cfg.user_id)
        if effective_user_id:
            if compatibility(cfg.provider).user_id_in_extra_body:
                # DeepSeek 要求经 extra_body 传 user_id，实现模块级 KV cache 隔离
                create_params["extra_body"] = {"user_id": effective_user_id}
            else:
                create_params["user"] = effective_user_id

        if cfg.stream:
            stream = await client.chat.completions.create(**create_params)
            try:
                return await self._process_stream(stream, context, emit)
            finally:
                await stream.close()

        response = await client.chat.completions.create(**create_params)
        return AdapterResult(
            message=response.choices[0].message,
            usage=self._convert_usage(response.usage),
            response_model=getattr(response, 'model', ''),
        )

    async def _prepare_messages(
        self, cfg: ModelConfig, full_messages: list[dict], context: Context,
    ) -> list[dict]:
        """
        Chat Completions 消息准备（cfg.vision 能力驱动）

        vision=False：content 数组扁平化为字符串，图片块替换 "[图片]" 占位，
        避免 API 400（原 _sanitize_for_chat 行为，纯文本模型兜底）。
        vision=True：产出多模态 content 数组，图片块转 base64 data URL
        （Chat Completions 各家通用形态；file_id 通道仅 Responses API 有，
        此处一律走 base64）。外部 URL 的 image_url 块直接透传。
        """
        resolver = getattr(context, "image_resolver", None)
        out = []
        tool_images = []
        for msg in full_messages:
            # Standard Chat tool messages accept text only. Attach tool images
            # as a following user message after all parallel tool replies.
            # DeepSeek's native file tool results retain their existing shape.
            if msg.get("role") and msg["role"] != "tool" and tool_images:
                out.append({"role": "user", "content": tool_images})
                tool_images = []
            # 厂商元数据/纯思考记录仍保留在 canonical history；chat 请求不能发送空 assistant。
            # 只在请求副本中省略，不改原历史，也不把思考内容冒充正文。
            if (msg.get("role") == "assistant" and not msg.get("tool_calls")
                    and msg.get("content") in (None, "", [])):
                continue
            allowed = {'role', 'content', 'tool_calls', 'tool_call_id', 'name'}
            if compatibility(cfg.provider).compatibility == 'deepseek':
                allowed.add('reasoning_content')
            msg = {k: v for k, v in msg.items() if k in allowed}
            # 服务端工具 item（web_search_call 等）无 role 字段，仅 Responses input 可识别，
            # chat 协议下无对应物，跳过（assistant 最终回答文本仍在历史中）
            if not msg.get("role"):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                out.append(msg)
                continue

            parts: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in ("text", "input_text"):
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif btype in ("audio", "video"):
                    label = "音频" if btype == "audio" else "视频"
                    parts.append({"type": "text", "text": f"[{label}：当前接口不支持此输入]"})
                elif btype in ("image", "input_image", "image_url"):
                    converted = None
                    if cfg.vision:
                        converted = await self._image_to_chat_block(
                            block,
                            resolver,
                            file_id_channel=compatibility(cfg.provider).chat_file_ids and cfg.chat_file_id_channel,
                            file_resolver=getattr(context, "image_file_resolver", None),
                        )
                    if converted is not None:
                        parts.append(converted)
                    else:
                        parts.append({"type": "text", "text": "[图片不可用]" if cfg.vision else "[图片]"})
                        if cfg.vision:
                            logger.warning("[chat 分支] 图片块无法解析为 base64，已替换占位文本")
                        else:
                            logger.warning("[chat 分支] 消息含图片块，已替换为占位文本")

            if cfg.vision and any(p.get("type") in ("image_url", "file") for p in parts):
                if msg["role"] == "tool" and compatibility(cfg.provider).compatibility != "deepseek":
                    label = f"[工具 {msg.get('tool_call_id', '')} 返回的图片]"
                    tool_images.append({"type": "text", "text": label})
                    tool_images.extend(p for p in parts if p.get("type") in ("image_url", "file"))
                    text = "\n".join(p["text"] for p in parts if p.get("type") == "text")
                    out.append({**msg, "content": f"{text}\n{label}".strip()})
                    continue
                # 多模态消息：合并相邻 text 块，保持消息紧凑
                merged: list[dict] = []
                for p in parts:
                    if (p.get("type") == "text" and merged
                            and merged[-1].get("type") == "text"):
                        merged[-1] = {"type": "text", "text": merged[-1]["text"] + p["text"]}
                    else:
                        merged.append(p)
                msg = {**msg, "content": merged}
            else:
                # 纯文本消息（含 vision=True 但图片全部解析失败的兜底）保持字符串形态
                text = "".join(p["text"] for p in parts if p.get("type") == "text") or "[图片]"
                msg = {**msg, "content": text}
            out.append(msg)
        if tool_images:
            out.append({"role": "user", "content": tool_images})
        return out

    @staticmethod
    async def _image_to_chat_block(block: dict, resolver, file_id_channel: bool = False,
                                   file_resolver=None) -> dict | None:
        """
        内部图片块 -> Chat Completions 图片块

        三级通道（与 Responses 方言的双通道设计对称）：
        1. file_id 直引（DeepSeek Files API 支持 chat completions 经
           {"type": "file", "file_id": ...} 引用，避免 base64 内联撑爆请求体）
        2. base64 data URL（resolver 读本地副本）
        3. 外链 image_url 透传

        Returns:
            图片块；无法解析（无 resolver / 读取失败）返回 None
        """
        # 仅使用解析器确认属于 DeepSeek Files API 的引用，绝不盲传 gs:// 等他厂 ID。
        if file_id_channel and file_resolver:
            file_id = await file_resolver(block, ("deepseek_files",))
            if file_id:
                return {"type": "file", "file_id": file_id}

        # image_url 块（外部 URL 或已是 data URL）直接透传
        if block.get("type") == "image_url" and isinstance(block.get("image_url"), dict):
            url = block["image_url"].get("url", "")
            if not url:
                return None
            img = {"type": "image_url", "image_url": {"url": url}}
            detail = block["image_url"].get("detail")
            if detail:
                img["image_url"]["detail"] = detail
            return img

        # image/input_image 块内嵌字符串 image_url 的形态
        if isinstance(block.get("image_url"), str) and block["image_url"]:
            return {"type": "image_url", "image_url": {"url": block["image_url"]}}

        # base64 兜底通道：经 resolver 读本地副本（无 file_id / 通道关闭 / 非 deepseek 时走）
        if resolver is None:
            return None
        try:
            data_url = await resolver(block)
        except Exception as e:
            logger.warning(f"[chat 分支] 图片 base64 解析失败: {e}")
            return None
        if not data_url:
            return None
        return {"type": "image_url", "image_url": {"url": data_url}}

    async def _process_stream(self, stream, context: Context, emit: Callable) -> AdapterResult:
        """
        处理流式响应

        实时派发增量事件（THINKING_DELTA、TEXT_DELTA），同时收集完整内容用于后续处理。

        Returns:
            AdapterResult: (完整的 message 对象, usage 对象)
        """
        full_content = ""
        full_reasoning = ""
        saw_reasoning = False
        response_model = ''
        tool_calls_chunks: dict[int, dict] = {}
        usage = None

        async for chunk in stream:
            response_model = getattr(chunk, 'model', None) or response_model
            if not chunk.choices:
                # 最后一个 chunk 可能没有 choices，但有 usage
                if chunk.usage:
                    usage = chunk.usage
                continue

            delta = chunk.choices[0].delta

            # 处理 reasoning_content delta
            if getattr(delta, 'reasoning_content', None) is not None:
                saw_reasoning = True
            if getattr(delta, "reasoning_content", None):
                full_reasoning += delta.reasoning_content
                await emit(Event(
                    type=EventType.THINKING_DELTA,
                    data={"content": delta.reasoning_content},
                    context=context,
                ))

            # 处理 content delta
            if delta.content:
                full_content += delta.content
                await emit(Event(
                    type=EventType.TEXT_DELTA,
                    data={"content": delta.content},
                    context=context,
                ))

            # 收集 tool_calls delta
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_chunks:
                        tool_calls_chunks[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tool_calls_chunks[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls_chunks[idx]["function"]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_chunks[idx]["function"]["arguments"] += tc.function.arguments

            # 获取 usage（最后一个 chunk）
            if chunk.usage:
                usage = chunk.usage

        # 构建完整的 message 对象
        message = self._build_message(full_content, full_reasoning, tool_calls_chunks)
        if saw_reasoning:
            object.__setattr__(message, 'reasoning_content', full_reasoning)
        return AdapterResult(message=message, usage=self._convert_usage(usage), response_model=response_model)

    @staticmethod
    def _convert_usage(usage) -> NormalizedUsage | None:
        """
        Chat Completions usage（SDK 对象或 SimpleNamespace）-> NormalizedUsage

        兼容形态：
        - DeepSeek: prompt_cache_hit_tokens（缓存命中）
        - OpenAI: prompt_tokens_details.cached_tokens +
                  completion_tokens_details.reasoning_tokens（思考 token）
        - Anthropic 预留: cache_creation_input_tokens -> cache_write_tokens
        """
        if usage is None:
            return None
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", None)
        if total is None:
            total = prompt + completion

        cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        if not cached:
            p_details = getattr(usage, "prompt_tokens_details", None)
            if p_details is not None:
                cached = getattr(p_details, "cached_tokens", 0) or 0

        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        reasoning = 0
        c_details = getattr(usage, "completion_tokens_details", None)
        if c_details is not None:
            reasoning = getattr(c_details, "reasoning_tokens", 0) or 0

        return NormalizedUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=cached,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
        )
