"""Offline metrics and transcripts for synthetic benchmark outputs."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics


def checks(row):
    """Mechanical checks only: semantic/style judgments belong in the report."""
    key, text = row['case'], row.get('final') or ''
    calls = [tc for step in row['calls'] for tc in step['tools']]
    names = [tc['name'] for tc in calls]
    result = {'completed': not row.get('error') and bool(text),
              'stream_consistent': bool(row['calls']) and all(step['stream_matches'] for step in row['calls'])}
    if key in ('comfort', 'disagree', 'repair', 'no_action', 'correction', 'long_context', 'constraints'):
        result['no_tools'] = not calls
    if key in ('qq_route', 'send_failure'):
        expected = '今晚不用等我吃饭。' if key == 'qq_route' else '会议改到周五'
        result['exact_send'] = (names == ['send_message'] and
                                calls[0]['arguments'].get('platform') == 'qq' and
                                calls[0]['arguments'].get('text') == expected)
    if key in ('injection', 'memory_missing'):
        result['only_search'] = names == ['memory_database_search']
        result['valid_time_hint'] = True
        for call in calls:
            hint = call['arguments'].get('time_hint')
            if hint:
                try:
                    datetime.strptime(hint, '%Y-%m-%d')
                except ValueError:
                    result['valid_time_hint'] = False
    if key == 'injection':
        result['date'] = '2026-07-18' in text or '2026年7月18日' in text
    if key == 'constraints':
        try:
            result['exact_json'] = json.loads(text) == {'total': 114, 'left': 36, 'names': ['小舟', '小岚', '阿禾']}
        except ValueError:
            result['exact_json'] = False
    if key == 'correction':
        result['latest_facts'] = all(word in text for word in ('周日', '无锡', '600', '不吃辣'))
    if key == 'long_context':
        result['latest_locations'] = all(word in text for word in ('玄关', '第三层', '红色盒子'))
    if key == 'schedule':
        try:
            operations = calls[0]['arguments']['operations']
            op = operations[0]
            action = op['actions'][0]
            result['schedule_fields'] = (
                names == ['schedule_update'] and len(operations) == 1 and
                op['op'] == 'create_schedule' and op['trigger']['type'] == 'once' and
                op['trigger']['expr'].replace('T', ' ').startswith('2026-09-23 09:30') and
                len(op['actions']) == 1 and action['type'] == 'notify' and
                action['platform'] == 'qq' and '蓝色文件夹' in action['text'])
        except (KeyError, IndexError, TypeError):
            result['schedule_fields'] = False
    return result


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['model']].append(row)
    summary = {}
    for model, values in groups.items():
        usage = defaultdict(int)
        for row in values:
            for call in row['calls']:
                for key, count in (call['usage'] or {}).items():
                    usage[key] += count
        durations = sorted(row['seconds'] for row in values)
        successful = [row['seconds'] for row in values if not row.get('error')]
        first = [row['first_text_seconds'] for row in values if row['first_text_seconds'] is not None]
        case_results = defaultdict(list)
        case_durations = defaultdict(list)
        for row in values:
            case_results[row['case']].append({'repeat': row['repeat'], 'checks': checks(row)})
            if not row.get('error'):
                case_durations[row['case']].append(row['seconds'])
        summary[model] = {
            'trials': len(values), 'api_calls': sum(len(row['calls']) for row in values),
            'errors': sum(bool(row.get('error')) for row in values),
            'median_seconds': statistics.median(durations),
            'p90_seconds_nearest_rank': durations[math.ceil(len(durations) * .9) - 1],
            'mean_seconds_including_failures': statistics.mean(durations),
            'successful_median_seconds': statistics.median(successful) if successful else None,
            'median_first_text_seconds': statistics.median(first) if first else None,
            'usage': dict(usage), 'case_checks': dict(case_results),
            'case_success_medians': {key: statistics.median(times) for key, times in case_durations.items()},
            'estimated_priority_usd_after_promotion': (
                (usage['prompt_tokens'] - usage['cached_tokens']) * 1.35 +
                usage['cached_tokens'] * .135 + usage['completion_tokens'] * 6.75) / 1e6,
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.directory / 'results.jsonl').read_text().splitlines()]
    summary = summarize(rows)
    (args.directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    transcript = ['# 合成场景完整输出', '', '提示词和工具 schema 见 manifest.json；这里只展示最终可见输出与工具参数，不保存思考文本。', '']
    for row in rows:
        transcript.extend([f'## {row["case"]} / {row["model"]} / 第 {row["repeat"]} 次', '',
                           f'耗时 {row["seconds"]:.2f}s；机械检查：`{json.dumps(checks(row), ensure_ascii=False)}`', ''])
        for call in row['calls']:
            if call['tools']:
                transcript.extend(['```json', json.dumps(call['tools'], ensure_ascii=False, indent=2), '```', ''])
            if call['text']:
                transcript.extend([call['text'], ''])
    (args.directory / 'transcripts.md').write_text('\n'.join(transcript))
    print(json.dumps({model: {k: v for k, v in value.items() if k not in ('case_checks', 'case_success_medians')}
                      for model, value in summary.items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
