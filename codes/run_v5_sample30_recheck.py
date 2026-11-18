#!/usr/bin/env python3
"""Reclassify the first 30 manual-review samples with the current V5 prompt.

The input sample contains old classification metadata. This runner removes only
that derived ``record.classification`` field before sending each record to the
model, so the new result cannot be contaminated by the previous labels.
"""
import concurrent.futures
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

ROOT = Path('/data/fuyuhan')
DEPS = ROOT / 'mirage-bench-agent/.deps'
if str(DEPS) not in sys.path:
    sys.path.insert(0, str(DEPS))
CLASSIFIER_PATH = Path(__file__).resolve().parent / 'agent-classify-v5.py'
SAMPLE_PATH = ROOT / 'v5-manual-review-sample-50-20261113.json'
OUT_DIR = ROOT / 'classification-results/v5-agent-trace/sample30-recheck-v5prompt-20261118'
PRIMARY = 'claude-opus-5-5'
FALLBACK = 'gpt-6.1-sol'
BASE_URL = 'https://router-link.world3.ai/api/v1'


def load_classifier():
    spec = importlib.util.spec_from_file_location('agent_classify_v5_sample30', str(CLASSIFIER_PATH))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def response_text_to_json(module, text):
    return module.response_json(text)


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    module = load_classifier()
    sample_doc = json.loads(SAMPLE_PATH.read_text(encoding='utf-8'))
    wrappers = sample_doc['records'][:30]
    records = []
    for index, wrapper in enumerate(wrappers):
        rec = deepcopy(wrapper['record'])
        # This field was added by the previous classifier and must not be sent.
        rec.pop('classification', None)
        records.append({
            'dataset': wrapper['dataset'],
            'source_record_id': wrapper['source_record_id'],
            'sample_index': wrapper['sample_index'],
            'record': rec,
        })

    api_key = os.getenv('OPENAI_API_KEY') or os.getenv('ROUTERLINK_API_KEY')
    if not api_key:
        raise SystemExit('OPENAI_API_KEY or ROUTERLINK_API_KEY is not set')
    client = module.OpenAI(api_key=api_key, base_url=os.getenv('OPENAI_BASE_URL') or BASE_URL,
                           timeout=120.0, max_retries=0)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / 'system_prompt.txt').write_text(module.SYSTEM_PROMPT, encoding='utf-8')
    (OUT_DIR / 'agent-classify-v5.snapshot.py').write_bytes(CLASSIFIER_PATH.read_bytes())
    attempts_path = OUT_DIR / 'attempts.jsonl'
    lock = threading.Lock()
    next_request = [0.0]
    limiter_lock = threading.Lock()
    interval = 0.85

    def wait_rate():
        with limiter_lock:
            t = time.monotonic()
            start = max(t, next_request[0])
            next_request[0] = start + interval
        if start > t:
            time.sleep(start - t)

    def log(event):
        with lock:
            with attempts_path.open('a', encoding='utf-8') as h:
                h.write(json.dumps(event, ensure_ascii=False) + '\n')
                h.flush()

    def classify_one(index, item):
        payload = json.dumps(item['record'], ensure_ascii=False, indent=2)
        user_prompt = (
            'Assign candidate classifications to the following benchmark record. '
            'Return only the specified JSON object.\n\n'
            f"Dataset: {item['dataset']}\nRecord ID: {item['source_record_id']}\n"
            f'Record content:\n{payload}'
        )
        last_error = None
        for model in (PRIMARY, FALLBACK):
            for attempt in range(1, 3):
                event = {
                    'timestamp_utc': now(), 'sample_position': index + 1,
                    'dataset': item['dataset'], 'source_record_id': item['source_record_id'],
                    'model': model, 'attempt': attempt,
                    'input_sha256': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
                }
                try:
                    wait_rate()
                    kwargs = {
                        'model': model,
                        'messages': [
                            {'role': 'system', 'content': module.SYSTEM_PROMPT},
                            {'role': 'user', 'content': user_prompt},
                        ],
                        'max_tokens': 600,
                    }
                    if model != PRIMARY:
                        kwargs['temperature'] = 0.0
                    response = client.chat.completions.create(**kwargs)
                    content = response.choices[0].message.content or ''
                    classification = module.validate_classification(
                        response_text_to_json(module, content)
                    )
                    event.update({'status': 'ok', 'classification': classification,
                                  'returned_model': getattr(response, 'model', None)})
                    log(event)
                    return index, classification, model, None
                except Exception as exc:
                    last_error = str(exc)
                    event.update({'status': 'error', 'error': last_error[:500]})
                    log(event)
                    if attempt < 2:
                        time.sleep(1.5 * attempt)
            # Try GPT only after Claude's attempts fail.
        return index, None, FALLBACK, last_error

    started_at = now()
    results = [None] * len(records)
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(classify_one, i, item) for i, item in enumerate(records)]
        for future in concurrent.futures.as_completed(futures):
            index, classification, model, error = future.result()
            if error:
                errors.append({'sample_position': index + 1, 'error': error})
                print('ERROR', index + 1, error, file=sys.stderr, flush=True)
            else:
                results[index] = (classification, model)
                print('OK', index + 1, records[index]['source_record_id'], model,
                      classification['failure_modes'], flush=True)

    out_records = []
    for index, (wrapper, item) in enumerate(zip(wrappers, records)):
        if results[index] is None:
            continue
        classification, model = results[index]
        out = deepcopy(wrapper)
        out['record'] = deepcopy(item['record'])
        out['record']['classification'] = classification
        out['classification_failure_modes'] = classification['failure_modes']
        out['classification_model'] = model
        out_records.append(out)

    output = {
        'sampling_scope': 'First 30 records from v5-manual-review-sample-50-20261113.json',
        'source_sample_file': str(SAMPLE_PATH),
        'source_sample_sha256': sha(SAMPLE_PATH),
        'classifier_file': str(CLASSIFIER_PATH),
        'classifier_sha256': sha(CLASSIFIER_PATH),
        'system_prompt_sha256': hashlib.sha256(module.SYSTEM_PROMPT.encode('utf-8')).hexdigest(),
        'primary_model': PRIMARY, 'fallback_model': FALLBACK,
        'input_removed_derived_fields': ['record.classification'],
        'started_at_utc': started_at, 'finished_at_utc': now(),
        'record_count': len(out_records), 'error_count': len(errors), 'errors': errors,
        'records': out_records,
    }
    (OUT_DIR / 'sample30_classified.json').write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (OUT_DIR / 'run_manifest.json').write_text(
        json.dumps({k: v for k, v in output.items() if k != 'records'}, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    if errors:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
