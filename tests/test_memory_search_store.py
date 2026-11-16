"""合成检索数据及驱动替身；不初始化数据库、embedding 或读取私人资料。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memory.ladybug_store import LadybugStore, search_date_bounds


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for row in self.rows:
            yield row


class Session:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run(self, query, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        return Rows(self.rows)


def store_with_session(rows=()):
    store = object.__new__(LadybugStore)
    session = Session(rows)
    store.driver = SimpleNamespace(session=lambda: session)
    return store, session


@pytest.mark.parametrize("args, expected", [
    ((None, None, None), (None, None)),
    (("2026-10-22", None, None), ("2026-10-22T00:00:00", "2026-10-23T00:00:00")),
    ((None, "2026-10-01", "2026-10-22"), ("2026-10-01T00:00:00", "2026-10-23T00:00:00")),
    ((None, "2026-10-22", None), ("2026-10-22T00:00:00", None)),
    ((None, None, "2026-10-22"), (None, "2026-10-23T00:00:00")),
    (("2024-02-29", None, None), ("2024-02-29T00:00:00", "2024-03-01T00:00:00")),
    (("2026-12-31", None, None), ("2026-12-31T00:00:00", "2027-01-01T00:00:00")),
])
def test_inclusive_calendar_dates_become_half_open_bounds(args, expected):
    assert search_date_bounds(*args) == expected
    start, end = search_date_bounds("2026-10-22")
    assert start <= "2026-10-22T23:59:59.999999" < end
    assert not start <= "2026-10-23T00:00:00" < end


@pytest.mark.parametrize("label", ["Event", "Person", "Concept"])
@pytest.mark.parametrize("start,end", [
    ("2026-10-01T00:00:00", "2026-10-23T00:00:00"),
    ("2026-10-01T00:00:00", None), (None, "2026-10-23T00:00:00"),
])
async def test_filter_query_applies_dates_to_events_for_all_node_types(label, start, end):
    store, session = store_with_session()
    assert await store._filtered_search(label, None, "synthetic-alias", start, end, 3) == []
    query, args, _ = session.calls[0]
    params = args[0]
    if label == "Event":
        assert "MATCH (n)-[r:" in query
        assert "m.name=$entity_filter" in query
        time_var = "n"
    elif label == "Person":
        assert "n.name=$entity_filter" in query and "n.aliases" in query
        assert "EXISTS { MATCH (n)-[]-(e:Event)" in query
        time_var = "e"
    else:
        assert "named.name=$entity_filter" in query
        assert "equivalence_group" in query and "member:Concept" in query
        time_var = "e"
    assert (f"{time_var}.create_time >= $start_time" in query) == bool(start)
    assert (f"{time_var}.create_time < $end_time" in query) == bool(end)
    assert ("start_time" in params) == bool(start)
    assert ("end_time" in params) == bool(end)
    assert "<= $end_time" not in query
    assert params["entity_filter"] == "synthetic-alias"


@pytest.mark.parametrize("label", ["Event", "Person", "Concept"])
async def test_low_scores_retained_and_each_entity_has_bounded_events(label):
    store = object.__new__(LadybugStore)
    store._initialized = True
    store.top_k = 3
    store._ensure_indexes = AsyncMock()
    store.embedding_client = SimpleNamespace(embed=AsyncMock(return_value=[[0.1]]))
    nodes = [{"id": "synthetic-1", "score": 0.3}, {"id": "synthetic-2", "score": 0.01}]
    store._filtered_search = AsyncMock(return_value=nodes)
    store._vector_search = AsyncMock()
    store._get_context = AsyncMock(side_effect=lambda _: {"persons": []})
    store._get_related_events = AsyncMock(side_effect=lambda *_: {"events": [], "events_has_more": False})
    data = await store.search("query", label, None, date_from="2026-10-01", date_to="2026-10-22", event_limit=2)
    assert data["primary_match"]["node"]["score"] == 0.3
    assert data["secondary_matches"][0]["score"] == 0.01
    store._filtered_search.assert_awaited_once_with(label, [0.1], None,
                                                  "2026-10-01T00:00:00", "2026-10-23T00:00:00", 3)
    store._vector_search.assert_not_awaited()
    if label == "Event":
        assert store._get_context.await_count == 1
        store._get_related_events.assert_not_awaited()
    else:
        assert store._get_context.await_count == 2
        assert store._get_related_events.await_count == 2
        store._get_related_events.assert_any_await("synthetic-2", "2026-10-01T00:00:00", "2026-10-23T00:00:00", 2)
        assert "events" in data["secondary_matches"][0]["context"]


async def test_unfiltered_semantic_search_uses_vector_ranking():
    store = object.__new__(LadybugStore)
    store._initialized = True
    store.top_k = 3
    store._ensure_indexes = AsyncMock()
    store.embedding_client = SimpleNamespace(embed=AsyncMock(return_value=[[0.1]]))
    store._filtered_search = AsyncMock()
    store._vector_search = AsyncMock(return_value=[])
    assert await store.search("query", "Person", None) == {"primary_match": None, "secondary_matches": []}
    store._vector_search.assert_awaited_once_with("Person", [0.1], 3)
    store._filtered_search.assert_not_awaited()


@pytest.mark.parametrize("count,limit,has_more", [(0, 3, False), (2, 3, False), (3, 3, False), (4, 3, True), (11, 10, True)])
async def test_related_event_query_limits_distinct_events_and_never_selects_body(count, limit, has_more):
    events = [{"id": f"event-{i}", "summary": f"summary-{i}", "relations": []} for i in range(count)]
    store, session = store_with_session(events)
    store._concept_member_ids = AsyncMock(return_value=[])
    result = await store._get_related_events("synthetic-person", "2026-10-01T00:00:00", "2026-10-23T00:00:00", limit)
    assert result == {"events": events[:limit], "events_has_more": has_more}
    query, args, _ = session.calls[0]
    assert "WITH e,collect(r)" in query  # 多条关系聚合成一条事件，保留各关系属性。
    assert "ORDER BY e.create_time DESC,e.id" in query
    assert "LIMIT $fetch_limit" in query and args[0]["fetch_limit"] == limit + 1
    assert "e.summary AS summary" in query and "e.content" not in query
    assert "e.create_time >= $start_time" in query and "e.create_time < $end_time" in query


@pytest.mark.parametrize("kwargs", [
    {"time_hint": "2026"}, {"date_from": "2026-10-23", "date_to": "2026-10-22"},
    {"time_hint": "2026-10-22", "date_to": "2026-10-22"},
    {"event_limit": 0}, {"event_limit": 11}, {"event_limit": True},
])
async def test_store_rejects_invalid_filters_without_initializing(kwargs):
    store = object.__new__(LadybugStore)
    store._init = lambda: pytest.fail("invalid filters must not initialize store")
    with pytest.raises(ValueError):
        await store.search("query", "Person", None, **kwargs)
