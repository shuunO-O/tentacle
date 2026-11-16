# 工具加载与启动校验

`run_web.py::main` 使用 `runtime/tool_loader.py::ToolLoader` 自动发现和注册工具，校验配置并装配依赖。
所有校验通过后才启动子 agent 通知、Web、QQ、心跳和 scheduler。校验不会执行工具或调用外部 API。
加载失败会恢复注册表并报告出错模块；配置和依赖错误分别汇总后抛出 `ToolStartupError`。

## 添加工具

在 `tools/` 新建单层 Python 模块，例如 `tools/example.py`：

```python
def example(args: dict) -> str:
    return args["text"]


def register_tools(registry):
    registry.register(
        name="example",
        description="返回提供的文本。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        func=example,
    )
```

再将工具名加入模型角色的 `tools.enabled` 列表。无需编辑入口 import。
工具函数接收一个参数字典；异步函数自动识别，也可以保留旧的 `is_async` 声明，但声明必须与函数一致。
模块可以注册多个工具，模块名不必等于工具名。
加载器按模块名排序扫描，跳过 `_` 前缀模块及包；辅助代码请以 `_` 开头。
普通模块缺少 `register_tools`、导入失败、重名注册均会阻止启动。
模块导入与注册函数应仅定义工具，不启动后台任务、执行工具或读取用户消息。
本机制在启动时发现工具，不提供运行期间热更新。

## 运行依赖

需要共享运行对象的模块仍保留 setter 和原工具函数，由加载器统一调用 setter：

```python
_service = None


def set_service(value):
    global _service
    _service = value


TOOL_BINDINGS = {"example_service": set_service}


def register_tools(registry):
    registry.register(
        "example", "示例", {"type": "object"}, example,
        requires=("example_service",),
    )
```

入口 `tool_loader.bind(context, selected_tools)` 的 context 提供该对象。
新增一种共享依赖时需要在入口创建并提供；使用已有依赖的工具无需修改入口。
所选工具声明的依赖必须非 None，且存在对应 `TOOL_BINDINGS` 声明；校验通过后统一绑定。
关闭时 `unbind()` 对 setter 传入 None，因此 setter 必须允许清空，不应承担后台资源启动或关闭职责。

子 agent manager 对象在校验前创建、在校验后 start；DSH Web Host 在首次派发任务或打开编程台时启动。
发送通道、Ladybug、模型、浏览器和远端服务的可用性仍由原有运行期逻辑处理，启动校验不发送消息或探测凭据。

## 校验边界与兼容

- 注册检查名称、重名、可调用对象及单字典调用签名、异步声明、参数基本结构、JSON 可序列化及 required 属性引用。
  兼容空 schema `{}`；不是完整 JSON Schema 验证器，也不增加运行期参数验证。
- 所有模型角色的工具列表检查类型、重复项及工具是否存在。未配置/null/空列表均沿用模型调用侧的禁用语义。
  `ToolRegistry.get_definitions(None)` 作为查询接口仍表示全部，`get_definitions([])` 返回空列表，未知名称抛错。
- 外部 coding 子 agent 使用 DeepSeek Harness 的模型和工具配置。主 agent 通过 `subagent_create` 等工具管理任务，见 [子 agent](SUBAGENTS.md)。
- `execute()` 的工具异常仍转为 JSON 错误；取消继续向上传递，保留任务超时和清理行为。
  模型启用列表仅控制定义暴露，尚不构成主注册表或 scheduler 的执行权限边界。

离线验证使用项目虚拟环境运行 pytest 与 `ruff check .`，不以启动应用验收。
