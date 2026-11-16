# 网页搜索与正文提取

Tentacle 提供 `web_search` 和 `web_fetch` 两个公开网页工具，支持 **Parallel**、**Tavily** 与 **DeepSeek** 三种后端。仅将本次问题或 URL 发给所选服务，不会传送完整聊天历史。

搜索调用示例：

```json
{"queries": ["Parallel Search API documentation"], "objective": "查找官方搜索接口", "max_results": 5}
```

正文提取调用示例：

```json
{"urls": ["https://docs.parallel.ai/search/search-quickstart"], "objective": "了解搜索请求格式"}
```

## 选择后端与密钥

在 Web「应用配置 → 公开网页工具」或 `config.yaml` 中选择服务商（`parallel` / `tavily` / `deepseek`），并在 `.env` 中填写对应的 API Key：

| `provider` | 默认环境变量名 | 说明 |
| --- | --- | --- |
| `parallel` | `PARALLEL_API_KEY` | Parallel Search & Extract API |
| `tavily` | `TAVILY_API_KEY` | Tavily Search & Extract API |
| `deepseek` | `DEEPSEEK_API_KEY` | DeepSeek Tools Search & Fetch API |

`web_tools.api_key_env` 留空时会自动选用上表对应的默认环境变量名；如需自定义环境变量名，也可显式指定：

```yaml
web_tools:
  provider: tavily          # parallel / tavily / deepseek
  api_key_env: ""           # 留空自动选用对应默认环境变量名
  timeout_seconds: 60
  max_output_chars: 40000
```

在模型的工具列表中启用 `web_search` 和 `web_fetch`。如果用应用配置页面管理模型，请在那里调整工具权限。

搜索最多返回 10 条结果；正文提取最多接收 5 个公开 HTTP(S) URL。响应会受超时和字符上限约束，来源 URL 保留。登录态页面和需要账户授权的内容请使用浏览器功能。

API 细节参考：
- [Parallel Search](https://docs.parallel.ai/search/search-quickstart) 与 [Extract](https://docs.parallel.ai/extract/extract-quickstart)
- [Tavily Search / Extract API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [DeepSeek Search / Fetch API](https://api-docs.deepseek.com/)
