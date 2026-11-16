#!/usr/bin/env python3
"""Opt-in real Interactions probe using synthetic data, never the app runtime.

Run under the explicitly authorized ADC identity. No config, database, prompts,
or conversation files are loaded; --env-file is only for reproducing ADC setup.
"""

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from dataclasses import replace

from dotenv import load_dotenv

script_directory = Path(__file__).resolve().parent
source_directory = script_directory if (script_directory / 'run_web.py').is_file() else script_directory.parent
sys.path.insert(0, str(source_directory))

from dialects.base import message_to_dict
from dialects.gemini import GeminiAPIError, GeminiClient, GeminiInteractionsDialect, _check_response
from dialects.gemini_generate import GeminiGenerateContentDialect
from agent.protocol import Context, ModelConfig


async def main(args):
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    cfg = ModelConfig(
        args.model, args.base_url, api_style='interactions', provider='google',
        auth='adc', project=args.project, location='global', stream=False,
        reasoning_effort='low', interactions_stateful=True, service_tier=args.service_tier,
    )
    context = Context('', [], '', 'synthetic-probe', session_id='synthetic-interactions-probe')
    dialect = GeminiInteractionsDialect()
    requests, identifiers, events = [], [], []
    totals = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0}

    async def trace(request):
        if request.method == 'POST' and request.url.path.endswith('/interactions'):
            requests.append(json.loads(request.content))

    async def diagnose(response):
        if not response.is_error:
            return
        await response.aread()
        # This client only submits the synthetic inputs defined in this file.
        # Never enable this diagnostic hook on an application client.
        try:
            message = response.json().get('error', {}).get('message', '')
        except ValueError:
            message = '(non-JSON error)'
        for identifier in identifiers:
            message = message.replace(identifier, '<synthetic-id>')
        for request in requests:
            identifier = request.get('previous_interaction_id')
            if identifier:
                message = message.replace(identifier, '<synthetic-id>')
        print(json.dumps({'synthetic_http_error': response.status_code, 'message': message[:800]},
                         ensure_ascii=False), flush=True)

    async def emit(event):
        events.append(event)

    print(json.dumps({'identity_uid': os.getuid(), 'model': cfg.name, 'project': cfg.project,
                      'location': cfg.location, 'service_tier': cfg.service_tier,
                      'case': args.case}, ensure_ascii=False), flush=True)
    async with GeminiClient(base_url=cfg.base_url, auth_mode='adc', project=cfg.project,
                            location=cfg.location, event_hooks={'request': [trace], 'response': [diagnose]}) as client:
        async def call(history, *, stream=False, tools=None):
            cfg.stream = stream
            events.clear()
            original = copy.deepcopy(history)
            result = await dialect.call(client, cfg, history, tools, context, emit)
            assert history == original, 'Local history was modified'
            saved = message_to_dict(result.message)
            assert saved.get('gemini_interaction', {}).get('id'), 'Missing interaction pointer'
            identifier = saved['gemini_interaction']['id']
            identifiers.append(identifier)
            assert 'gemini_steps' in saved, 'Missing complete response steps'
            usage = result.usage
            assert usage and usage.total_tokens > 0, 'Missing usage'
            totals['requests'] += 1
            totals['input_tokens'] += usage.prompt_tokens
            totals['output_tokens'] += usage.completion_tokens
            totals['cached_tokens'] += usage.cached_tokens
            print(json.dumps({'request': totals['requests'], 'stream': stream,
                              'chained': 'previous_interaction_id' in requests[-1],
                              'input_steps_sent': len(requests[-1]['input']),
                              'input_tokens': usage.prompt_tokens, 'output_tokens': usage.completion_tokens,
                              'cached_tokens': usage.cached_tokens}, ensure_ascii=False), flush=True)
            if stream and result.message.content:
                rendered = ''.join(e.data['content'] for e in events if e.type.value == 'text_delta')
                assert rendered == result.message.content, 'Stream does not match saved response'
            return saved

        try:
            if args.case == 'migration':
                # A completed legacy tool round must survive a live API switch
                # without resetting, reading, or rewriting the actual session.
                legacy = GeminiGenerateContentDialect()
                legacy_cfg = replace(cfg, api_style='gemini_generate', interactions_stateful=False)
                tools = [{'type': 'function', 'function': {
                    'name': 'synthetic_add', 'description': 'Add two integers.',
                    'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                                   'required': ['a', 'b']},
                }}]
                history = [{'role': 'user', 'content':
                            'Call synthetic_add exactly once with a=17 and b=25, then report its result.'}]
                asked = await legacy.call(client, legacy_cfg, history, tools, context, emit)
                saved = message_to_dict(asked.message)
                assert saved.get('gemini_content') and len(saved.get('tool_calls') or []) == 1
                history.append(saved)
                tc = saved['tool_calls'][0]
                assert tc['function']['name'] == 'synthetic_add'
                values = json.loads(tc['function']['arguments'])
                assert values == {'a': 17, 'b': 25}
                history.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': '42'})
                finished = await legacy.call(client, legacy_cfg, history, tools, context, emit)
                assert not finished.message.tool_calls and '42' in (finished.message.content or '')
                history.append(message_to_dict(finished.message))
                history = json.loads(json.dumps(history))
                history.append({'role': 'user', 'content': 'What was the sum just calculated? Reply with exactly 42.'})
                answer = await call(history, tools=tools)
                assert '42' in (answer.get('content') or '') and not answer.get('tool_calls')
                assert 'previous_interaction_id' not in requests[-1]
                print('[migration] PASS: completed generateContent tool history continues through Interactions', flush=True)
                return
            history = [
                {'role': 'system', 'content': 'This is a synthetic API integration test. Follow exact output requests.'},
                {'role': 'user', 'content': 'The synthetic marker color is AMBER. Remember it. Reply with exactly ACK.'},
            ]
            first = await call(history)
            assert 'ACK' in (first.get('content') or '').upper(), 'Unexpected initial answer'
            history.append(first)
            print('[text] PASS: ADC, create, local response and pointer', flush=True)
            if args.case == 'text':
                return

            with tempfile.TemporaryDirectory(prefix='tentacle-interactions-synthetic-') as directory:
                snapshot = Path(directory) / 'history.json'
                snapshot.write_text(json.dumps(history, ensure_ascii=False), encoding='utf-8')
                checkpoint = json.loads(snapshot.read_text(encoding='utf-8'))
                history = copy.deepcopy(checkpoint)
                history.append({'role': 'user', 'content':
                                'Change the synthetic marker color to TEAL. Reply with exactly TEAL.'})
                second = await call(history, stream=True)
                assert 'TEAL' in (second.get('content') or '').upper()
                assert requests[-1]['previous_interaction_id'] == first['gemini_interaction']['id']
                assert len(requests[-1]['input']) == 1
                history.append(second)
                assert len(history) == len(checkpoint) + 2 and checkpoint == history[:len(checkpoint)]
                print('[reload-and-stream] PASS: full local snapshot and incremental remote input', flush=True)

                # The same checkpoint used by /rollback: branch from the older
                # pointer rather than from the most recently completed request.
                rolled_back = copy.deepcopy(checkpoint)
                rolled_back.append({'role': 'user', 'content':
                                    'What is the synthetic marker color? Reply with exactly its name.'})
                branch = await call(rolled_back)
                assert (branch.get('content') or '').strip().upper().rstrip('.') == 'AMBER', 'Discarded branch leaked'
                assert requests[-1]['previous_interaction_id'] == first['gemini_interaction']['id']
                assert requests[-1]['previous_interaction_id'] != second['gemini_interaction']['id']
                rolled_back.append(branch)
                print('[rollback-branch] PASS: earlier ID returns AMBER, excluding discarded TEAL turn', flush=True)

                # Delete only a record created by THIS probe to force a genuine
                # missing-record response, rather than fabricating an invalid ID.
                missing_id = branch['gemini_interaction']['id']
                deletion = await client.delete('interactions/' + missing_id)
                _check_response(deletion)
                identifiers.remove(missing_id)
                rolled_back.append({'role': 'user', 'content':
                                    'Repeat the remembered synthetic marker color, with just its name.'})
                before = len(requests)
                rebuilt = await call(rolled_back, stream=True)
                assert len(requests) == before + 2, 'Missing ID did not cause exactly one full rebuild'
                assert requests[before]['previous_interaction_id'] == missing_id
                assert 'previous_interaction_id' not in requests[-1]
                assert requests[-1]['input'][0]['content'][0]['text'].startswith('The synthetic marker color is AMBER')
                assert (rebuilt.get('content') or '').strip().upper().rstrip('.') == 'AMBER'
                print('[missing-id-rebuild] PASS: real deleted ID, full local replay, AMBER preserved', flush=True)

            tools = [{'type': 'function', 'function': {
                'name': 'synthetic_add', 'description': 'Add two integers for this synthetic test.',
                'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                               'required': ['a', 'b']},
            }}]
            history = [{'role': 'user', 'content':
                        'Call synthetic_add exactly once with a=17 and b=25, then report its result.'}]
            asked = await call(history, stream=True, tools=tools)
            assert len(asked.get('tool_calls') or []) == 1, 'Expected one synthetic tool call'
            history.append(asked)
            tc = asked['tool_calls'][0]
            assert tc['function']['name'] == 'synthetic_add'
            values = json.loads(tc['function']['arguments'])
            assert values == {'a': 17, 'b': 25}
            history.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': str(values['a'] + values['b'])})
            answer = await call(history, stream=True, tools=tools)
            assert not answer.get('tool_calls') and '42' in (answer.get('content') or '')
            assert requests[-1]['previous_interaction_id'] == asked['gemini_interaction']['id']
            assert len(requests[-1]['input']) == 1 and requests[-1]['input'][0]['type'] == 'function_result'
            print('[tools] PASS: streamed call, real result continuation, complete local call/result', flush=True)
            print('ALL PASS', flush=True)
        finally:
            # Never enumerate interactions or delete an ID from production data.
            deleted, failed = 0, 0
            for identifier in reversed(identifiers):
                try:
                    response = await client.delete('interactions/' + identifier)
                    if response.status_code != 404:
                        _check_response(response)
                    deleted += 1
                except Exception:
                    failed += 1
            print(json.dumps({'cleanup_deleted': deleted, 'cleanup_failed': failed,
                              'usage': totals, 'http_create_attempts': len(requests)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--model', default='gemini-3.8-flash')
    parser.add_argument('--base-url', default='https://aiplatform.googleapis.com/v1beta1')
    parser.add_argument('--service-tier', choices=['standard', 'priority', 'flex'], default='standard')
    parser.add_argument('--case', choices=['text', 'stateful', 'migration'], default='stateful')
    parser.add_argument('--env-file')
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(main(args), timeout=300))
    except (GeminiAPIError, AssertionError, TimeoutError) as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as exc:
        # Authentication/network exception text can include provider-controlled
        # values. Keep unknown errors private until deliberately diagnosed.
        print(f'FAIL: {type(exc).__name__} (details omitted)', file=sys.stderr, flush=True)
        sys.exit(1)
