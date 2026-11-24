"""
嵌入生成模块

调用 qwen3.7-text-embedding（OpenAI 兼容接口）生成文本向量。
采用单例模式，避免重复创建 AsyncOpenAI 客户端。
"""

import logging
import math

from config.manager import ConfigManager

logger = logging.getLogger(__name__)


def _load_openai() -> None:
    from openai import AsyncOpenAI as _AsyncOpenAI, DefaultAsyncHttpxClient as _DefaultAsyncHttpxClient

    globals().setdefault("AsyncOpenAI", _AsyncOpenAI)
    globals().setdefault("DefaultAsyncHttpxClient", _DefaultAsyncHttpxClient)


def __getattr__(name: str):
    if name in ("AsyncOpenAI", "DefaultAsyncHttpxClient"):
        _load_openai()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# qwen3.7-text-embedding 单次最多 20 条
_BATCH_SIZE = 20


class EmbeddingClient:
    """
    向量生成客户端（单例）
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _init(self, raw_config=None):
        """从配置中初始化模型参数"""
        manager = ConfigManager()
        memory_cfg = manager.get_memory_config() if raw_config is None else raw_config.get("memory", {})
        emb_cfg = memory_cfg.get("embedding", {})

        model_role = emb_cfg.get("model_role", "embedding")
        model = manager.get_model(model_role, raw_config=raw_config)

        self.model_name = model.name
        self.dimensions = emb_cfg.get("dimensions", 1024)
        if "AsyncOpenAI" not in globals() or (
            model.proxy is not None and "DefaultAsyncHttpxClient" not in globals()
        ):
            _load_openai()
        client_options = {}
        if model.proxy is not None:
            client_options["http_client"] = globals()["DefaultAsyncHttpxClient"](
                proxy=model.proxy or None, trust_env=False,
            )
        self.client = globals()["AsyncOpenAI"](api_key=model.api_key, base_url=model.base_url, **client_options)
        self._initialized = True

        logger.info(f"EmbeddingClient 初始化完成: {self.model_name}, dim={self.dimensions}")

    async def close(self):
        if self._initialized:
            self._initialized = False
            await self.client.close()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        批量生成 embedding，自动去重并按原顺序返回。

        Args:
            texts: 待编码文本列表

        Returns:
            与输入顺序一致的 embedding 列表
        """
        if not self._initialized:
            self._init()

        if not texts:
            return []

        # 去重，保留顺序，减少重复 API 调用
        seen = {}
        order = []
        for t in texts:
            key = t.strip()
            if key not in seen:
                seen[key] = len(seen)
                order.append(key)

        embeddings_map = {}
        for i in range(0, len(order), _BATCH_SIZE):
            batch = order[i : i + _BATCH_SIZE]
            try:
                resp = await self.client.embeddings.create(
                    model=self.model_name,
                    input=batch,
                    dimensions=self.dimensions,
                )
                items = sorted(resp.data, key=lambda item: item.index)
                if len(items) != len(batch) or [item.index for item in items] != list(range(len(batch))):
                    raise ValueError("embedding响应未完整覆盖输入")
                for text, item in zip(batch, items):
                    vector = item.embedding
                    if (len(vector) != self.dimensions
                            or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
                            or not any(x != 0 for x in vector)):
                        raise ValueError("embedding响应包含无效向量")
                    embeddings_map[text] = vector
            except Exception as e:
                logger.error("生成 embedding 失败: %s", type(e).__name__)
                raise

        return [embeddings_map[t.strip()] for t in texts]
