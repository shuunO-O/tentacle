"""历史校验只使用合成消息，不访问归档或模型。"""

from copy import deepcopy
import json

import pytest

from agent.history import prepare_history


def call(call_id="synthetic-call"):
    return {"id": call_id, "type": "function", "function": {"name": "synthetic_tool", "arguments": "{}"}}


def test_null_and_empty_assistants_are_removed_but_real_tool_calls_and_signatures_survive():
    assistant = {"role": "assistant", "content": None, "tool_calls": [call()],
                 "gemini_content": {"role": "model", "parts": [{"thoughtSignature": "synthetic"}]}}
    result = {"role": "tool", "tool_call_id": "synthetic-call", "content": "saved"}
    attachment = {"role": "user", "content": [{"type": "image", "local_id": "synthetic-local"}]}
    history = [None, {"role": "assistant", "content": None}, attachment,
               {"role": "assistant", "content": "  "}, assistant, result,
               {"role": "assistant", "content": "", "reasoning_content": "synthetic reasoning"}]
    original = deepcopy(history)
    assert prepare_history(history) == [attachment, history[3], assistant, result, history[6]]
    assert history == original
    assert prepare_history(None) == []


@pytest.mark.parametrize("extra", [
    {"reasoning_content": "synthetic reasoning"},
    {"refusal": "synthetic refusal"},
    {"audio": {"id": "synthetic audio"}},
    {"gemini_steps": [{"type": "thought", "signature": "synthetic signature"}]},
    {"gemini_content": {"role": "model", "parts": [{"thoughtSignature": "synthetic signature"}]}},
    {"gemini_interaction": {"id": "synthetic interaction"}},
    {"responses_output": [{"type": "reasoning", "encrypted_content": "synthetic ciphertext"}]},
    {"future_provider_state": {"unknown": "preserve"}},
    {"future_counter": 0}, {"future_flag": False},
])
def test_assistant_without_visible_text_keeps_all_supplementary_data(extra):
    history = [{"role": "user", "content": "synthetic input"},
               {"role": "assistant", "content": None, **extra}]
    original = deepcopy(history)
    assert prepare_history(history) == original
    assert history == original


def test_whitespace_and_empty_text_blocks_are_not_guessed_to_be_disposable():
    history = [{"role": "assistant", "content": " \n "},
               {"role": "assistant", "content": [{"type": "text", "text": ""}]},
               {"role": "assistant", "content": [{"type": "image", "local_id": "synthetic"}]}]
    assert prepare_history(history) == history


def test_only_pure_empty_sdk_message_is_disposable():
    history = [{"role": "assistant", "content": None, "tool_calls": None,
                "reasoning_content": "", "refusal": None, "audio": None,
                "function_call": None, "annotations": []}]
    assert prepare_history(history) == []


@pytest.mark.parametrize("api_style", ["interactions", "gemini_generate", "responses"])
async def test_native_only_history_survives_guard_and_wire_conversion(api_style):
    from dialects import get_dialect
    from agent.protocol import Context, ModelConfig
    cfg = ModelConfig("synthetic", "https://model.invalid", api_style=api_style,
                      provider="openai" if api_style == "responses" else "google")
    if api_style == "interactions":
        native = {"gemini_steps": [{"type": "thought", "signature": "synthetic signature"}]}
    elif api_style == "gemini_generate":
        native = {"gemini_content": {"role": "model", "parts": [
            {"thought": True, "text": "synthetic thought", "thoughtSignature": "synthetic signature"},
        ]}}
    else:
        native = {"responses_output": [{"type": "reasoning", "id": "synthetic reasoning",
                                        "encrypted_content": "synthetic signature"}]}
    history = [{"role": "assistant", "content": None, **native},
               {"role": "user", "content": "synthetic follow-up"}]
    original = deepcopy(history)
    repaired = prepare_history(history)
    assert repaired == original
    dialect = get_dialect(api_style)
    context = Context("synthetic", [], "", "test")
    if api_style == "responses":
        wire = await dialect._to_responses_input(repaired, cfg, context)
    else:
        wire = await dialect._request(cfg, repaired, None, context)
    assert "synthetic signature" in json.dumps(wire)
    assert history == original and repaired == original


def test_missing_results_are_inserted_before_next_user_without_reexecuting_tools():
    assistant = {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]}
    history = [assistant, {"role": "tool", "tool_call_id": "a", "content": "confirmed"},
               {"role": "user", "content": "next question"}]
    repaired = prepare_history(history)
    assert repaired[1] == history[1]
    assert repaired[2]["tool_call_id"] == "b"
    assert json.loads(repaired[2]["content"])["status"] == "unknown"
    assert repaired[3] == history[2]
    assert prepare_history(repaired) == repaired


def test_null_tool_result_is_unknown_but_empty_string_is_a_valid_result():
    history = [{"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
               {"role": "tool", "tool_call_id": "a", "content": None},
               {"role": "tool", "tool_call_id": "b", "content": ""}]
    repaired = prepare_history(history)
    assert json.loads(repaired[1]["content"])["status"] == "unknown"
    assert repaired[2]["content"] == ""


@pytest.mark.parametrize("history", [
    "corrupt history", [42], [{"role": "invalid", "content": "text"}],
    [{"role": "user", "content": None}], [{"role": "user", "content": {"text": "wrong"}}],
    [{"role": "user", "content": [{"type": "text", "text": None}]}],
    [{"role": "assistant", "content": None, "tool_calls": {}}],
    [{"role": "assistant", "content": None, "tool_calls": [call("a"), call("a")]}],
    [{"role": "tool", "tool_call_id": "orphan", "content": "saved"}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {}}]}],
])
def test_ambiguous_corruption_is_rejected_and_original_is_preserved(history):
    original = deepcopy(history)
    with pytest.raises(ValueError, match="历史结构错误"):
        prepare_history(history)
    assert history == original
