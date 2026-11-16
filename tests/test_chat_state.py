"""重连展示快照的顺序、隔离和生命周期；全部使用合成事件。"""

from web.chat_state import ChatState


def event(kind, data, round_id="synthetic"):
    return {"type": kind, "data": data, "round": {"round_id": round_id, "request_id": None}}


def test_snapshot_compacts_deltas_without_mutating_sent_events_or_reordering_boundaries():
    state = ChatState()
    first = state.record(event("message_delta", {"text": "first", "platform": "web"}))
    state.record(event("message_delta", {"text": " second", "platform": "web"}))
    state.record(event("message_seg_end", {}))
    state.record(event("message_delta", {"text": "third", "platform": "web"}))
    snapshot = state.snapshot([{"round_id": "synthetic"}])
    assert first["data"]["text"] == "first"
    assert snapshot["seq"] == 4
    assert [item["chat_seq"] for item in snapshot["events"]] == [2, 3, 4]
    assert snapshot["events"][0]["data"]["text"] == "first second"
    snapshot["events"][0]["data"]["text"] = "mutated client copy"
    assert state.snapshot([{"round_id": "synthetic"}])["events"][0]["data"]["text"] == "first second"


def test_snapshot_drops_finished_streams_and_retains_bounded_terminal_receipts():
    state = ChatState()
    for number in range(130):
        round_id = str(number)
        state.record(event("message_delta", {"text": "synthetic"}, round_id))
        state.record(event("round_finished", {"round_id": round_id, "status": "completed"}, round_id))
    snapshot = state.snapshot([])
    assert snapshot["events"] == [] and state._events == {}
    assert len(snapshot["finished"]) == 128 and snapshot["finished"][0]["round_id"] == "2"
    assert ChatState().instance_id != state.instance_id


def test_snapshot_only_contains_registered_live_rounds_and_round_events_have_a_shared_sequence():
    state = ChatState()
    state.record(event("message_delta", {"text": "visible"}, "live"))
    state.record(event("message_delta", {"text": "orphan"}, "other"))
    hello = {"type": "hello", "data": {}}
    assert state.record(hello) is hello
    snapshot = state.snapshot([{"round_id": "live"}])
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["data"]["text"] == "visible"
    assert snapshot["events"][0]["chat_instance"] == snapshot["instance_id"]
    assert snapshot["seq"] == 2
