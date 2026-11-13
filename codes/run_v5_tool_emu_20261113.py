#!/usr/bin/env python3
"""Run the current V4 classifier on the full Agent-SafetyBench and ToolEmu data.

The classifier module owns the prompt, input preparation, JSON parsing, and label
validation. This file only supplies the input records, dispatches Claude first
and GPT-6.1-SOL as a fallback, and writes resumable source-shaped outputs.
"""

import argparse
import concurrent.futures
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path('/data/fuyuhan')
DEPS = ROOT / 'mirage-bench-agent/.deps'
if str(DEPS) not in sys.path:
    sys.path.insert(0, str(DEPS))

CLASSIFIER_PATH = ROOT / 'codes/agent-classify-v5-toolemu-20261113.py'
AGENT_SOURCE = ROOT / 'agentsafetybench-data/released_data.json'
AGENT_TRACE = ROOT / 'agent-safetybench-router-results/gpt-4o/gen_res.json'
TOOL_SOURCE = ROOT / 'toolemu-data/all_cases.json'
DEFAULT_OUT = ROOT / (
    'classification-results/v5-agent-trace/'
    'toolemu-potential-h-20261113'
)
PRIMARY_MODEL = 'claude-opus-5-5'
FALLBACK_MODEL = 'gpt-6.1-sol'


class RequestRateLimiter:
    """Serialize request starts so the shared router rate limit is respected."""

    def __init__(self, interval: float):
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now_value = time.monotonic()
            start = max(now_value, self._next)
            self._next = start + self.interval
        delay = start - now_value
        if delay > 0:
            time.sleep(delay)


def load_classifier():
    spec = importlib.util.spec_from_file_location('toolemu_agent_classify_v5_20261113', str(CLASSIFIER_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError('无法加载当前分类器')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def safe_error(exc: Exception) -> str:
    parts = [type(exc).__name__]
    status = getattr(exc, 'status_code', None)
    if status is not None:
        parts.append('HTTP {}'.format(status))
    text = str(exc).replace('\n', ' ').strip()
    if text:
        parts.append(text[:240])
    return ': '.join(parts)


def record_id(dataset: str, record: Dict[str, Any], index: int) -> str:
    key = 'id' if dataset == 'agentsafetybench' else 'name'
    return str(record.get(key, index))


def model_request(
    classifier,
    client,
    model: str,
    dataset: str,
    index: int,
    record: Dict[str, Any],
    args,
    log_attempt,
    limiter: RequestRateLimiter,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    if args.max_record_chars and len(payload) > args.max_record_chars:
        payload = payload[:args.max_record_chars] + '\n[Record truncated]'
    user_prompt = (
        'Assign candidate classifications to the following benchmark record. '
        'Return only the specified JSON object.\n\n'
        'Dataset: {}\nRecord ID: {}\nRecord content:\n{}'.format(
            dataset, record_id(dataset, record, index), payload
        )
    )
    last_error = None
    for attempt in range(1, args.max_retries + 2):
        event = {
            'timestamp_utc': now(),
            'dataset': dataset,
            'source_index': index,
            'record_id': record_id(dataset, record, index),
            'requested_model': model,
            'attempt': attempt,
            'record_sha256': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
        }
        try:
            limiter.wait()
            options = {
                'model': model,
                'messages': [
                    {'role': 'system', 'content': classifier.SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_prompt},
                ],
                'max_tokens': args.max_tokens,
            }
            # The router accepts Claude with a system-role message and no
            # temperature parameter. GPT uses the normal OpenAI-compatible call.
            if model != PRIMARY_MODEL:
                options['temperature'] = args.temperature
            response = client.chat.completions.create(**options)
            if not response.choices:
                raise ValueError('No response choices')
            choice = response.choices[0]
            content = choice.message.content or ''
            classification = classifier.validate_classification(
                classifier.response_json(content)
            )
            event.update(
                status='ok',
                finish_reason=choice.finish_reason,
                refusal=getattr(choice.message, 'refusal', None),
                classification=classification,
            )
            if getattr(response, 'id', None):
                event['response_id'] = response.id
            if getattr(response, 'model', None):
                event['returned_model'] = response.model
            usage = getattr(response, 'usage', None)
            if usage is not None and hasattr(usage, 'model_dump'):
                event['usage'] = usage.model_dump()
            log_attempt(event)
            return classification, {
                'status': 'classified',
                'classification_model': model,
                'attempts': attempt,
            }
        except Exception as exc:
            last_error = exc
            event.update(status='error', error=safe_error(exc))
            log_attempt(event)
            if attempt <= args.max_retries:
                time.sleep(args.retry_delay * (2 ** (attempt - 1)))
    raise RuntimeError(safe_error(last_error or RuntimeError('unknown error')))


def prepare_records(classifier, dataset: str) -> Tuple[List[Dict[str, Any]], Path, Optional[Path]]:
    if dataset == 'agentsafetybench':
        source = classifier.load_source(str(AGENT_SOURCE))
        trace = classifier.load_source(str(AGENT_TRACE))
        records = classifier.build_agentsafety_trace_records(source, trace)
        return records, AGENT_SOURCE, AGENT_TRACE
    records = classifier.load_source(str(TOOL_SOURCE))
    return records, TOOL_SOURCE, None


def load_or_initialize(output: Path, records: List[Dict[str, Any]], classifier) -> Tuple[List[Dict[str, Any]], int]:
    if not output.exists():
        return [deepcopy(x) for x in records], 0
    old = json.loads(output.read_text(encoding='utf-8'))
    if not isinstance(old, list) or len(old) != len(records):
        raise ValueError('已有输出与本次输入数量不一致：{}'.format(output))
    result = [deepcopy(x) for x in records]
    completed = 0
    for index, row in enumerate(old):
        if not isinstance(row, dict) or record_id('agentsafetybench' if 'agent_execution' in records[index] else 'toolemu', row, index) != record_id('agentsafetybench' if 'agent_execution' in records[index] else 'toolemu', records[index], index):
            continue
        stripped = {k: v for k, v in row.items() if k != 'classification'}
        if stripped != records[index]:
            raise ValueError('已有输出的原始字段发生变化：{} index {}'.format(output, index))
        if isinstance(row.get('classification'), dict):
            result[index]['classification'] = classifier.validate_classification(row['classification'])
            completed += 1
    return result, completed


def run_dataset(classifier, client, dataset: str, records: List[Dict[str, Any]], source_path: Path, trace_path: Optional[Path], out: Path, args, manifest: Dict[str, Any]) -> None:
    output = out / '{}_classified.json'.format(dataset)
    result, completed = load_or_initialize(output, records, classifier)
    provenance_path = out / '{}_provenance.json'.format(dataset)
    provenance: List[Dict[str, Any]] = [
        {'source_index': i, 'record_id': record_id(dataset, records[i], i),
         'status': 'classified' if 'classification' in result[i] else 'pending',
         'classification_model': None}
        for i in range(len(records))
    ]
    if provenance_path.exists():
        old_provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
        if isinstance(old_provenance, list) and len(old_provenance) == len(records):
            provenance = old_provenance
    lock = threading.Lock()
    limiter = RequestRateLimiter(args.request_interval)
    attempts_path = out / '{}_attempts.jsonl'.format(dataset)

    def log_attempt(event):
        with lock:
            with attempts_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                handle.flush()

    pending = [i for i, row in enumerate(result) if 'classification' not in row]
    print('{}: {} records, {} already complete, {} pending'.format(dataset, len(records), completed, len(pending)), flush=True)
    errors: Dict[int, str] = {}
    completed_queue = queue.Queue()

    def request(index: int):
        primary_error = None
        try:
            classification, info = model_request(
                classifier, client, PRIMARY_MODEL, dataset, index, records[index],
                args, log_attempt, limiter
            )
            completed_queue.put((index, classification, info, None))
            return
        except Exception as exc:
            primary_error = safe_error(exc)
        try:
            classification, info = model_request(
                classifier, client, FALLBACK_MODEL, dataset, index, records[index],
                args, log_attempt, limiter
            )
            info['primary_error'] = primary_error
            completed_queue.put((index, classification, info, None))
            return
        except Exception as exc:
            completed_queue.put((index, None, {'primary_error': primary_error}, safe_error(exc)))

    if pending:
        worker_count = min(args.workers, len(pending))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(request, index) for index in pending]
            for number in range(1, len(pending) + 1):
                index, classification, info, error = completed_queue.get()
                if error is not None:
                    errors[index] = error
                    print('ERROR {} {}: {}'.format(dataset, record_id(dataset, records[index], index), error), file=sys.stderr, flush=True)
                else:
                    result[index]['classification'] = classification
                    provenance[index].update(info, status='classified')
                    completed += 1
                    print('OK {} {}/{}: {} ({})'.format(dataset, number, len(pending), record_id(dataset, records[index], index), info['classification_model']), flush=True)
                if number % args.flush_every == 0 or number == len(pending):
                    save_json(output, result)
                    save_json(provenance_path, provenance)
    save_json(output, result)
    save_json(provenance_path, provenance)
    counts = Counter(code for row in result for code in row.get('classification', {}).get('failure_modes', []))
    empty = sum(1 for row in result if not row.get('classification', {}).get('failure_modes'))
    manifest['datasets'][dataset] = {
        'source_file': str(source_path),
        'source_sha256': sha(source_path),
        'trace_file': str(trace_path) if trace_path else None,
        'trace_sha256': sha(trace_path) if trace_path else None,
        'output_file': str(output),
        'provenance_file': str(provenance_path),
        'record_count': len(records),
        'classified_count': sum(1 for row in result if 'classification' in row),
        'pending_count': len(errors),
        'empty_label_count': empty,
        'failure_mode_counts': dict(counts),
        'error_ids': [record_id(dataset, records[i], i) for i in sorted(errors)],
    }
    save_json(out / 'classification_manifest.json', manifest)
    if errors:
        raise RuntimeError('{} still has {} records without valid classifications'.format(dataset, len(errors)))
    if empty:
        raise RuntimeError('{} contains {} empty label arrays'.format(dataset, empty))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default=str(DEFAULT_OUT))
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--retry-delay', type=float, default=5.0)
    parser.add_argument('--request-interval', type=float, default=0.45,
                        help='每次 API 请求的最小启动间隔；默认 0.45 秒，低于路由每分钟 200 次限制')
    parser.add_argument('--flush-every', type=int, default=10,
                        help='每完成多少条后写入一次输出')
    parser.add_argument('--max-tokens', type=int, default=600)
    parser.add_argument('--temperature', type=float, default=0.0)
    parser.add_argument('--max-record-chars', type=int, default=0)
    parser.add_argument('--dataset', choices=('all', 'agentsafetybench', 'toolemu'), default='all')
    args = parser.parse_args()
    if args.workers < 1 or args.max_retries < 0 or args.flush_every < 1:
        parser.error('workers must be positive and max-retries must be nonnegative')
    classifier = load_classifier()
    api_key = os.getenv('OPENAI_API_KEY') or os.getenv('ROUTERLINK_API_KEY')
    if not api_key:
        raise SystemExit('请设置 OPENAI_API_KEY 或 ROUTERLINK_API_KEY；脚本不会从文件读取密钥。')
    base_url = os.getenv('OPENAI_BASE_URL') or 'https://router-link.world3.ai/api/v1'
    client = classifier.OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=0)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'agent-classify-v5.snapshot.py').write_bytes(CLASSIFIER_PATH.read_bytes())
    (out / 'system_prompt.txt').write_text(classifier.SYSTEM_PROMPT, encoding='utf-8')
    manifest = {
        'version': 'v5-toolemu-potential-h-rerun-20261113',
        'status': 'running',
        'started_at_utc': now(),
        'finished_at_utc': None,
        'classifier_file': str(CLASSIFIER_PATH),
        'classifier_sha256': sha(CLASSIFIER_PATH),
        'system_prompt_sha256': hashlib.sha256(classifier.SYSTEM_PROMPT.encode('utf-8')).hexdigest(),
        'runner_file': str(Path(__file__)),
        'runner_sha256': sha(Path(__file__)),
        'base_url': base_url,
        'primary_model': PRIMARY_MODEL,
        'fallback_model': FALLBACK_MODEL,
        'workers': args.workers,
        'max_retries': args.max_retries,
        'max_tokens': args.max_tokens,
        'temperature_for_gpt': args.temperature,
        'temperature_for_claude': None,
        'request_interval': args.request_interval,
        'max_record_chars': args.max_record_chars,
        'datasets': {},
    }
    save_json(out / 'classification_manifest.json', manifest)
    datasets = [args.dataset] if args.dataset != 'all' else ['agentsafetybench', 'toolemu']
    try:
        for dataset in datasets:
            records, source_path, trace_path = prepare_records(classifier, dataset)
            run_dataset(classifier, client, dataset, records, source_path, trace_path, out, args, manifest)
        manifest['status'] = 'complete'
        manifest['finished_at_utc'] = now()
        save_json(out / 'classification_manifest.json', manifest)
        print('分类完成：{}'.format(out), flush=True)
        return 0
    except Exception as exc:
        manifest['status'] = 'partial'
        manifest['finished_at_utc'] = now()
        manifest['error'] = safe_error(exc)
        save_json(out / 'classification_manifest.json', manifest)
        print('分类未完整完成：{}'.format(safe_error(exc)), file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
