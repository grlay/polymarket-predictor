"""Provider-neutral chat-completions client; explicit synthetic backend for tests only.

Real backend: any /chat/completions JSON API. For Gemini use LLM_API_BASE=
https://generativelanguage.googleapis.com/v1beta/openai and LLM_MODEL=gemini-3.8-flash (see .env.example).
Transient failures (HTTP 429/5xx, network, timeouts) and malformed answers are retried within bounds; every HTTP
request counts toward max_calls_per_run. Only validated answers are cached.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from src.common import canonical, digest, probability, read_json, utcnow, write_json, load_env

TRANSIENT = (429, 500, 502, 503, 504)


class BudgetExhausted(RuntimeError):
    """The configured request budget is spent. Raised once per call; the orchestrator stops cleanly."""


def validate_output(result, evidence_ids):
    if not isinstance(result, dict):
        raise ValueError('LLM output must be a JSON object')
    if 'probability' not in result:
        raise ValueError('Missing probability')
    p = probability(result['probability'])
    cited = result.get('evidence_ids')
    if not isinstance(cited, list) or not all(isinstance(x, str) for x in cited):
        raise ValueError('evidence_ids must be a string list')
    if not set(cited).issubset(set(evidence_ids)):
        raise ValueError('Invented or unavailable citation')
    if not isinstance(result.get('rationale'), str) or not result['rationale'].strip():
        raise ValueError('Missing rationale')
    if not isinstance(result.get('flags'), list) or not all(isinstance(x, str) for x in result['flags']):
        raise ValueError('Invalid flags')
    return {**result, 'probability': p}


class LLMClient:
    def __init__(self, cfg):
        load_env()
        self.settings = cfg['llm']
        self.provider = self.settings['provider']
        self.model = self.settings.get('model') or os.getenv('LLM_MODEL')
        self.base = os.getenv('LLM_API_BASE', '').rstrip('/')
        self.key = os.getenv('LLM_API_KEY', '')
        self.calls = 0
        self.cache_hits = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.response_models = set()
        self.lock = threading.Lock()
        self.cache_dir = Path(cfg['data_dir'])/'cache'/'llm'
        if self.provider == 'mock':
            if cfg['mode'] != 'synthetic' or self.model != 'synthetic-test-backend':
                raise ValueError('Mock backend only allowed in explicitly synthetic experiments')
        elif self.provider == 'chat_completions':
            if not self.key or not self.model or not self.base.startswith('https://') or '.example' in self.base:
                raise ValueError('Set LLM_API_KEY, LLM_API_BASE and LLM_MODEL in .env before real runs')
        else:
            raise ValueError('Unsupported LLM provider')

    # -- request budget -----------------------------------------------------------------------
    def spend(self):
        with self.lock:
            if self.calls >= self.settings['max_calls_per_run']:
                raise BudgetExhausted('Configured LLM call budget exhausted; increase explicitly or resume')
            self.calls += 1

    @property
    def exhausted(self):
        return self.calls >= self.settings['max_calls_per_run']

    # -- transport -----------------------------------------------------------------------------
    def post(self, request):
        """POST one request, retrying transient failures with backoff. Every attempt spends budget."""
        retries = self.settings.get('retries', 3)
        wait = self.settings.get('retry_wait_seconds', 2)
        for attempt in range(retries + 1):
            self.spend()
            req = urllib.request.Request(self.base+'/chat/completions', data=canonical(request).encode(),
                headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}, method='POST')
            delay = min(wait * 2 ** attempt, 60)
            try:
                with urllib.request.urlopen(req, timeout=self.settings['timeout_seconds']) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode('utf-8', 'replace') if hasattr(exc, 'read') else ''
                if exc.code not in TRANSIENT or attempt == retries:
                    raise RuntimeError(f'LLM HTTP {exc.code}: {detail}') from None
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                if retry_after and str(retry_after).isdigit():
                    delay = min(int(retry_after), 120)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == retries:
                    raise RuntimeError(f'LLM network failure: {type(exc).__name__}') from None
            time.sleep(delay)
        raise RuntimeError('Exhausted LLM retries')

    def answer(self, request, evidence_ids):
        """One validated answer, re-asking a bounded number of times when the reply is unusable."""
        last = None
        for _ in range(self.settings.get('format_retries', 2) + 1):
            payload = self.post(request)
            usage = payload.get('usage') or {}
            with self.lock:
                self.prompt_tokens += int(usage.get('prompt_tokens') or 0)
                self.completion_tokens += int(usage.get('completion_tokens') or 0)
                self.response_models.add(payload.get('model', self.model))
            try:
                choice = payload['choices'][0]
                content = choice['message']['content']
                finish = choice.get('finish_reason')
            except (KeyError, IndexError, TypeError):
                last = ValueError('Unexpected LLM response shape')
                continue
            if finish == 'length':
                last = ValueError('Output truncated at max_tokens (thinking models count reasoning against it); '
                                  'raise llm.max_output_tokens')
                continue
            if not isinstance(content, str) or not content.strip():
                last = ValueError('Empty LLM content')
                continue
            # Permit a JSON fence, but never silently extract arbitrary substrings.
            if content.strip().startswith('```'):
                content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
            try:
                result = validate_output(json.loads(content), evidence_ids)
            except ValueError as exc:  # includes JSONDecodeError
                last = exc
                continue
            return result, usage, payload.get('model', self.model)
        raise ValueError(f'Unusable LLM output after retries: {last}')

    # -- public --------------------------------------------------------------------------------
    def predict(self, prompt, context, role, replicate=0, temperature=None):
        """temperature overrides the configured value for this call (used by sampled repeated-call baselines)."""
        request = dict(model=self.model, max_tokens=self.settings['max_output_tokens'],
                       messages=[{'role': 'system', 'content': prompt},
                                 {'role': 'user', 'content': canonical(context)}])
        temperature = self.settings.get('temperature') if temperature is None else temperature
        if temperature is not None:
            request['temperature'] = temperature
        if self.settings.get('reasoning_effort'):
            request['reasoning_effort'] = self.settings['reasoning_effort']
        evidence_ids = [d['document_id'] for d in context['documents']]
        # Replicates deliberately bypass one another's cache even at temperature=0.
        key = digest({'provider': self.provider, 'base': self.base, 'request': request,
                      'role': role, 'replicate': replicate})
        cache_file = self.cache_dir/(key+'.json')
        if cache_file.exists():
            cached = read_json(cache_file)
            if cached['request_hash'] != key:
                raise ValueError('Cache identity mismatch')
            with self.lock:
                self.cache_hits += 1
            result = validate_output(cached['result'], evidence_ids)
        else:
            if self.provider == 'mock':
                self.spend()
                result = validate_output(self._mock(context, role, replicate), evidence_ids)
                usage, response_model = {'synthetic': True}, self.model
            else:
                result, usage, response_model = self.answer(request, evidence_ids)
            write_json(cache_file, {'request_hash': key, 'request': request, 'result': result,
                                    'generated_at': utcnow(), 'response_model': response_model, 'usage': usage})
        return {**result, 'cache_key': key, 'backend': self.provider}

    @staticmethod
    def _mock(context, role, replicate):
        """No labels or market price are accessible. Synthetic text contains noisy signals."""
        docs = context['documents']
        signals = []
        for doc in docs:
            match = re.search(r'SYNTHETIC_SIGNAL=([0-9]+(?:\.[0-9]+)?)', doc['body'])
            if match:
                signals.append(float(match.group(1)))
        center = float(np.mean(signals)) if signals else 0.5
        jitter = (int(digest([context['question'], role, replicate])[:8], 16)/0xffffffff-0.5)*0.10
        if role == 'verifier' and context.get('drafts'):
            center = 0.5*center+0.5*np.mean([d['probability'] for d in context['drafts']])
        return dict(probability=float(np.clip(center+jitter, .02, .98)),
                    evidence_ids=[d['document_id'] for d in docs[:2]],
                    rationale='SYNTHETIC TEST OUTPUT: deterministic noisy text-signal calculation; not LLM research evidence.',
                    flags=['synthetic_only'])
