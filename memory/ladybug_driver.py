"""Ladybug 的异步会话边界；所有连接共享一个本地 Database。"""

import asyncio
import os
from runtime.paths import state_path


def _ladybug():
    mod = globals().get("ladybug")
    if mod is None:
        import ladybug as mod

        globals()["ladybug"] = mod
    return mod


def __getattr__(name: str):
    if name == "ladybug":
        return _ladybug()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


LABELS = ("Event", "Person", "Concept", "Location")
RELATIONS = ("PARTICIPATED_IN", "OCCURRED_AT", "MENTIONS", "HAS_ATTITUDE")


async def offload(function, *args):
    """取消等待时仍等原生线程退出，随后才能释放连接或结果。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def properties(value):
    """剥离 Ladybug 返回的内部节点/关系标识，保留业务属性。"""
    return {k: v for k, v in value.items() if not k.startswith("_") and v is not None}


def normalize(value):
    if isinstance(value, dict):
        return {(k.lower() if k.startswith("_") else k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for row in self.rows:
            yield row


class LadybugDriver:
    def __init__(self, path, dimensions, *, max_num_threads: int | None = None, max_idle_connections: int = 2):
        self.path = state_path(path)
        if type(dimensions) is not int or dimensions < 1:
            raise ValueError("记忆向量维度必须为正整数")
        self.dimensions = dimensions
        if max_num_threads is None:
            self.max_num_threads = min(2, os.cpu_count() or 1)
        else:
            self.max_num_threads = max(1, int(max_num_threads))
        self._max_idle_connections = max(0, int(max_idle_connections))
        self.database = None
        self._ready_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._sessions = set()
        self._idle_connections = []
        self._drained = asyncio.Event()
        self._drained.set()
        self._closed = False

    async def ready(self):
        async with self._ready_lock:
            if self._closed:
                raise RuntimeError("记忆数据库已关闭")
            if self.database is not None:
                return
            # 不设 buffer_pool_size / max_db_size 硬上限以免库膨胀触发 OOM，仅限制查询并发线程数降低 arena 碎片。
            ladybug = _ladybug()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            opening = asyncio.create_task(
                asyncio.to_thread(ladybug.Database, str(self.path), max_num_threads=self.max_num_threads)
            )
            try:
                database = await asyncio.shield(opening)
            except asyncio.CancelledError:
                database = await opening
                await offload(database.close)
                raise
            try:
                await offload(self._schema, database)
            except BaseException:
                await offload(database.close)
                raise
            self.database = database

    def _schema(self, database):
        ladybug = _ladybug()
        common = ("id STRING PRIMARY KEY, name STRING, aliases STRING[], summary STRING, content STRING, "
                  "type STRING, create_time STRING, raw_session STRING, first_context STRING, "
                  "equivalence_group STRING, group_joined_at STRING, concept_alignment STRING, source_rebuild_lock INT64, "
                  f"embedding DOUBLE[{self.dimensions}], embedding_model STRING")
        conn = ladybug.Connection(database)
        try:
            for label in LABELS:
                conn.execute(f"CREATE NODE TABLE IF NOT EXISTS {label}({common})").close()
            conn.execute("CREATE NODE TABLE IF NOT EXISTS EventSource(id STRING PRIMARY KEY, start INT64, "
                         f"`end` INT64, text STRING, hash STRING, embedding DOUBLE[{self.dimensions}], "
                         "embedding_model STRING, embedding_dim INT64)").close()
            conn.execute("CREATE NODE TABLE IF NOT EXISTS MemoryMeta(id STRING PRIMARY KEY, model STRING, "
                         "dimensions INT64, lock_revision INT64)").close()
            conn.execute("CREATE NODE TABLE IF NOT EXISTS ConceptGroupAudit(id STRING PRIMARY KEY, "
                         "snapshot STRING, result STRING, created_at STRING)").close()
            endpoints = ",".join(f"FROM {a} TO {b}" for a in LABELS for b in LABELS)
            for kind in RELATIONS:
                conn.execute(f"CREATE REL TABLE IF NOT EXISTS {kind}({endpoints}, sentiment STRING, "
                             "properties_json STRING)").close()
            conn.execute("CREATE REL TABLE IF NOT EXISTS HAS_SOURCE(FROM Event TO EventSource)").close()
        finally:
            conn.close()

    def session(self):
        return Session(self)

    async def close(self):
        async with self._ready_lock:
            self._closed = True
        await self._drained.wait()
        idle = list(self._idle_connections)
        self._idle_connections.clear()
        for conn in idle:
            await offload(conn.close)
        if self.database is not None:
            await offload(self.database.close)
            self.database = None


class Session:
    def __init__(self, driver):
        self.driver = driver
        self.connection = None
        self._dirty = False

    async def __aenter__(self):
        await self.driver.ready()
        if self.driver._closed:
            raise RuntimeError("记忆数据库已关闭")
        # 一个事务的语句必须使用同一原生连接；不同会话仍可并发读取，空闲连接回池复用以避免频繁启停线程。
        if self.driver._idle_connections:
            self.connection = self.driver._idle_connections.pop()
        else:
            self.connection = _ladybug().AsyncConnection(
                self.driver.database,
                max_concurrent_queries=1,
                max_threads_per_query=self.driver.max_num_threads,
            )
        self._dirty = False
        self.driver._sessions.add(self)
        self.driver._drained.clear()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            conn, self.connection = self.connection, None
            if conn is not None:
                if (
                    exc_type is None
                    and not self._dirty
                    and not self.driver._closed
                    and len(self.driver._idle_connections) < self.driver._max_idle_connections
                ):
                    self.driver._idle_connections.append(conn)
                else:
                    await offload(conn.close)
        finally:
            self.driver._sessions.discard(self)
            if not self.driver._sessions:
                self.driver._drained.set()

    async def run(self, query, parameters=None, **kwargs):
        try:
            result = await self.connection.execute(query, {**(parameters or {}), **kwargs})
            try:
                names = result.get_column_names()
                rows = await offload(result.get_all)
                return Rows([dict(zip(names, map(normalize, row))) for row in rows])
            finally:
                result.close()
        except BaseException:
            self._dirty = True
            raise

    async def _transaction(self, callback, args, kwargs, *, write):
        await self.run("BEGIN TRANSACTION" if write else "BEGIN TRANSACTION READ ONLY")
        try:
            result = await callback(self, *args, **kwargs)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            await self.run("COMMIT")
            return result
        except BaseException:
            # 原生异步 API 已中断并等待在途查询，随后在同一连接回滚。
            try:
                await asyncio.shield(self.run("ROLLBACK"))
            except Exception:
                # COMMIT 已完成而取消回执时，不能用“无活动事务”覆盖原异常。
                pass
            raise

    async def execute_write(self, callback, *args, **kwargs):
        async with self.driver._write_lock:
            return await self._transaction(callback, args, kwargs, write=True)

    async def execute_read(self, callback, *args, **kwargs):
        return await self._transaction(callback, args, kwargs, write=False)
