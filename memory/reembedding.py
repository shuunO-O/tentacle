"""离线重算整个 Ladybug 记忆库的向量，成功后一次切换数据库文件。"""

from contextlib import contextmanager, nullcontext
import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile

import ladybug

from config.files import ConfigFiles
from memory.embeddings import EmbeddingClient
from memory.ladybug_driver import LABELS, RELATIONS, LadybugDriver, offload
from runtime.paths import state_path
from memory.ladybug_store import LadybugStore
from memory.retrieval import source_chunks


VECTOR_TEXT = {"Event": "summary", **dict.fromkeys(LABELS[1:], "name"), "EventSource": "text"}
BATCH_SIZE = 64
SIDECARS = (".wal", ".shadow", ".tmp")
logger = logging.getLogger(__name__)


class ReembeddingError(ValueError):
    """可安全展示给操作者的错误，不包含库内容或模型请求。"""


def sidecars_exist(path):
    return any(os.path.lexists(str(path) + suffix) for suffix in SIDECARS)


def check_output(path):
    if os.path.lexists(path) or sidecars_exist(path):
        raise ReembeddingError("目标库或其辅助文件已存在，请指定新路径")


def check_snapshot(database):
    required = {*VECTOR_TEXT, *RELATIONS, "HAS_SOURCE", "MemoryMeta", "ConceptGroupAudit"}
    with ladybug.Connection(database) as connection:
        result = connection.execute("CALL SHOW_TABLES() RETURN name")
        try:
            tables = {row[0] for row in result.get_all()}
        finally:
            result.close()
    if not required <= tables:
        raise ReembeddingError("源库缺少 Tentacle 记忆表")


async def embed_texts(client, texts):
    vectors = await client.embed(texts)
    if len(vectors) != len(texts):
        raise ReembeddingError("模型返回的向量数量不完整")
    return vectors


async def update_vectors(session, client, label, text_field):
    source_metadata = ",n.embedding_dim=$dimensions" if label == "EventSource" else ""
    model_params = {"model": client.model_name}
    if source_metadata:
        model_params["dimensions"] = client.dimensions
    update = f"""UNWIND $rows AS row MATCH (n:{label} {{id:row.id}})
        SET n.embedding=CAST(row.vector AS DOUBLE[{client.dimensions}]),
            n.embedding_model=$model{source_metadata}"""
    count, after = 0, ""
    while True:
        rows = [row async for row in await session.run(f"""MATCH (n:{label}) WHERE n.id>$after
            RETURN n.id AS id,n.{text_field} AS text ORDER BY n.id LIMIT $limit""",
            after=after, limit=BATCH_SIZE)]
        if not rows:
            break
        texts = [row["text"] for row in rows]
        if any(not isinstance(value, str) or not value.strip() for value in texts):
            raise ReembeddingError(f"{label} 存在空摘要、名称或切片，无法完整生成向量")
        vectors = await embed_texts(client, texts)
        await session.run(update, rows=[{"id": row["id"], "vector": vector}
                                       for row, vector in zip(rows, vectors, strict=True)], **model_params)
        after = rows[-1]["id"]
        count += len(rows)

    observed = [row async for row in await session.run(
        f"MATCH (n:{label}) RETURN count(n) AS total,count(n.embedding) AS vectors")]
    if observed != [{"total": count, "vectors": count}]:
        raise ReembeddingError(f"{label} 向量写入不完整")
    return count


async def add_missing_sources(session, client):
    after, count = "", 0

    async def write_sources(tx, event_id, chunks, vectors):
        for chunk, vector in zip(chunks, vectors, strict=True):
            await LadybugStore._create_source(tx, event_id, {
                **chunk, "id": f'{event_id}:source:{chunk["start"]}', "embedding": vector,
                "embedding_model": client.model_name, "embedding_dim": client.dimensions,
            })

    while True:
        events = [row async for row in await session.run("""MATCH (e:Event)
            WHERE e.id>$after AND e.content<>'' AND NOT EXISTS { MATCH (e)-[:HAS_SOURCE]->(:EventSource) }
            RETURN e.id AS id,e.content AS content ORDER BY e.id LIMIT $limit""",
            after=after, limit=BATCH_SIZE)]
        if not events:
            return count
        for event in events:
            chunks = source_chunks(event["content"])
            for start in range(0, len(chunks), BATCH_SIZE):
                batch = chunks[start:start + BATCH_SIZE]
                vectors = await embed_texts(client, [chunk["text"] for chunk in batch])
                await session.execute_write(write_sources, event["id"], batch, vectors)
                count += len(batch)
        after = events[-1]["id"]


@contextmanager
def replacement_lock(source):
    lock = source.with_name(f".{source.name}.reembed.lock")
    try:
        lock.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        raise ReembeddingError("源库已有全量向量重算。若上次异常退出，确认无重算进程后清理对应的 .reembed.lock") from None
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def file_revision(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def replace_source(staging, source, before):
    if hasattr(os, "chown"):
        current = staging.stat()
        if (current.st_uid, current.st_gid) != (before.st_uid, before.st_gid):
            os.chown(staging, before.st_uid, before.st_gid)
    staging.chmod(stat.S_IMODE(before.st_mode))
    if sidecars_exist(source) or file_revision(source.stat()) != file_revision(before):
        raise ReembeddingError("源库在重算期间发生变化，未切换文件")
    os.replace(staging, source)


async def rebuild_vectors(source, output, client, *, check_before_publish=None):
    """在副本中重算，默认替换原路径，指定 output 时生成单独文件。"""
    source = Path(source).resolve()
    in_place = output is None
    output = source if in_place else Path(output).absolute()
    dimensions = client.dimensions
    if type(dimensions) is not int or dimensions < 1 or not client.model_name:
        raise ReembeddingError("目标 embedding 模型名或维度无效")
    if not source.is_file():
        raise ReembeddingError("找不到源 Ladybug 文件")
    if not in_place:
        check_output(output)
    if sidecars_exist(source):
        raise ReembeddingError("源库还有辅助文件，请让宿主正常关闭后重试")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = replacement_lock(source) if in_place else nullcontext()
    with lock, tempfile.TemporaryDirectory(prefix=".reembed-", dir=output.parent) as temporary:
        staging = Path(temporary) / "memory.lbdb"
        with ladybug.Database(source, read_only=True) as snapshot:
            if sidecars_exist(source):
                raise ReembeddingError("源库还有辅助文件，请让宿主正常关闭后重试")
            before = source.stat()
            await offload(check_snapshot, snapshot)
            await offload(shutil.copyfile, source, staging)
            driver = LadybugDriver(staging, dimensions)
            try:
                async with driver.session() as session:
                    counts = {}
                    for label, text_field in VECTOR_TEXT.items():
                        await session.run(f"ALTER TABLE {label} DROP embedding")
                        await session.run(f"ALTER TABLE {label} ADD embedding DOUBLE[{dimensions}]")
                        counts[label] = await update_vectors(session, client, label, text_field)
                    new_sources = await add_missing_sources(session, client)
                    await session.run("""MERGE (m:MemoryMeta {id:'embedding'})
                        SET m.model=$model,m.dimensions=$dimensions""", model=client.model_name, dimensions=dimensions)
                    await session.run("CHECKPOINT")
            finally:
                await driver.close()
            if sidecars_exist(staging):
                raise ReembeddingError("目标库尚有未合并的辅助文件")
            if check_before_publish is not None:
                await check_before_publish()
            if in_place:
                # Windows 不允许替换已打开的文件。其他平台持有只读锁直到切换完成。
                if os.name == "nt":
                    snapshot.close()
                replace_source(staging, source, before)
            else:
                staging.chmod(0o600)
                check_output(output)
                os.link(staging, output)
    counts["EventSource"] += new_sources
    return {"model": client.model_name, "dimensions": dimensions, "nodes": counts,
            "added_sources": new_sources, "output": str(output)}


async def reembed_saved_config(config_path, revision):
    """仅由已确认的重启请求调用，配置版本仍一致才发布新库。"""
    files = config_path if isinstance(config_path, ConfigFiles) else ConfigFiles(config_path)

    def read_checked():
        current, _, raw, _, _ = files.read()
        if current != revision:
            raise ReembeddingError("配置已改变，请按当前配置重新发起重算")
        return raw

    async def check_before_publish():
        await offload(read_checked)

    client = None
    try:
        raw = await offload(read_checked)
        source = state_path(raw.get("memory", {}).get("ladybug", {}).get("path", "data/memory.lbdb"))
        if not source.is_file():
            return {"state": "skipped", "message": "配置指定的路径还没有记忆库，首次写入会使用新模型。"}
        client = EmbeddingClient()
        client._init(raw)
        await rebuild_vectors(source, None, client, check_before_publish=check_before_publish)
        return {"state": "succeeded", "message": "记忆向量重算完成。"}
    except Exception as exc:
        logger.warning("重启期间记忆向量重算失败: %s", type(exc).__name__)
        detail = str(exc) if isinstance(exc, ReembeddingError) else "请检查向量模型与数据库文件。"
        return {"state": "failed", "message": f"重算失败，原库保留。{detail}"}
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.warning("维护用向量客户端关闭失败: %s", type(exc).__name__)
