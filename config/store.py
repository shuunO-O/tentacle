"""配置字段定义与文件事务。不初始化 Agent、不加载 .env、不请求外部服务。"""

import json
import math
import os
import re
import stat
import tempfile
import threading
import textwrap
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml
from attachments.downloads import download_settings
from runtime.heartbeat import DEFAULT_HANDOVER_MESSAGE, heartbeat_settings
from runtime.timezone import DEFAULT_TIMEZONE, timezone_for
from config.providers import (
    CONCEPT_ROLES,
    CONNECTION_KEYS,
    MODEL_KEYS,
    PARAMETERS,
    PROVIDERS,
    ConfigurationError,
    _identifier,
    automatic_identifier,
    catalog,
    references,
    resolved_models,
    validate_structure,
)
from config.files import ConfigFiles, MODEL_SECTIONS, parse_config
from config.environment import dotenv_update, environment_name


class ConfigurationConflict(ConfigurationError):
    pass


@dataclass(frozen=True)
class Field:
    path: str
    label: str
    kind: str
    default: object = None
    effect: str = "restart"
    group: str = "运行设置"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple = ()
    nullable: bool = False
    help: str = ""
    multiline: bool = False


def _append_plugin_fields(add, *, plugins_dir: Path | None = None, bundled_dir: Path | None = None):
    """动态合并已发现内置及本地插件 manifest.yaml 的 config_schema 字段。"""
    try:
        from plugins.manifest import discover_plugin_dirs, parse_manifest

        for _pid, entry, _is_bundled in discover_plugin_dirs(plugins_dir=plugins_dir, bundled_dir=bundled_dir):
            try:
                manifest = parse_manifest(entry)
            except Exception:
                continue
            group = f"插件 · {manifest.name}"
            base = f"plugins.items.{manifest.id}"
            add(f"{base}.enabled", "启用插件", "boolean", False, group=group, effect="restart")
            add(f"{base}.trusted", "已审核信任", "boolean", False, group=group, effect="restart")
            add(f"{base}.disabled_tools", "禁用的工具", "tools", [], group=group, effect="restart")
            for spec in manifest.config_schema.values():
                add(
                    f"{base}.config.{spec.key}",
                    spec.label,
                    spec.type,
                    spec.default,
                    group=group,
                    effect="restart",
                    choices=spec.choices or (),
                    minimum=spec.minimum,
                    maximum=spec.maximum,
                    nullable=spec.nullable,
                    multiline=spec.multiline,
                    help=spec.help,
                )
    except Exception:
        return


def _model_template_fields():
    """连接、模型、角色共用字段定义模板。"""
    def f(path, label, kind, default=None, **kw):
        return Field(path, label, kind, default, **kw)

    return {
        "name": f("", "模型名称", "string", "gpt-4o-mini"),
        "base_url": f("", "API 地址", "url", "https://api.openai.com/v1"),
        "api_style": f("", "API 协议", "string", "chat", choices=("chat", "responses", "interactions", "gemini_generate", "systemone")),
        "auth": f("", "认证方式", "string", "api_key", choices=("api_key", "adc")),
        "api_key_env": f("", "密钥环境变量名", "env", "", help="填写已有变量名，或在下方录入密钥并自动生成变量。"),
        "project": f("", "Google Cloud 项目", "string", None, nullable=True),
        "project_env": f("", "项目环境变量名", "env", "GOOGLE_CLOUD_PROJECT"),
        "location": f("", "Google Cloud 区域", "string", "global"),
        "credentials_env": f("", "凭据路径环境变量名", "env", "", help="变量值为主机上的 Google 凭据文件路径；留空使用 ADC。"),
        "gcs_base_url": f("", "此连接的 GCS 地址", "string", "", help="gs://BUCKET/PREFIX；独立凭据留空时图片使用本地回退，不使用全局 bucket。"),
        "proxy": f("", "HTTP 代理", "proxy", None, nullable=True, help="空字符串直连；使用默认值则继承环境代理。"),
        "service_tier": f("", "消费层级", "string", None, choices=("standard", "priority", "flex"), nullable=True),
        "temperature": f("", "温度", "number", None, minimum=0, maximum=2, nullable=True),
        "reasoning_effort": f("", "思考强度", "string", "high", help="填写当前模型支持的值。"),
        "vision": f("", "允许图片输入", "boolean", False),
        "audio": f("", "允许音频输入（Gemini）", "boolean", False),
        "video": f("", "允许视频输入（Gemini）", "boolean", False),
        "stream": f("", "流式输出", "boolean", True),
        "max_iterations": f("", "每轮最大迭代次数", "integer", 10, minimum=1, maximum=1000),
        "context_window": f("", "上下文窗口（tokens）", "integer", 1048576, minimum=1),
        "max_retries": f("", "请求最大重试次数", "integer", 3, minimum=0, maximum=10),
        "retry_base_seconds": f("", "重试基础等待（秒）", "number", 1.0, minimum=0.1, maximum=300),
        "interactions_stateful": f("", "Interactions 远端续接", "boolean", False, help="开启后使用远端 ID 续接；本地完整历史仍保留。"),
        "tools.enabled": f("", "可用工具", "tools", [], nullable=True, help="只控制该模型可见的工具，不限制 scheduler 直接调用工具。"),
    }


def fields_for(raw):
    validate_structure(raw)
    template = _model_template_fields()
    result = {}

    def add(path, label, kind, default=None, **kw):
        result[path] = Field(path, label, kind, default, **kw)

    def copy(key, path, group, effect="restart", **kw):
        result[path] = replace(template[key], path=path, group=group, effect=effect, **kw)

    add("app.timezone", "程序时区", "string", DEFAULT_TIMEZONE, group="程序",
        help="IANA 时区名称，例如 Asia/Shanghai、UTC。独立于服务器，重启后生效；用于日历、调度、统计与日志。")
    add("app.user_name", "个人称呼", "string", "", group="程序", nullable=True,
        help="提示词与上下文标签里的称呼；留空使用 user。")
    add("app.agent_name", "Agent 名字", "string", "", group="程序", nullable=True,
        help="统一用于界面、提示词与上下文标签；留空时界面显示 Tentacle，提示词使用 assistant。")
    resolved = resolved_models(raw)
    add("memory.embedding.dimensions", "向量维度", "integer", 1024, group="记忆检索", minimum=1,
        help="使用向量嵌入角色支持的维度；更改后可在本页重算记忆向量并重启。")
    add("memory.search.content_mode", "事件原文返回方式", "string", "excerpt", group="记忆检索",
        choices=("excerpt", "full"), help="excerpt返回命中片段摘录；full返回完整原文，长记忆会增加上下文用量。")
    add("memory.search.event_top_k", "事件详细结果数", "integer", 8, group="记忆检索", minimum=1, maximum=20)
    add("memory.search.include_index", "附带候选索引", "boolean", True, group="记忆检索",
        help="详细结果之外附带候选ID和短摘要，可按需read原文。")
    add("memory.search.index_top_k", "候选索引数", "integer", 24, group="记忆检索", minimum=0, maximum=64)
    concept_roles = tuple(key for key in raw["roles"]
                          if resolved.get(key, {}).get("api_style") == "systemone"
                          or (key in CONCEPT_ROLES and key not in resolved))
    add("memory.concept_alignment.enabled", "新记忆自动去重（JEV）", "boolean", False, group="记忆去重",
        help="写入新记忆时判断概念是否加入已有等价组；关闭后不调用 JEV。历史扫描仍由记忆页手动启动。")
    for section, label in (("concept_alignment", "新记忆去重角色"), ("concept_dedup", "历史去重角色")):
        add(f"memory.{section}.model_role", label, "string", section, group="记忆去重", choices=concept_roles,
            help="使用 System One 评分模型；供应商、地址、密钥变量和模型名在模型与角色中管理。")
        for key, title, kind, default, low, high in (
            ("top_k", "召回候选数", "integer", 5, 1, 20),
            ("same_probability", "允许归并的概率门槛", "number", 0.95, 0.500001, 1),
            ("different_probability", "确认独立的概率门槛", "number", 0.90, 0.500001, 1),
            ("timeout_seconds", "单次评分超时（秒）", "number", 15, 0.001, 300),
            ("total_timeout_seconds", "整体处理超时（秒）", "number", 60, 0.001, 300),
            ("max_concepts", "单次最大概念数", "integer", 32, 1, 100),
        ):
            if section == "concept_dedup" and key in {"top_k", "max_concepts"}:
                continue  # 历史扫描的规模由记忆页启动参数控制。
            add(f"memory.{section}.{key}", ("新记忆 · " if section == "concept_alignment" else "历史去重 · ") + title,
                kind, default, group="记忆去重", minimum=low, maximum=high)
    add("heartbeat.enabled", "启用定时心跳", "boolean", True, effect="round", group="心跳",
        help="关闭固定计时心跳；已明确预约的 scheduler 唤醒仍独立执行。")
    add("heartbeat.interval_minutes", "心跳间隔（分钟）", "number", 30,
        effect="round", group="心跳", minimum=0.1, maximum=10080,
        help="应用后从当前时刻重新计时，不立即触发心跳。")
    add("heartbeat.message", "普通心跳消息", "string", "心跳", effect="round", group="心跳")
    add("heartbeat.include_in_progress_tasks", "普通心跳附带进行中任务", "boolean", False,
        effect="round", group="心跳", help="关闭后普通心跳不附带任务；24 小时目标回顾与交接心跳仍附带完整目标。")
    add("heartbeat.session_switch.enabled", "启用心跳日 session 切换", "boolean", True,
        effect="round", group="心跳")
    for key, label, default in (
        ("window_start", "session 切换窗口开始", "01:00"),
        ("window_end", "session 切换窗口结束", "07:00"),
    ):
        add(f"heartbeat.session_switch.{key}", label, "string", default, effect="round", group="心跳",
            help="程序时区 HH:MM，支持跨午夜；包含开始时刻，不包含结束时刻。错过窗口不补切。")
    add("heartbeat.session_switch.message", "切换前近况交接消息", "string", DEFAULT_HANDOVER_MESSAGE,
        effect="round", group="心跳", multiline=True,
        help="长文本在页面中自动换行显示，仍按单行保存。程序仍要求本轮正常完成且近况写入成功后才能切换。")
    add("heartbeat.service_tier", "心跳消费层级", "string", "flex", effect="round", group="心跳",
        choices=("standard", "priority", "flex"), nullable=True)
    add("heartbeat.model_timeout_seconds", "心跳模型等待上限（秒）", "number", 300,
        effect="round", group="心跳", minimum=1, maximum=1800)
    add("heartbeat.max_retries", "心跳最大重试次数", "integer", 1,
        effect="round", group="心跳", minimum=0, maximum=10)
    add("web_tools.provider", "网页工具服务商", "string", "parallel", group="公开网页工具",
        choices=("parallel", "tavily", "deepseek"))
    for path, label, kind, default, group, low, high in (
        ("webui.host", "监听地址", "string", "0.0.0.0", "Web 服务", None, None),
        ("webui.port", "监听端口", "integer", 8420, "Web 服务", 1, 65535),
        ("web_tools.timeout_seconds", "请求超时（秒）", "number", 60, "公开网页工具", 1, 300),
        ("web_tools.max_output_chars", "最大输出字符数", "integer", 40000, "公开网页工具", 100, 200000),
        ("web_tools.api_key_env", "网页工具密钥环境变量", "env", "", "公开网页工具", None, None),
        ("media_inputs.auto_bilibili", "自动获取 B 站视频", "boolean", True, "媒体输入", None, None),
        ("scheduler.catch_up_grace_hours", "单次任务补跑宽限（小时）", "number", 24, "定时任务", 0, 8760),
        ("scheduler.interval_catch_up_grace_hours", "间隔任务补跑宽限（小时）", "number", 6, "定时任务", 0, 8760),
        ("subagents.enabled", "启用外部 coding 子 agent", "boolean", False, "功能开关", None, None),
    ):
        add(path, label, kind, default, group=group, minimum=low, maximum=high)
    add("logging.level", "日志级别", "string", "INFO", group="日志",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    add("media_inputs.download.backend", "视频下载后端", "string", "local", group="媒体输入",
        choices=("local", "http_jobs"))
    for key, label, kind, default in (
        ("base_url", "远程任务服务地址", "proxy", ""),
        ("profile", "远程下载配置", "string", "bilibili-480p-v1"),
        ("proxy", "下载服务 HTTP 代理（空为直连）", "proxy", ""),
        ("api_key_env", "下载服务令牌环境变量（可空）", "env", ""),
    ):
        add("media_inputs.download." + key, label, kind, default, group="媒体输入")
    add("media_inputs.download.timeout_seconds", "远程等待与领取预算（秒）", "number", 360,
        group="媒体输入", minimum=1, maximum=360)
    _append_plugin_fields(add)

    for key, item in raw["providers"].items():
        spec = PROVIDERS[item["type"]]
        group = "连接 · " + key
        add(f"providers.{key}.type", "接入类型", "string", item["type"], group=group, choices=tuple(PROVIDERS))
        add(f"providers.{key}.label", "备注", "string", None, nullable=True, group=group, effect="immediate",
            help="可留空；未填写时显示标识。")
        add(f"providers.{key}.models_url", "模型列表接口地址", "catalog_url", None, nullable=True,
            group=group, effect="immediate",
            help="留空按接入类型使用默认地址；可填写完整 HTTP(S) 地址及版本等查询参数，认证沿用此连接。")
        add(f"providers.{key}.balance_mode", "余额查询方式", "string", "auto",
            choices=("auto", "none", "deepseek"), group=group, effect="immediate",
            help="auto：按接入类型；none：不查询；deepseek：按 DeepSeek 格式解析，需 API Key 认证。")
        add(f"providers.{key}.balance_url", "余额查询接口地址", "catalog_url", None, nullable=True,
            group=group, effect="immediate",
            help="填写完整 HTTP(S) 地址，认证和代理沿用连接；留空使用 API 地址加 /user/balance。自定义连接需选择 deepseek 格式。")
        for name in sorted(CONNECTION_KEYS):
            default = {"base_url": spec.base_url, "auth": spec.auth}.get(name, template[name].default)
            copy(name, f"providers.{key}.{name}", group, default=default)
    for key, item in raw["models"].items():
        spec = PROVIDERS[raw["providers"][item["provider"]]["type"]]
        group = "模型 · " + key
        add(f"models.{key}.label", "备注", "string", None, nullable=True, group=group, effect="immediate",
            help="可留空；未填写时显示标识。")
        add(f"models.{key}.provider", "供应商连接", "string", item["provider"], group=group,
            choices=tuple(raw["providers"]))
        systemone = item.get("api_style", spec.api_style) == "systemone"
        for name in sorted({"name", "api_style"} if systemone else MODEL_KEYS):
            copy(name, f"models.{key}.{name}", group,
                 **({"default": spec.api_style, "choices": spec.protocols} if name == "api_style" else {}))
        for name in sorted(set() if systemone else PARAMETERS - {"response_format"}):
            copy(name, f"models.{key}.defaults.{name}", group)
    for key, item in raw["roles"].items():
        group = "角色 · " + key
        effect = "round" if key == "chat" else "restart"
        add(f"roles.{key}.label", "备注", "string", None, nullable=True, group=group, effect="immediate",
            help="可留空；未填写时显示标识。")
        model_id = item.get("model")
        systemone = key in concept_roles
        model_choices = tuple(model_id for model_id, model in raw["models"].items()
                              if (model.get("api_style", PROVIDERS[raw["providers"][model["provider"]]["type"]].api_style)
                                  == "systemone") == systemone)
        add(f"roles.{key}.model", "使用模型", "string", None, nullable=True, group=group,
            choices=model_choices, effect=effect, help="先添加供应商连接和模型，再为角色选择模型。")
        if systemone:
            continue
        for name in ("max_iterations", "tools.enabled", "interactions_stateful"):
            copy(name, f"roles.{key}.{name}", group, effect=effect)
        for name in sorted(PARAMETERS - {"response_format"}):
            path = f"roles.{key}.overrides.{name}"
            inherited = raw["models"].get(model_id, {}).get("defaults", {}).get(name, template[name].default)
            copy(name, path, group, default=inherited, effect=effect, help="角色覆盖值；未设置时继承模型默认参数。")
    add("usage.balance_provider", "余额查询连接", "string", None, nullable=True, group="用量与余额",
        choices=tuple(key for key, item in raw["providers"].items() if PROVIDERS[item["type"]].balance))
    return result


def value_at(raw, path, default=None):
    value = raw
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return deepcopy(default)
        value = value[part]
    return deepcopy(value)


def set_value(raw, path, value):
    parts = path.split(".")
    target = raw
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        if not isinstance(target[part], dict):
            raise ConfigurationError(f"{path} 的父配置必须为对象")
        target = target[part]
    target[parts[-1]] = deepcopy(value)


def remove_value(raw, path):
    target = raw
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.get(part, {})
    target.pop(parts[-1], None)


def inherit_change(path, value):
    return path.startswith("roles.") and ".overrides." in path and value == {"$inherit": True}


def protected_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        return True


def public_value(field, value):
    if ((field.kind in {"url", "proxy"} and protected_url(value))
            or (field.kind == "catalog_url" and protected_catalog_url(value))):
        return "[包含认证信息，需在主机管理]"
    return value


def protected_catalog_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        secret_names = {"key", "apikey", "token", "accesstoken", "authorization", "password",
                        "secret", "clientsecret", "sig", "signature", "credential"}
        return bool(parsed.username or parsed.password or parsed.fragment or any(
            re.sub(r"[-_]", "", key.lower()) in secret_names for key, _ in parse_qsl(parsed.query)))
    except ValueError:
        return True


def validate_value(field, value):
    error = None
    if value is None and field.nullable:
        return
    if field.kind in {"string", "env", "url", "proxy", "catalog_url"}:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            error = "必须是单行文本（最多 4096 字符）"
        elif field.kind == "env" and value and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
            error = "必须为环境变量名，不能填写密钥"
        elif field.kind == "catalog_url":
            try:
                url = urlsplit(value)
                if (url.scheme not in {"http", "https"} or not url.hostname or protected_catalog_url(value)
                        or (url.port is not None and not 0 < url.port <= 65535)):
                    error = "必须为完整 HTTP(S) 地址；认证沿用连接，不能在地址中填写密钥或片段"
            except ValueError:
                error = "地址格式无效"
        elif field.kind in {"url", "proxy"} and (field.kind == "url" or value):
            try:
                url = urlsplit(value)
                if (url.scheme not in {"http", "https"} or not url.hostname or protected_url(value)
                        or (url.port is not None and not 0 < url.port <= 65535)):
                    error = "必须为 HTTP(S) 地址，不能包含认证信息、查询参数或片段"
            except ValueError:
                error = "地址格式无效"
        elif not value and field.kind == "string" and not field.nullable:
            error = "不能为空"
    elif field.kind == "boolean":
        if type(value) is not bool:
            error = "必须为布尔值"
    elif field.kind in {"integer", "number"}:
        if (type(value) not in ({int} if field.kind == "integer" else {int, float})
                or not math.isfinite(value)):
            error = "必须为整数" if field.kind == "integer" else "必须为有限数值"
        elif field.minimum is not None and value < field.minimum:
            error = f"不能小于 {field.minimum}"
        elif field.maximum is not None and value > field.maximum:
            error = f"不能大于 {field.maximum}"
    elif field.kind == "tools":
        if (not isinstance(value, list) or any(not isinstance(n, str) or not n for n in value)
                or len(value) > 200 or len(set(value)) != len(value)):
            error = "必须为不重复的工具名列表"
    elif field.kind == "string_list":
        if (not isinstance(value, list) or len(value) > 200
                or any(not isinstance(n, str) or len(n) > 4096 or any(ord(c) < 32 for c in n) for n in value)):
            error = "必须为单行字符串数组（最多 200 项）"
    if not error and field.choices and value not in field.choices:
        error = "必须选择支持的选项"
    if error:
        raise ConfigurationError(f"{field.path}：{error}")


def patch_text(text, path, value):
    """只替换目标 YAML 节点，保留无关内容、注释及键顺序。"""
    if yaml.compose(text) is None:
        nested = value
        for key in reversed(path.split(".")):
            nested = {key: nested}
        return text + ("" if not text or text.endswith("\n") else "\n") + yaml.safe_dump(nested, allow_unicode=True, sort_keys=False)
    node = yaml.compose(text)
    parts = path.split(".")
    for index, part in enumerate(parts):
        if not isinstance(node, yaml.MappingNode):
            raise ConfigurationError(f"{path} 的父配置必须为对象")
        found = next((v for k, v in node.value if k.value == part), None)
        if found is None:
            nested = value
            for key in reversed(parts[index + 1:]):
                nested = {key: nested}
            if node.flow_style or not node.value:
                existing = yaml.safe_load(text[node.start_mark.index:node.end_mark.index]) or {}
                existing[part] = nested
                return text[:node.start_mark.index] + json.dumps(existing, ensure_ascii=False) + text[node.end_mark.index:]
            # 插在最后一个值所在行后面，避免把下一节前的注释吞进当前节。
            last = node.value[-1][1]
            end = last.end_mark.index
            if isinstance(last, yaml.ScalarNode) or last.flow_style:
                newline = text.find("\n", end)
                end = len(text) if newline < 0 else newline + 1
            elif end == len(text) and not text.endswith("\n"):
                end = len(text)
            else:
                end = text.rfind("\n", 0, end) + 1
            indent = node.start_mark.column
            entry = " " * indent + json.dumps(part, ensure_ascii=False) + ": " + json.dumps(nested, ensure_ascii=False) + "\n"
            prefix = "" if end == 0 or text[end - 1] == "\n" else "\n"
            return text[:end] + prefix + entry + text[end:]
        node = found
    old = text[node.start_mark.index:node.end_mark.index]
    suffix = ""
    if not isinstance(node, yaml.ScalarNode) or node.style in {"|", ">"}:
        suffix = re.search(r"\s*$", old).group()
    # PyYAML 生成的无额外缩进块序列（key:\n- item）换成 flow 值时必须增加缩进。
    padding = "  " if isinstance(node, yaml.SequenceNode) and not node.flow_style else ""
    return text[:node.start_mark.index] + padding + json.dumps(value, ensure_ascii=False, allow_nan=False) + suffix + text[node.end_mark.index:]


def remove_text(text, path):
    parts = path.split(".")
    parent = yaml.compose(text)
    for part in parts[:-1]:
        parent = next(v for k, v in parent.value if k.value == part)
    key, node = next((k, v) for k, v in parent.value if k.value == parts[-1])
    if parent.flow_style:
        value = yaml.safe_load(text[parent.start_mark.index:parent.end_mark.index])
        del value[parts[-1]]
        return patch_text(text, ".".join(parts[:-1]), value)
    start = text.rfind("\n", 0, key.start_mark.index) + 1
    end = node.end_mark.index
    if isinstance(node, yaml.ScalarNode) or node.flow_style:
        newline = text.find("\n", end)
        end = len(text) if newline < 0 else newline + 1
    elif end != len(text) or text.endswith("\n"):
        end = text.rfind("\n", 0, end) + 1
    # 最后一个条目删除后保留有效的空映射。
    if len(parent.value) == 1:
        return patch_text(text, ".".join(parts[:-1]), {})
    return text[:start] + text[end:]


def replace_section(text, key, value):
    """迁移整节使用可读的块状 YAML；保留下一节之前的独立注释。"""
    root = yaml.compose(text)
    if root is None or root.flow_style:
        return patch_text(text, key, value)
    pair = next(((k, v) for k, v in root.value if k.value == key), None)
    rendered = key + ":\n" + textwrap.indent(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), "  ")
    if pair is None:
        return text + ("" if text.endswith("\n") else "\n") + rendered
    key_node, node = pair
    last = node
    while not isinstance(last, yaml.ScalarNode) and not last.flow_style and last.value:
        last = last.value[-1][1] if isinstance(last, yaml.MappingNode) else last.value[-1]
    end = text.find("\n", last.end_mark.index)
    end = len(text) if end < 0 else end + 1
    return text[:key_node.start_mark.index] + rendered + text[end:]


def atomic_write(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ConfigStore:
    """Web 与主机 CLI 共享的乐观并发事务；只允许修改声明过的字段。"""

    def __init__(self, path=None, *, validator=None, backup_dir=None):
        self.files = path if isinstance(path, ConfigFiles) else ConfigFiles(path)
        self.path = self.files.path
        self.backup_dir = self.files.backup_dir if backup_dir is None else Path(backup_dir)
        self.validator = validator
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self):
        # 跨 Web/CLI 进程锁；崩溃时内核自动释放，不留下永久锁文件状态。
        with self._lock:
            with self.path.with_name(".tentacle-config.lock").open("a+b") as lock:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    if not lock.read(1):
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        return self.files.read()[:3]

    def _model_text(self, text, raw):
        """首次编辑继承的模型时固定完整接入配置，避免写出不完整的模型目录。"""
        if not (MODEL_SECTIONS & parse_config(text).keys()):
            for key in sorted(MODEL_SECTIONS & raw.keys()):
                text = patch_text(text, key, raw[key])
        return text

    def validate(self, raw, changed):
        validate_structure(raw)
        try:
            download_settings((raw.get('media_inputs') or {}).get('download'))
            heartbeat_settings(raw.get("heartbeat"))
            app = raw.get("app", {})
            if not isinstance(app, dict):
                raise ValueError("app 必须是配置对象")
            timezone_for(app.get("timezone", DEFAULT_TIMEZONE))
            from memory.concept_aligner import AlignmentConfig
            for section in CONCEPT_ROLES:
                AlignmentConfig.from_app_config(raw, section)
        except ValueError as error:
            raise ConfigurationError(str(error)) from None
        fields = fields_for(raw)
        template = _model_template_fields()
        # 所有已声明受管字段都检查，包括暂未被角色引用的模型/连接。
        for path, field in fields.items():
            value = value_at(raw, path, "__missing__")
            if value != "__missing__":
                validate_value(field, value)
        for _role, model in resolved_models(raw).items():
            for key, value in model.items():
                source = template.get(key)
                if source:
                    validate_value(source, value)
        if self.validator:
            self.validator(raw, changed)

    def _prepare(self, changes, revision, *, credential_only=False):
        current, text, raw = self.read()
        if current != revision:
            raise ConfigurationConflict("配置已被其他操作修改，请刷新后重新预览")
        if not isinstance(changes, dict) or not 0 < len(changes) <= 300:
            raise ConfigurationError("请提交 1 到 300 个配置字段")
        if any(not isinstance(path, str) for path in changes):
            raise ConfigurationError("配置字段路径必须为文本")
        fields = fields_for(raw)
        diff = []
        if any(path.split(".")[0] in MODEL_SECTIONS for path in changes):
            text = self._model_text(text, raw)
        for path, value in changes.items():
            field = fields.get(path)
            if field is None:
                raise ConfigurationError("包含不支持修改的配置字段")
            before = value_at(raw, path, field.default)
            if public_value(field, before) != before:
                raise ConfigurationError(f"{path} 包含认证信息，请在主机管理")
            if inherit_change(path, value):
                if value_at(raw, path, "__missing__") == "__missing__":
                    continue
                remove_value(raw, path)
                text = remove_text(text, path)
                diff.append({"path": path, "label": field.label, "before": before,
                             "after": "继承模型默认值：" + str(field.default), "effect": field.effect})
                continue
            checking = (replace(field, choices=("chat", "responses", "interactions", "gemini_generate", "systemone"))
                        if path.startswith("models.") and path.endswith(".api_style") else field)
            validate_value(checking, value)
            if before == value and type(before) is type(value):
                continue
            set_value(raw, path, value)
            text = patch_text(text, path, value)
            diff.append({"path": path, "label": field.label, "before": before,
                         "after": value, "effect": field.effect})
        # 确保保留注释的文本修改与预期对象完全一致。
        if self.files.effective(text) != raw:
            raise ConfigurationError("配置文本无法可靠更新，请在主机整理该配置段")
        self.validate(raw, [] if credential_only else list(changes))
        return current, text, raw, diff

    def preview(self, changes, revision):
        with self._lock:
            current, _, candidate, diff = self._prepare(changes, revision)
            refs = references(candidate)
            for change in diff:
                parts = change["path"].split(".")
                if parts[0] in {"providers", "models"}:
                    change["impact"] = refs[parts[0]].get(parts[1], [])
            return {"revision": current, "changes": diff}

    def save(self, changes, revision):
        with self._transaction():
            current, text, _, diff = self._prepare(changes, revision)
            return self._write(current, text, diff)

    def save_environment_value(self, path, value, revision):
        with self._transaction():
            current, _, raw = self.read()
            if current != revision:
                raise ConfigurationConflict("配置已被修改，请刷新后再保存凭据")
            field = fields_for(raw).get(path)
            if field is None or field.kind != "env":
                raise ConfigurationError("该配置项不支持录入环境变量")
            name = environment_name(path)
            dotenv_path = self.path.with_name(".env")
            content = dotenv_update(dotenv_path, name, value)
            current, text, _, diff = self._prepare({path: name}, revision, credential_only=True)
            # 先写入变量，配置引用只有在变量已持久化后才提交。
            atomic_write(dotenv_path, content)
            self._write(current, text, diff)
            return name

    def _write(self, current, text, diff):
        if not diff:
            return {"revision": current, "changes": [], "backup": None}
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.backup_dir.is_symlink():
            raise ConfigurationError("配置备份目录不能是符号链接")
        backup = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex + ".yaml"
        original = self.path.read_bytes() if self.path.exists() else b""
        if self.read()[0] != current:
            raise ConfigurationConflict("配置已变更，请刷新")
        atomic_write(self.backup_dir / backup, original)
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else 0o600
        atomic_write(self.path, text.encode("utf-8"), mode)
        return {"revision": self.read()[0], "changes": diff, "backup": backup}

    def upsert_plugin_entry(
        self,
        kind: str,
        entry_id: str,
        value: dict,
        *,
        revision: str | None = None,
    ):
        """安全写入或更新 plugins.items.<id> / plugins.mcp.<id>（带锁、备份与注释保留）。"""
        if kind not in {"items", "mcp"}:
            raise ConfigurationError("插件配置类别必须为 items 或 mcp")
        if not isinstance(entry_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", entry_id):
            raise ConfigurationError(f"插件或服务标识无效: {entry_id!r}")
        if not isinstance(value, dict):
            raise ConfigurationError("配置条目必须为对象")
        path = f"plugins.{kind}.{entry_id}"
        with self._transaction():
            current, text, raw = self.read()
            if revision is not None and current != revision:
                raise ConfigurationConflict("配置已被其他操作修改，请刷新后重试")
            before = value_at(raw, path, None)
            after = deepcopy(value)
            if before == after:
                return {"revision": current, "changes": [], "backup": None}
            set_value(raw, path, after)
            text = patch_text(text, path, after)
            if self.files.effective(text) != raw:
                raise ConfigurationError("插件配置文本无法可靠更新，请检查 config.yaml 格式")
            validate_structure(raw)
            return self._write(current, text, [{
                "path": path,
                "label": f"更新 {path}",
                "before": before,
                "after": after,
                "effect": "restart",
            }])

    def delete_plugin_entry(
        self,
        kind: str,
        entry_id: str,
        *,
        revision: str | None = None,
    ):
        """安全删除 plugins.items.<id> / plugins.mcp.<id>。"""
        if kind not in {"items", "mcp"}:
            raise ConfigurationError("插件配置类别必须为 items 或 mcp")
        path = f"plugins.{kind}.{entry_id}"
        with self._transaction():
            current, text, raw = self.read()
            if revision is not None and current != revision:
                raise ConfigurationConflict("配置已被其他操作修改，请刷新后重试")
            before = value_at(raw, path, "__missing__")
            if before == "__missing__":
                return {"revision": current, "changes": [], "backup": None}
            remove_value(raw, path)
            text = remove_text(text, path)
            if self.files.effective(text) != raw:
                raise ConfigurationError("插件配置文本无法可靠删除，请检查 config.yaml 格式")
            validate_structure(raw)
            return self._write(current, text, [{
                "path": path,
                "label": f"移除 {path}",
                "before": before,
                "after": None,
                "effect": "restart",
            }])

    def structure(self, operations, revision, *, apply=False):
        with self._transaction() if apply else self._lock:
            current, text, raw = self.read()
            if current != revision:
                raise ConfigurationConflict("配置已被其他操作修改，请刷新后重新预览")
            if not isinstance(operations, list) or not 0 < len(operations) <= 50:
                raise ConfigurationError("结构变更必须包含 1 到 50 个操作")
            diff, seen = [], set()
            text = self._model_text(text, raw)
            before_refs = references(raw)
            for operation in operations:
                if not isinstance(operation, dict) or set(operation) - {"op", "kind", "id", "value"}:
                    raise ConfigurationError("结构操作格式无效")
                kind, key, op = (operation.get(name) for name in ("kind", "id", "op"))
                if isinstance(kind, str) and kind in {"providers", "models", "roles"} and op == "create" and key in (None, ""):
                    value = operation.get("value")
                    if not isinstance(value, dict):
                        raise ConfigurationError("新条目必须为配置对象")
                    key = automatic_identifier(kind, value, raw[kind])
                if not isinstance(kind, str) or kind not in {"providers", "models", "roles"}:
                    raise ConfigurationError("条目类别或标识无效")
                _identifier(key, kind)
                path = kind + "." + key
                if path in seen:
                    raise ConfigurationError("同一条目不能在一个批次中重复操作")
                seen.add(path)
                exists = key in raw[kind]
                if op == "create" and not exists:
                    value = operation.get("value")
                    if not isinstance(value, dict):
                        raise ConfigurationError("新条目必须为配置对象")
                    raw[kind][key] = deepcopy(value)
                    text = patch_text(text, path, value)
                    after = deepcopy(value)
                elif op == "delete" and exists:
                    concept_refs = {(raw.get("memory", {}).get(section) or {}).get("model_role", section)
                                    for section in CONCEPT_ROLES}
                    if kind == "roles" and key in {"chat", "embedding", "compress", "memory_extractor", "subagent", *CONCEPT_ROLES} | concept_refs:
                        raise ConfigurationError("系统角色不能删除，可修改其模型分配")
                    del raw[kind][key]
                    text = remove_text(text, path)
                    after = "删除条目，保存后需要重启"
                else:
                    raise ConfigurationError("条目已存在、不存在或操作类型无效")
                impact = before_refs.get(kind, {}).get(key, [])
                diff.append({"path": path, "label": path, "before": "已存在" if exists else "未创建", "after": after,
                             "effect": "restart", "impact": impact})
            self.validate(raw, [])
            if self.files.effective(text) != raw:
                raise ConfigurationError("结构变更无法可靠写入")
            return self._write(current, text, diff) if apply else {"revision": current, "changes": diff}

    def history(self):
        if self.backup_dir.is_symlink():
            raise ConfigurationError("配置备份目录不能是符号链接")
        files = [p for p in self.backup_dir.glob("*.yaml") if not p.is_symlink() and p.is_file()]
        return [{"id": p.name, "created_at": p.stat().st_mtime}
                for p in sorted(files, key=lambda item: item.stat().st_mtime_ns, reverse=True)[:20]]

    def _backup_config(self, backup):
        if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{32}\.yaml", backup):
            raise ConfigurationError("备份标识无效")
        path = self.backup_dir / backup
        if self.backup_dir.is_symlink() or path.is_symlink() or not path.is_file():
            raise ConfigurationError("备份不存在或不是普通文件")
        previous = self.files.effective(path.read_text(encoding="utf-8-sig"))
        validate_structure(previous)
        return previous

    def restore(self, backup, revision, *, apply=False):
        with self._transaction() if apply else self._lock:
            current, text, raw = self.read()
            if current != revision:
                raise ConfigurationConflict("配置已被其他操作修改，请刷新后重新预览")
            previous = self._backup_config(backup)
            candidate = deepcopy(raw)
            diff = []
            before_fields, after_fields = fields_for(raw), fields_for(previous)
            # Restore all three linked registries as one transaction, including removals.
            for kind in ("providers", "models", "roles"):
                for key in sorted(set(raw[kind]) | set(previous[kind])):
                    path = kind + "." + key
                    if key not in raw[kind] or key not in previous[kind]:
                        adding = key in previous[kind]
                        diff.append({"path": path, "label": ("新增 " if adding else "删除 ") + path,
                                     "before": "不存在" if adding else "存在",
                                     "after": "从备份恢复条目" if adding else "删除当前条目", "effect": "restart"})
                    elif raw[kind][key] != previous[kind][key]:
                        # Field-level output uses the same credential redaction as normal previews.
                        changed_fields = {p: f for p, f in {**before_fields, **after_fields}.items()
                                          if p.startswith(path + ".")}
                        detailed = False
                        for p, field in changed_fields.items():
                            before = value_at(raw, p, "未指定")
                            after = value_at(previous, p, "未指定")
                            if before != after or type(before) is not type(after):
                                diff.append({"path": p, "label": "修改 " + p,
                                             "before": public_value(field, before),
                                             "after": public_value(field, after), "effect": field.effect})
                                detailed = True
                        if not detailed:
                            diff.append({"path": path, "label": "修改 " + path,
                                         "before": "当前条目", "after": "备份条目", "effect": "restart"})
                candidate[kind] = deepcopy(previous[kind])
                if raw[kind] != previous[kind]:
                    text = replace_section(text, kind, previous[kind])
            missing = Ellipsis
            for path, field in before_fields.items():
                if path.split(".")[0] in {"providers", "models", "roles"}:
                    continue
                before, after = value_at(raw, path, missing), value_at(previous, path, missing)
                if before == after and type(before) is type(after):
                    continue
                if after is missing:
                    remove_value(candidate, path)
                    text = remove_text(text, path)
                else:
                    set_value(candidate, path, after)
                    text = patch_text(text, path, after)
                diff.append({"path": path, "label": "修改 " + field.label,
                             "before": "未指定" if before is missing else public_value(field, before),
                             "after": "未指定" if after is missing else public_value(field, after),
                             "effect": field.effect})
            self.validate(candidate, list(fields_for(candidate)))
            if self.files.effective(text) != candidate:
                raise ConfigurationError("备份恢复文本无法可靠更新")
            if apply:
                return self._write(current, text, diff)
            return {"revision": current, "changes": diff}

    def public(self, active=None, *, loaded=None):
        revision, text, raw = loaded if loaded is not None else self.read()
        user = parse_config(text)
        result = []
        active_fields = fields_for(active) if active is not None else {}
        for field in fields_for(raw).values():
            saved = value_at(raw, field.path, field.default)
            live_default = active_fields.get(field.path, field).default
            live = value_at(active, field.path, live_default) if active is not None else None
            inheritable = field.path.startswith("roles.") and ".overrides." in field.path
            # 角色参数的 source=default 已用于表示“继承模型”，保持页面该语义。
            declared = raw if inheritable else user
            record = asdict(field)
            record.update(value=public_value(field, saved),
                          inheritable=inheritable,
                          source="file" if value_at(declared, field.path, "__missing__") != "__missing__" else "default",
                          protected=public_value(field, saved) != saved)
            if field.kind == "env":
                record["configured"] = bool(os.getenv(saved)) if isinstance(saved, str) and saved else False
            if active is not None:
                record.update(active=public_value(field, live), pending=saved != live)
            result.append(record)
        credentials = {}
        for role, model in resolved_models(raw).items():
            if isinstance(model, dict):
                name = model.get("api_key_env", "")
                credentials[role] = {"auth": model.get("auth", "api_key"),
                                     "configured": bool(os.getenv(name)) if isinstance(name, str) and name else False}
        entities = {}
        refs = references(raw)
        for kind in ("providers", "models", "roles"):
            entities[kind] = [{"id": key, "label": value.get("label") or key,
                               **{k: value[k] for k in ("type", "provider", "model", "selectable", "name") if k in value},
                               "references": refs.get(kind, {}).get(key, [])}
                              for key, value in raw[kind].items()]
        connection_status = {}
        for key, item in raw["providers"].items():
            env = item.get("api_key_env", "")
            connection_status[key] = {"auth": PROVIDERS[item["type"]].auth, "configured": bool(os.getenv(env)) if env else False}
        return {"revision": revision, "fields": result, "credentials": credentials, "history": self.history(),
                "connection_status": connection_status, "provider_types": catalog(), "entities": entities}
