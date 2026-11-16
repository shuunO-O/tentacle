"""仅 GitHub runner 临时 Ladybug 数据库的合成图集成测试；默认跳过，不连接本机/生产。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from memory.ladybug_driver import LadybugDriver

from memory.ladybug_store import LadybugStore
from tools.memory_database_search import _format_search_data


@pytest_asyncio.fixture
async def graph(tmp_path):
    prefix = "synthetic-search-" + uuid4().hex
    driver = LadybugDriver(tmp_path / "synthetic.lbdb", 2)
    person_id, concept_id, outside_id = prefix + "-person", prefix + "-concept", prefix + "-outside"
    events = [
        {"id": prefix + "-before", "time": "2026-10-21T23:59:59.999999"},
        {"id": prefix + "-start", "time": "2026-10-22T00:00:00"},
        {"id": prefix + "-middle", "time": "2026-10-22T12:00:00"},
        {"id": prefix + "-last", "time": "2026-10-22T23:59:59.999999"},
        {"id": prefix + "-after", "time": "2026-10-23T00:00:00"},
    ]
    ids = [person_id, concept_id, outside_id] + [e["id"] for e in events]
    try:
        async with driver.session() as session:
            await session.run("""
                CREATE (p:Person {id: $person_id, name: $person_id, aliases: [$person_alias], embedding: [1.0, 0.0]}),
                       (c:Concept {id: $concept_id, equivalence_group:$concept_id, name: $concept_id, aliases: [$concept_alias], embedding: [1.0, 0.0]}),
                       (o:Person {id: $outside_id, name: $outside_id, embedding: [1.0, 0.0]})
                WITH p, c, o
                UNWIND $events AS row
                CREATE (e:Event {id: row.id, create_time: row.time, type: 'synthetic',
                                summary: row.id, content: 'FULL_EVENT_BODY', embedding: [1.0, 0.0]}),
                       (p)-[:PARTICIPATED_IN]->(e), (p)-[:HAS_ATTITUDE {sentiment: 'good'}]->(e),
                       (e)-[:MENTIONS]->(c)
                WITH o, e WHERE e.id = $after_id
                CREATE (o)-[:PARTICIPATED_IN]->(e)
            """, person_id=person_id, concept_id=concept_id, outside_id=outside_id,
                person_alias=person_id + "-alias", concept_alias=concept_id + "-alias",
                events=events, after_id=events[-1]["id"])
        store = object.__new__(LadybugStore)
        store._initialized = True
        store.dimensions = 2
        store.driver = driver
        store.top_k = 3
        store._ensure_indexes = AsyncMock()
        store.embedding_client = SimpleNamespace(embed=AsyncMock(return_value=[[1.0, 0.0]]))
        yield SimpleNamespace(store=store, person=person_id, concept=concept_id, outside=outside_id,
                              events=events, driver=driver)
    finally:
        async with driver.session() as session:
            await session.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=ids)
        await driver.close()


@pytest.mark.parametrize("label,attr", [("Person", "person"), ("Concept", "concept")])
async def test_entity_alias_and_date_range_return_latest_distinct_event_summaries(graph, label, attr):
    node_id = getattr(graph, attr)
    data = await graph.store.search(None, label, node_id + "-alias",
                                    date_from="2026-10-22", date_to="2026-10-22", event_limit=2)
    primary = data["primary_match"]
    assert primary["node"]["id"] == node_id
    context = primary["context"]
    assert [e["id"] for e in context["events"]] == [graph.events[3]["id"], graph.events[2]["id"]]
    assert context["events_has_more"] is True
    assert all("content" not in event for event in context["events"])
    if label == "Person":
        relations = context["events"][0]["relations"]
        assert {r["relation"] for r in relations} == {"PARTICIPATED_IN", "HAS_ATTITUDE"}
        assert {"relation": "HAS_ATTITUDE", "properties": {"sentiment": "good"}} in relations
    output = _format_search_data(data)
    assert "关联事件:2(还有更多)" in output
    assert "FULL_EVENT_BODY" not in output


async def test_event_dates_include_last_fractional_second_and_keep_full_body(graph):
    data = await graph.store.search("query", "Event", graph.person + "-alias", time_hint="2026-10-22")
    nodes = [data["primary_match"]["node"]] + data["secondary_matches"]
    assert {n["id"] for n in nodes} == {e["id"] for e in graph.events[1:4]}
    assert all(n["content"] == "FULL_EVENT_BODY" for n in nodes)


async def test_entity_with_only_outside_events_does_not_match_range(graph):
    data = await graph.store.search(None, "Person", graph.outside,
                                    date_from="2026-10-22", date_to="2026-10-22")
    assert data == {"primary_match": None, "secondary_matches": []}


@pytest.mark.parametrize("bounds,indices", [
    ({"date_from": "2026-10-22"}, [4, 3, 2, 1]),
    ({"date_to": "2026-10-22"}, [3, 2, 1, 0]),
])
async def test_open_date_ranges_also_restrict_related_event_summaries(graph, bounds, indices):
    data = await graph.store.search("query", "Concept", graph.concept, event_limit=10, **bounds)
    context = data["primary_match"]["context"]
    assert [e["id"] for e in context["events"]] == [graph.events[i]["id"] for i in indices]
    assert context["events_has_more"] is False
