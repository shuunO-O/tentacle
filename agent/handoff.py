"""Whole-session compatibility and deterministic dialogue handoff; no model calls."""

from copy import deepcopy
import hashlib
import json
import os

from config.providers import compatibility


def model_identity(cfg):
    # Persist only opaque connection identity, never keys or authenticated URLs.
    connection = [cfg.provider_id, cfg.provider_type or compatibility(cfg.provider).compatibility,
                  cfg.base_url.rstrip('/'), cfg.auth, cfg.api_key, cfg.project, cfg.location]
    if cfg.credentials_env or cfg.gcs_base_url:
        connection.extend([cfg.credentials_env, os.getenv(cfg.credentials_env, ''), cfg.gcs_base_url])
    digest = hashlib.sha256(json.dumps(connection, sort_keys=True).encode()).hexdigest()
    return {'version': 1, 'connection': digest, 'provider': compatibility(cfg.provider).compatibility,
            'model': cfg.name, 'protocol': cfg.api_style}


def compatible_history(previous, current):
    if not isinstance(previous, dict) or previous.get('version') != current['version']:
        return False
    if previous == current:
        return True
    # Google explicitly supports replaying prior models' thought steps within
    # Interactions. This does not imply interchange with generateContent.
    return (previous.get('connection') == current['connection']
            and previous.get('provider') == current['provider'] == 'google'
            and previous.get('protocol') == current['protocol'] == 'interactions'
            and all(str(item.get('model', '')).startswith('gemini-') for item in (previous, current)))


def interactions_to_generate(history, previous, current):
    """Import a supported Interactions trace, without reusing encrypted state.

    generateContent documents this sentinel specifically for imported function
    traces. A thought.signature is NOT known to be a portable thoughtSignature.
    Return None for the entire trace if any record cannot be represented safely.
    """
    if not (isinstance(previous, dict) and previous.get('version') == current['version']
            and previous.get('connection') == current['connection']
            and previous.get('provider') == current['provider'] == 'google'
            and previous.get('model') == current['model'] and current['model'].startswith('gemini-')
            and previous.get('protocol') == 'interactions' and current['protocol'] == 'gemini_generate'):
        return None
    converted, pending = [], {}
    used_ids = set()
    for msg in history:
        role = msg.get('role')
        if role == 'user':
            if pending:
                return None
            converted.append({'role': 'user', 'content': _content(msg.get('content'), attachments=True)})
        elif role == 'assistant':
            if pending:
                return None
            calls = msg.get('tool_calls') or []
            steps = msg.get('gemini_steps')
            parts, native_calls = [], []
            if steps is not None:
                if not isinstance(steps, list):
                    return None
                for step in steps:
                    if not isinstance(step, dict):
                        return None
                    kind = step.get('type')
                    if kind == 'thought':
                        continue
                    if kind == 'model_output':
                        for block in step.get('content', []):
                            if (not isinstance(block, dict) or block.get('type') != 'text'
                                    or not isinstance(block.get('text'), str)):
                                return None
                            parts.append({'text': block['text']})
                    elif kind == 'function_call':
                        if not (isinstance(step.get('arguments'), dict) and step.get('name') and step.get('id')):
                            return None
                        native_calls.append((step['id'], step['name'], step['arguments']))
                        part = {'functionCall': {'id': step['id'], 'name': step['name'],
                                                 'args': deepcopy(step['arguments'])}}
                        if len(native_calls) == 1:
                            part['thoughtSignature'] = 'skip_thought_signature_validator'
                        parts.append(part)
                    else:
                        return None  # built-in searches/tools have no faithful Part mapping
            elif calls:
                return None
            else:
                content = msg.get('content')
                if not isinstance(content, str):
                    return None
                parts = [{'text': content}] if content else []
            canonical_calls = []
            for call in calls:
                func = call.get('function') or {}
                try:
                    args = json.loads(func.get('arguments') or '{}')
                except (TypeError, ValueError):
                    return None
                call_id = call.get('id')
                if not call_id or call_id in used_ids:
                    return None
                used_ids.add(call_id)
                canonical_calls.append((call_id, func.get('name'), args))
                pending[call_id] = func.get('name')
            if canonical_calls != native_calls:
                return None
            content = msg.get('content') or ''
            if not isinstance(content, str) or ''.join(p.get('text', '') for p in parts) != content:
                return None
            if parts:
                clean = {'role': 'assistant', 'content': content or None,
                         'gemini_content': {'role': 'model', 'parts': parts}}
                if calls:
                    clean['tool_calls'] = deepcopy(calls)
                converted.append(clean)
        elif role == 'tool':
            call_id = msg.get('tool_call_id')
            if call_id not in pending or not isinstance(msg.get('content'), str):
                return None
            pending.pop(call_id)
            converted.append({'role': 'tool', 'tool_call_id': call_id, 'content': msg['content']})
        else:
            return None
    return None if pending else converted


def _content(value, *, attachments=False):
    if isinstance(value, str):
        return value
    blocks = []
    for block in value if isinstance(value, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind in ('text', 'input_text'):
            blocks.append({'type': 'text', 'text': block.get('text', '')})
        elif attachments and kind in ('image', 'input_image', 'image_url', 'audio', 'video'):
            keys = ('type', 'local_id', 'mime', 'detail')
            clean = {k: deepcopy(block[k]) for k in keys if k in block}
            # Legacy IDs remain lookup hints only. Dialects must resolve them
            # through FileManager, never send them directly to a new provider.
            if not clean.get('local_id') and block.get('file_id'):
                clean['file_id'] = block['file_id']
            if kind == 'image_url' or block.get('image_url'):
                clean['image_url'] = deepcopy(block.get('image_url'))
            blocks.append(clean)
    return blocks


def dialogue_history(history):
    """Keep user attachments, final replies and confirmed sends; drop tool state."""
    result, pending, sent = [], {}, set()
    for msg in history:
        role = msg.get('role')
        if role == 'user':
            pending, sent = {}, set()
            content = _content(msg.get('content'), attachments=True)
            if content:
                result.append({'role': 'user', 'content': content})
        elif role == 'assistant':
            calls = msg.get('tool_calls') or []
            for call in calls:
                function = call.get('function') or {}
                if function.get('name') != 'send_message':
                    continue
                try:
                    args = json.loads(function.get('arguments') or '{}')
                except (TypeError, ValueError):
                    continue
                if isinstance(args, dict) and isinstance(args.get('text'), str) and args['text'].strip():
                    pending[call.get('id')] = args['text']
            if not calls:
                content = _content(msg.get('content'))
                if content and (not isinstance(content, str) or content not in sent):
                    result.append({'role': 'assistant', 'content': content})
        elif role == 'tool':
            text = pending.pop(msg.get('tool_call_id'), None)
            raw_content = msg.get('content')
            try:
                status = json.loads(raw_content)
            except (TypeError, ValueError):
                status = None
            confirmed = False
            if isinstance(status, dict):
                confirmed = not status.get('error') and (
                    status.get('status') == 'sent' or 'sent_chunks' in status
                )
            elif isinstance(raw_content, str) and raw_content.startswith('已发送'):
                confirmed = True
            if text and confirmed:
                result.append({'role': 'assistant', 'content': text})
                sent.add(text)
    return result
