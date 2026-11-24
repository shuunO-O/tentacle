"""
Agent 主循环模块

作为大模型与外部环境交互的调度中枢，职责：
1. 按方言适配器（dialects/）调用大模型 API，解析统一格式的响应
2. 解析响应，通过回调派发事件
3. 处理工具调用循环（ReAct 模式）

边界定义：
- 不组装提示词：full_messages 由适配器组装后传入
- 不定义工具：工具 Schema 由外部传入
- 不实现工具逻辑：仅负责调度执行
- 不讲方言：API 请求/响应转译全部下沉到 dialects/ 适配层

API 方言说明：
- 内部统一维护 Chat Completions 风格的 full_messages（history 格式对适配器层不变）
- 按模型配置的 api_style 分发方言适配器（chat / responses），
  响应统一转回 ChatCompletionMessage，工具执行循环复用主干
"""

import asyncio
import json
import logging
from contextvars import ContextVar
from dataclasses import replace
from typing import Callable

from dialects import get_dialect, message_to_dict
from plugins.hooks import hook_bus
from dialects.gemini import GeminiClient
from dialects.responses import web_search_summary
from agent.protocol import ModelConfig, Context, Event, EventType, LoopResult
from runtime.tool_registry import registry
from tools.send_message import has_active_channels


def resolve_model_tools(model_config: ModelConfig, excluded_tools: tuple[str, ...] = ()) -> list[dict] | None:
    """解析并过滤本轮实际暴露给模型的工具定义列表。"""
    names = registry.resolve_tool_names(
        model_config.tool_names,
        role=getattr(model_config, "role", None),
    )
    if names and "web_search" in names:
        api_style = getattr(model_config, "api_style", "chat")
        if api_style in {"responses", "interactions", "gemini_generate"} and not registry.has("web_search"):
            names = [name for name in names if name != "web_search"]
    if names and "send_message" in names and not has_active_channels():
        names = [name for name in names if name != "send_message"]
    tools = registry.get_definitions(names) if names else None
    if tools and excluded_tools:
        excluded = set(excluded_tools)
        tools = [tool for tool in tools if tool.get("function", {}).get("name") not in excluded]
    return tools
from runtime.usage import get_usage_tracker
from agent.history import has_content, missing_tool_result, prepare_history, validate_tool_calls

logger = logging.getLogger(__name__)

# 仅在真实模型工具调用期间可用；子代理管理工具用它取得委派来源和可选上下文。
current_tool_context: ContextVar[tuple[Context, list[dict]] | None] = ContextVar(
    "current_tool_context", default=None,
)


class HeartbeatYield(Exception):
    """A user arrived while only a model request was in flight."""


class RoundInterrupted(Exception):
    """显式停止；只携带已派发正文，不携带残缺工具参数或厂商状态。"""

    def __init__(self, content=""):
        super().__init__("回合已中断")
        self.content = content


def check_stop(context):
    if context.stop_event is not None and context.stop_event.is_set():
        raise RoundInterrupted


def _load_openai() -> None:
    from openai import AsyncOpenAI as _AsyncOpenAI, DefaultAsyncHttpxClient as _DefaultAsyncHttpxClient

    globals().setdefault("AsyncOpenAI", _AsyncOpenAI)
    globals().setdefault("DefaultAsyncHttpxClient", _DefaultAsyncHttpxClient)


def __getattr__(name: str):
    if name in ("AsyncOpenAI", "DefaultAsyncHttpxClient"):
        _load_openai()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class AgentLoop:
    """
    Agent 主循环

    实现单轮对话的完整推理流程：模型调用 -> 解析响应 -> 工具执行 -> 循环直到完成
    """

    def __init__(self, model_config: ModelConfig, callback: Callable, *,
                 tool_executor=None, tool_definitions=None, checkpoint=None):
        """
        初始化主循环

        Args:
            model_config: 模型配置
            callback: 统一回调函数，接收 Event 对象，由适配器层实现
        """
        self.model_config = model_config
        self.callback = callback
        self.tool_executor = tool_executor
        self.tool_definitions = tool_definitions
        self.checkpoint = checkpoint
        is_gemini = model_config.api_style in ("interactions", "gemini_generate")
        if is_gemini:
            client_class = GeminiClient
        else:
            if "AsyncOpenAI" not in globals() or (
                model_config.proxy is not None and "DefaultAsyncHttpxClient" not in globals()
            ):
                _load_openai()
            client_class = globals()["AsyncOpenAI"]
        client_options = {}
        if model_config.proxy is not None:
            if is_gemini:
                client_options["proxy"] = model_config.proxy
            else:
                client_options["http_client"] = globals()["DefaultAsyncHttpxClient"](
                    proxy=model_config.proxy or None, trust_env=False,
                )
        self.client = client_class(
            api_key=model_config.api_key, base_url=model_config.base_url,
            **({
                "auth_mode": model_config.auth,
                "project": model_config.project,
                "location": model_config.location,
                "credentials_env": model_config.credentials_env,
            } if is_gemini else {}),
            **client_options,
        )
        self.dialect = get_dialect(model_config.api_style)
        cache_config = model_config.explicit_cache or {}
        if (model_config.api_style == 'gemini_generate' and model_config.auth == 'adc'
                and model_config.name.startswith('gemini-') and cache_config.get('enabled', False)):
            from dialects.gemini_cache import GeminiContextCache
            self.client.context_cache = GeminiContextCache(self.client, cache_config)

    async def _call_model(self, full_messages, tools, context):
        check_stop(context)
        cfg = self.model_config
        heartbeat = context.source == "heartbeat"
        if heartbeat:
            changes = {"max_retries": min(cfg.max_retries, cfg.heartbeat_max_retries)}
            if cfg.heartbeat_service_tier and (
                cfg.api_style == "interactions"
                or (cfg.api_style == "gemini_generate" and cfg.auth == "adc")
            ):
                changes["service_tier"] = cfg.heartbeat_service_tier
            cfg = replace(cfg, **changes)
        event = context.interrupt_event
        if event is not None and event.is_set():
            raise HeartbeatYield
        text = []

        async def emit(event):
            if context.stop_event is not None and context.stop_event.is_set():
                raise RoundInterrupted("".join(text))
            if event.type in (EventType.TEXT_DELTA, EventType.TEXT):
                text.append(event.data.get("content", ""))
            await self.callback(event)

        call = self.dialect.call(
            client=self.client, cfg=cfg, full_messages=full_messages,
            tools=tools, context=context, emit=emit,
        )
        if not heartbeat and context.stop_event is None:
            return await call
        # 只取消模型调用，不取消承载保存流程的父回合或正在执行的工具。
        model_task = asyncio.create_task(call)
        waiter = asyncio.create_task(event.wait()) if event is not None else None
        stop_waiter = asyncio.create_task(context.stop_event.wait()) if context.stop_event is not None else None
        tasks = [task for task in (model_task, waiter, stop_waiter) if task is not None]
        try:
            done, _ = await asyncio.wait(tasks, timeout=cfg.heartbeat_timeout_seconds if heartbeat else None,
                                         return_when=asyncio.FIRST_COMPLETED)
            # 显式停止优先于同时完成的响应；心跳自动让路仍保留整批闭合语义。
            if context.stop_event is not None and context.stop_event.is_set():
                raise RoundInterrupted("".join(text))
            # A complete response wins a simultaneous arrival: its tool batch
            # must be executed and paired before handing the history over.
            if model_task in done:
                return model_task.result()
            if waiter is not None and waiter in done:
                raise HeartbeatYield
            raise TimeoutError("心跳模型请求超过等待预算，本次心跳结束")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _emit_tool_event(self, event):
        try:
            await self.callback(event)
        except Exception:
            # 工具结果已写入上下文；展示通道失败不能改变工具结果或导致工具重跑。
            logger.exception("工具事件派发失败: %s", event.type)

    async def _run_tool_batch(self, message, full_messages, context, checkpoint):
        assistant = message_to_dict(message)
        used = {call['id'] for msg in full_messages for call in msg.get('tool_calls') or []}
        validate_tool_calls(assistant.get('tool_calls'), used)
        full_messages.append(assistant)
        completed, started = set(), None
        executor = getattr(self, "tool_executor", None) or registry
        try:
            for tool_call in message.tool_calls:
                started = None
                check_stop(context)
                await self._emit_tool_event(Event(EventType.TOOL_CALL_START, {
                    "name": tool_call.function.name, "arguments": tool_call.function.arguments,
                    "tool_call_id": tool_call.id,
                }, context))
                check_stop(context)
                try:
                    args = json.loads(tool_call.function.arguments)
                except (TypeError, json.JSONDecodeError) as exc:
                    result_json = json.dumps({"error": f"参数解析失败: {exc}"}, ensure_ascii=False)
                else:
                    if not isinstance(args, dict):
                        result_json = json.dumps({"error": "工具参数必须是对象"}, ensure_ascii=False)
                    else:
                        token = current_tool_context.set((context, full_messages))
                        try:
                            started = tool_call.id
                            if tool_call.function.name in context.excluded_tools:
                                result_json = json.dumps(
                                    {"error": f"当前回合禁止调用工具: {tool_call.function.name}"},
                                    ensure_ascii=False,
                                )
                            else:
                                hooks = getattr(self, "hook_bus", None) or hook_bus
                                allowed, eff_args, block_reason = await hooks.emit_before_tool_call(
                                    tool_name=tool_call.function.name,
                                    arguments=args,
                                    tool_call_id=tool_call.id,
                                    context=context,
                                )
                                if not allowed:
                                    result_json = json.dumps(
                                        {"error": block_reason or "工具调用被插件阻止"},
                                        ensure_ascii=False,
                                    )
                                else:
                                    result_json = await executor.execute(tool_call.function.name, eff_args)
                                    result_json = await hooks.emit_after_tool_result(
                                        tool_name=tool_call.function.name,
                                        arguments=eff_args,
                                        result=result_json,
                                        tool_call_id=tool_call.id,
                                        context=context,
                                    )
                                    if not isinstance(result_json, (str, list)):
                                        result_json = json.dumps(result_json, ensure_ascii=False)
                        finally:
                            current_tool_context.reset(token)
                # 先保存真实结果，再通知外部通道。
                full_messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result_json})
                completed.add(tool_call.id)
                await self._emit_tool_event(Event(EventType.TOOL_RESULT, {
                    "name": tool_call.function.name, "result": result_json, "tool_call_id": tool_call.id,
                }, context))
        except BaseException:
            for call in message.tool_calls:
                if call.id not in completed:
                    full_messages.append(missing_tool_result(
                        call.id, status="unknown" if call.id == started else "not_executed",
                    ))
            raise
        finally:
            # 包括异常收尾后的完整批次；保存失败就停止，不继续请求模型。
            if checkpoint is not None:
                await checkpoint(full_messages)

    async def run(self, full_messages: list[dict], context: Context) -> LoopResult:
        """
        执行主循环

        核心流程：
        1. 经方言适配器调用大模型 API
        2. 解析响应，派发 USAGE/THINKING/TEXT 事件
        3. 判断终止条件：无 tool_calls 则派发 FINAL_ANSWER 并退出
        4. 若有 tool_calls，执行工具并将结果追加到 full_messages，继续循环

        Args:
            full_messages: 完整的对话上下文（由适配器组装，内部规范格式）
            context: 上下文信息，会原样传递给回调函数

        Returns:
            LoopResult: 包含完整对话、最终回答和累计 Token 用量
        """
        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cache_write_tokens": 0,
            # 最后一次请求的 prompt_tokens，即当前实际上下文窗口占用
            "context_prompt_tokens": 0,
        }
        final_answer = ""
        stop_reason = "complete"
        error = None
        pending_content = ""
        tool_executor = getattr(self, "tool_executor", None)
        checkpoint = getattr(self, "checkpoint", None)

        try:
            # 获取工具定义，供大模型选择调用
            tools = getattr(self, "tool_definitions", None)
            if tools is None:
                tools = resolve_model_tools(self.model_config, context.excluded_tools)
            elif context.excluded_tools:
                excluded = set(context.excluded_tools)
                tools = [tool for tool in tools if tool.get("function", {}).get("name") not in excluded]

            for iteration in range(self.model_config.max_iterations):
                pending_content = ""
                full_messages[:] = prepare_history(full_messages)
                result = await self._call_model(full_messages, tools, context)
                message = result.message
                if self.model_config.stream and message is not None:
                    pending_content = message.content or ""
                usage = result.usage
                web_search_calls = result.web_search_calls

                # 累加 Token 用量
                if usage:
                    total_usage["prompt_tokens"] += usage.prompt_tokens
                    total_usage["completion_tokens"] += usage.completion_tokens
                    total_usage["total_tokens"] += usage.total_tokens
                    # 每次请求的 prompt_tokens 都是当前完整上下文大小，取最后一次（最大）
                    total_usage["context_prompt_tokens"] = usage.prompt_tokens

                    for key in ("cached_tokens", "reasoning_tokens", "cache_write_tokens"):
                        total_usage[key] += getattr(usage, key)

                    # 派发单次 Token 用量事件（零值字段不携带，前端 data.get 兼容）
                    usage_data = {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    }
                    if usage.cached_tokens:
                        usage_data["cached_tokens"] = usage.cached_tokens
                    if usage.reasoning_tokens:
                        usage_data["reasoning_tokens"] = usage.reasoning_tokens
                    if usage.cache_write_tokens:
                        usage_data["cache_write_tokens"] = usage.cache_write_tokens
                    await self.callback(Event(
                        type=EventType.USAGE,
                        data=usage_data,
                        context=context,
                    ))

                    # 持久化用量（统计页长期数据，失败不影响主循环）
                    response_model = getattr(result, 'response_model', '')
                    response_model = response_model.strip() if isinstance(response_model, str) else ''
                    provider_id = self.model_config.provider_id or self.model_config.provider
                    await get_usage_tracker().record_usage(
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                        cached_tokens=usage.cached_tokens,
                        reasoning_tokens=usage.reasoning_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        platform=getattr(context, "platform", "unknown") or "unknown",
                        model=response_model,
                        provider_id=provider_id,
                        model_id=f'{provider_id}/{response_model}' if response_model else '',
                        role=self.model_config.role,
                    )

                if message is None or (not message.tool_calls and not has_content(message.content)):
                    raise ValueError("模型返回空响应，本轮未完成；未将空 assistant 消息写入历史")

                check_stop(context)
                # 非流式模式下派发思考内容事件（流式模式已在流处理中派发）
                if not self.model_config.stream and getattr(message, "reasoning_content", None):
                    await self.callback(Event(
                        type=EventType.THINKING,
                        data={"content": message.reasoning_content},
                        context=context,
                    ))

                # 联网搜索事件（非流式模式由这里派发；流式已在方言流处理中派发）
                # 同时将 web_search_call items 追加到历史（多轮上下文需要保留搜索记录）
                for ws in web_search_calls:
                    if not self.model_config.stream:
                        await self.callback(Event(
                            type=EventType.WEB_SEARCH,
                            data={
                                "query": web_search_summary(ws),
                                "status": ws.get("status", ""),
                                "id": ws.get("id", ""),
                            },
                            context=context,
                        ))
                    full_messages.append(ws)

                # 非流式模式下派发文本输出事件（流式模式已在流处理中派发）
                if not self.model_config.stream and message.content:
                    check_stop(context)
                    await self.callback(Event(
                        type=EventType.TEXT,
                        data={"content": message.content},
                        context=context,
                    ))
                    pending_content = message.content
                    final_answer = message.content

                # 流式模式下，final_answer 在流处理中已收集
                if self.model_config.stream and message.content:
                    final_answer = message.content

                check_stop(context)

                # 终止条件：模型未请求工具调用，本轮结束
                if not message.tool_calls:
                    await self.callback(Event(
                        type=EventType.FINAL_ANSWER,
                        data={"content": final_answer},
                        context=context,
                    ))
                    # 将最终回复的 assistant 消息追加到上下文，确保历史记录完整
                    full_messages.append(message_to_dict(message))
                    if checkpoint is not None:
                        await checkpoint(full_messages)
                    break

                pending_content = ""  # 工具批次负责保存完整 assistant 及配对结果。
                await self._run_tool_batch(message, full_messages, context, checkpoint)
                check_stop(context)
                if context.interrupt_event is not None and context.interrupt_event.is_set():
                    raise HeartbeatYield
                if getattr(tool_executor, "question", None):
                    stop_reason = "waiting_input"
                    final_answer = tool_executor.question
                    break
            else:
                # 达到最大迭代次数，强制结束
                final_answer = "已达到最大迭代次数，停止执行。"
                stop_reason = "iteration_limit"
                await self.callback(Event(
                    type=EventType.FINAL_ANSWER,
                    data={"content": final_answer},
                    context=context,
                ))

        except RoundInterrupted as exc:
            stop_reason = "cancelled"
            final_answer = exc.content or pending_content
            if final_answer.strip():
                full_messages.append({"role": "assistant", "content": final_answer, "interrupted": True})
            await self.callback(Event(type=EventType.FINAL_ANSWER, data={"content": final_answer}, context=context))
        except HeartbeatYield:
            stop_reason = "yielded"
            final_answer = ""
            # Close any partial stream without putting its unfinished response
            # or function arguments in canonical history.
            await self.callback(Event(type=EventType.FINAL_ANSWER, data={"content": ""}, context=context))
            await self.callback(Event(type=EventType.NOTICE, data={
                "content": "心跳已让路给你的消息，刚才的输出可能尚未完成。",
            }, context=context))
        except Exception as e:
            # 外层统一兜底，捕获所有未预期错误
            logger.error(f"主循环异常: {e}", exc_info=True)
            stop_reason = "error"
            error = str(e)
            await self.callback(Event(
                type=EventType.ERROR,
                data={"error": str(e)},
                context=context,
            ))

        return LoopResult(
            full_messages=full_messages,
            final_answer=final_answer,
            total_usage=total_usage,
            stop_reason=stop_reason,
            error=error,
        )
