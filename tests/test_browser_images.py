"""Bundled browser plugin screenshot tool results use each Gemini wire format; no live services."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import attachments.manager as attachments
from agent.commands import CommandRegistry
from agent.protocol import Context, ModelConfig
from dialects import get_dialect
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from runtime.tool_registry import ToolRegistry


def _load_browser_plugin_module():
    path = Path(__file__).resolve().parents[1] / "plugins" / "bundled" / "browser" / "plugin.py"
    spec = importlib.util.spec_from_file_location("test_bundled_browser_plugin_img", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def screenshot(monkeypatch, tmp_path):
    monkeypatch.setattr(attachments.FileManager, '_instance', None)
    manager = attachments.FileManager(str(tmp_path / 'files.db'))
    manager.configure({'defer_uploads': True, 'uploader': ''})
    image = tmp_path / 'browser screenshot.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic-screenshot')
    plugin_mod = _load_browser_plugin_module()
    monkeypatch.setattr(plugin_mod, 'run_browser_command', lambda args, cfg=None: {
        '__browser_shot__': f'Screenshot saved to {image}'})
    return manager, image, plugin_mod


@pytest.mark.parametrize('style', ['interactions', 'gemini_generate'])
@pytest.mark.parametrize('transport', ['inline', 'remote', 'missing'])
@pytest.mark.parametrize('vision', [True, False])
async def test_screenshot_tool_result_wire_format(screenshot, style, transport, vision):
    manager, image, plugin_mod = screenshot
    blocks = await plugin_mod.execute_browser(
        {'command': 'screenshot', 'screenshot_as_image': True},
        {},
        upload_image_fn=manager.upload_image,
    )
    assert blocks[0]['mime'] == 'image/png'
    assert blocks[0]['local_id']
    assert 'file_id' not in blocks[0]
    record = await manager.get_file(blocks[0]['local_id'])
    assert record['filename'] == image.name

    blocks[0]['file_id'] = 'deepseek-only-reference'
    blocks.insert(0, {'type': 'input_text', 'text': 'Screenshot captured'})
    saved = deepcopy(blocks)
    cfg = ModelConfig('gemini-3.8-flash', 'https://model.test/', provider='google',
                      api_style=style, auth='adc', vision=vision, stream=False)
    remote = AsyncMock(return_value='gs://test-bucket/screenshot' if transport == 'remote' else None)
    inline = AsyncMock(side_effect=manager.image_block_to_data_url) if transport != 'missing' else AsyncMock()
    if transport == 'missing':
        inline.return_value = None
    context = Context('', [], '', 'test', image_resolver=inline, image_file_resolver=remote)
    messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'shot', 'function': {'name': 'p_browser__browser', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'shot', 'content': blocks},
    ]
    body = await get_dialect(style)._request(cfg, messages, None, context)
    wire = json.dumps(body)
    assert 'deepseek-only-reference' not in wire
    assert 'local_id' not in wire
    assert blocks == saved

    if style == 'interactions':
        result = body['input'][-1]
        assert result['call_id'] == 'shot'
        assert result['name'] == 'p_browser__browser'
        assert result['result'][0] == {'type': 'text', 'text': 'Screenshot captured'}
        picture = result['result'][1]
        if not vision or transport == 'missing':
            assert picture == {'type': 'text', 'text': '[图片]' if not vision else '[图片不可用]'}
        elif transport == 'remote':
            assert picture == {'type': 'image', 'uri': 'gs://test-bucket/screenshot', 'mime_type': 'image/png'}
        else:
            assert picture == {'type': 'image', 'mime_type': 'image/png',
                               'data': (await manager.image_block_to_data_url(blocks[1])).split(',', 1)[1]}
    else:
        result = body['contents'][-1]['parts'][0]['functionResponse']
        assert result['id'] == 'shot'
        assert result['name'] == 'p_browser__browser'
        if not vision or transport == 'missing':
            assert 'parts' not in result
            assert result['response']['output'] == 'Screenshot captured\n' + (
                '[图片]' if not vision else '[图片不可用]')
        else:
            assert result['response'] == {'output': 'Screenshot captured'}
            picture = result['parts'][0]
            if transport == 'remote':
                assert picture == {'fileData': {'mimeType': 'image/png', 'fileUri': 'gs://test-bucket/screenshot'}}
            else:
                assert picture == {'inlineData': {'mimeType': 'image/png',
                                   'data': (await manager.image_block_to_data_url(blocks[1])).split(',', 1)[1]}}
    if not vision:
        remote.assert_not_awaited()
        inline.assert_not_awaited()


@pytest.mark.parametrize('style,stateful', [
    ('interactions', False), ('interactions', True), ('gemini_generate', False),
])
async def test_browser_screenshot_round_completes(screenshot, monkeypatch, tmp_path, style, stateful):
    import agent.loop as agent_loop

    manager, image, _ = screenshot
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    bundled = Path(__file__).resolve().parents[1] / "plugins" / "bundled"
    pm = PluginManager(
        plugins_dir=tmp_path / "user-plugins",
        bundled_dir=bundled,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        services={"file_manager": manager},
    )
    await pm.startup({"plugins": {"items": {"browser": {"enabled": True, "trusted": True}}, "mcp": {}}})
    plugin_instance_mod = pm._active_runtimes["browser"]["module"]
    monkeypatch.setattr(plugin_instance_mod, 'run_browser_command', lambda args, cfg=None: {
        '__browser_shot__': f'Screenshot saved to {image}'})

    try:
        cfg = ModelConfig('gemini-3.8-flash', 'https://model.test/', provider='google',
                          api_style=style, stream=False, vision=True, interactions_stateful=stateful)
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if len(calls) == 2:
                assert 'local_id' not in request.content.decode()
                if style == 'interactions':
                    result = body['input'][-1]
                    assert result['call_id'] == 'shot'
                    assert result['result'][0]['mime_type'] == 'image/png'
                    assert result['result'][0]['data']
                    if stateful:
                        assert body['previous_interaction_id'] == 'turn-1'
                        assert len(body['input']) == 1
                else:
                    result = body['contents'][-1]['parts'][0]['functionResponse']
                    assert result['id'] == 'shot'
                    assert result['parts'][0]['inlineData']['mimeType'] == 'image/png'
            first = len(calls) == 1
            args = {'command': 'screenshot', 'screenshot_as_image': True}
            if style == 'interactions':
                steps = [{'type': 'function_call', 'id': 'shot', 'name': 'p_browser__browser', 'arguments': args}] if first else [
                    {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Screenshot received'}]}]
                return httpx.Response(200, json={'id': f'turn-{len(calls)}', 'steps': steps,
                                                'status': 'requires_action' if first else 'completed'})
            parts = [{'functionCall': {'id': 'shot', 'name': 'p_browser__browser', 'args': args},
                      'thoughtSignature': 'test-signature'}] if first else [{'text': 'Screenshot received'}]
            return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': parts}}]})

        executor = AsyncMock()
        executor.question = None
        async def execute(name, args):
            return await reg._tools[name]["func"](args)

        executor.execute.side_effect = execute
        async with httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(agent_loop, 'GeminiClient', lambda **kwargs: client)
            loop = agent_loop.AgentLoop(cfg, AsyncMock(), tool_executor=executor, tool_definitions=[])
            result = await loop.run([{'role': 'user', 'content': 'Take a screenshot'}], Context(
                '', [], '', 'test', image_resolver=manager.image_block_to_data_url))
        assert result.stop_reason == 'complete'
        assert result.final_answer == 'Screenshot received'
        assert len(calls) == 2
        executor.execute.assert_awaited_once()
        assert result.full_messages[2]['content'][0]['type'] == 'image'
    finally:
        await pm.shutdown()
