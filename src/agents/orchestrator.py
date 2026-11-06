"""Frozen evidence, independent first-stage agents, bounded verification, immutable logging."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.agents import news_agent, base_rate_agent, verifier
from src.agents.llm_client import LLMClient, BudgetExhausted
from src.baselines import llm_no_retrieval, single_agent_rag, single_agent_kx
from src.common import (canonical, digest, iso, timestamp, utcnow, write_json, forecast_fingerprint, forecast_identity,
                        runtime_info)
from src.dataset.leakage_checks import check_evidence
from src.db.loaders import insert, event
from src.retrieval.temporal_retriever import retrieve
from src.retrieval.relevance import require_relevant_evidence

SYSTEMS = ['news_agent', 'base_rate_agent', 'llm_no_retrieval', 'single_agent_rag',
           'single_agent_kx', 'multi_unverified', 'multi_verified', 'multi_no_social']

# Expected, loggable failures. Programming errors (TypeError, AttributeError, ...) are NOT in this list on
# purpose: they must stop the run loudly instead of being recorded as "the forecast failed".
EXPECTED_FAILURES = (ValueError, RuntimeError, OSError)


def utc(value):
    """Readable UTC timestamp for the model; raw epoch integers are easy to misread."""
    return iso(int(value)).replace('+00:00', 'Z')


def context_for(snapshot, docs):
    # Deliberate allowlist: neither labels nor market prices reach any independent agent.
    cutoff = int(snapshot['cutoff'])
    context = dict(question=snapshot['question'], rules=snapshot['rules'], cutoff_utc=utc(cutoff))
    horizon = snapshot.get('horizon_hours')
    if horizon is not None and horizon == horizon:  # not NaN
        context['deadline_utc'] = utc(cutoff + float(horizon) * 3600)
        context['hours_to_deadline'] = round(float(horizon), 1)
    context['documents'] = [{**{k: d[k] for k in ('document_id', 'title', 'body', 'source', 'source_type', 'content_kind')},
                             'published_utc': utc(d['published_at'])} for d in docs]
    return context


def too_old(cutoff, cfg, now):
    """A live forecast must be generated soon after its cutoff; older snapshots are skipped, not failed."""
    return now - int(cutoff) > cfg['llm'].get('max_forecast_delay_seconds', 900)


def plan(rows, finished, cfg, now, limit=None):
    """Choose which snapshots to forecast now. Returns (ready, skipped_stale).

    Newest first: fresh snapshots are the only ones a live run may still forecast, and a limit or an
    exhausted budget should spend itself on them. Selection never looks at outcomes or prices.
    """
    ready, skipped = [], 0
    for row in sorted(rows, key=lambda r: (-int(r['cutoff']), r['snapshot_id'])):
        if row['snapshot_id'] in finished:
            continue
        if row['source_kind'] != 'synthetic' and not cfg['llm']['allow_retrospective'] and too_old(row['cutoff'], cfg, now):
            skipped += 1
            continue
        if limit is not None and len(ready) >= limit:
            break
        ready.append(row)
    return ready, skipped


def forecast_all(client, cfg, context, context_ns, outputs):
    """All systems for one snapshot. Fills `outputs` progressively so partial results survive a failure."""
    outputs['news_agent'] = news_agent.forecast(client, context, cfg)
    outputs['base_rate_agent'] = base_rate_agent.forecast(client, context, cfg)
    outputs['llm_no_retrieval'] = llm_no_retrieval.forecast(client, context, cfg)
    outputs['single_agent_rag'] = single_agent_rag.forecast(client, context, cfg)
    outputs['single_agent_kx'] = single_agent_kx.forecast(client, context, cfg)
    drafts = [outputs['news_agent'], outputs['base_rate_agent']]
    outputs['multi_unverified'] = dict(probability=sum(d['probability'] for d in drafts)/2,
        evidence_ids=outputs['news_agent']['evidence_ids'], rationale='Equal-weight independent agent mean.', flags=[])
    outputs['multi_verified'] = verifier.forecast(client, context, drafts, cfg)
    news_ns = news_agent.forecast(client, context_ns, cfg)
    outputs['multi_no_social'] = verifier.forecast(client, context_ns, [news_ns, outputs['base_rate_agent']], cfg)


def run_agents(con, data, cfg, limit=None, market_ids=None):
    """Forecast snapshots. market_ids restricts the run to those markets (applied before limit).

    A requested market with no snapshot is reported as a failure and the others still run; if none of the requested
    markets has a snapshot the run stops with an error. Selection never looks at prices or outcomes.
    """
    requested_failures = []
    if market_ids is not None:
        wanted = {str(m) for m in market_ids}
        if not wanted:
            raise ValueError('market_ids is empty')
        present = set(data.market_id.astype(str)) if not data.empty else set()
        for missing in sorted(wanted - present):
            requested_failures.append(dict(market_id=missing, error='NoSnapshot: no snapshot exists for this market'))
        data = data[data.market_id.astype(str).isin(wanted)] if not data.empty else data
        if data.empty:
            raise ValueError('No snapshots for the requested market ids: ' + ', '.join(sorted(wanted)))
    if data.empty:
        raise ValueError('No snapshots to forecast')
    kinds = set(data.source_kind)
    if len(kinds) != 1 or (cfg['mode'] == 'synthetic') != (kinds == {'synthetic'}):
        raise ValueError('Mixed or mismatched source kind')
    client = LLMClient(cfg)
    prompt_hash = digest({p.name: p.read_text() for p in sorted(Path(cfg['llm']['prompt_dir']).glob('*.md'))})
    code_hash = forecast_fingerprint()
    identity = digest(forecast_identity(cfg))
    # Identity = what determines a forecast. Paths, split dates, budgets and collector code are excluded,
    # so moving machines or setting split dates later does not orphan collected forecasts.
    run_id = digest([identity, client.provider, client.model, prompt_hash, code_hash])
    existing = con.execute('SELECT run_id FROM forecast_runs WHERE run_id=?', (run_id,)).fetchone()
    if not existing:
        insert(con, 'forecast_runs', dict(run_id=run_id, created_at=utcnow(), config_json=canonical(cfg),
            config_hash=identity, model=client.model, provider=client.provider,
            prompt_hash=prompt_hash, source_kind=next(iter(kinds))))
        write_json(Path(cfg['artifact_dir'])/run_id[:12]/'run_metadata.json',
            {'run_id': run_id, 'code_hash': code_hash, 'prompt_hash': prompt_hash,
             'config_hash': identity, 'runtime': runtime_info(), 'created_at': utcnow()})
    failures = list(requested_failures)
    for failure in requested_failures:
        event(con, 'agents', 'failed', failure)
    now = utcnow()
    counts = dict(con.execute("SELECT snapshot_id, COUNT(*) FROM forecasts WHERE run_id=? AND status='ok' "
                              "GROUP BY snapshot_id", (run_id,)).fetchall())
    finished = {sid for sid, n in counts.items() if n == len(SYSTEMS)}
    ready, skipped_stale = plan(data.to_dict('records'), finished, cfg, now, limit)

    # Phase 1 (main thread: database reads) - freeze evidence and decide admissibility.
    pending = []
    for snapshot in ready:
        cutoff = int(snapshot['cutoff'])
        try:
            docs = retrieve(con, snapshot, cfg)
            no_social_docs = retrieve(con, snapshot, cfg, include_social=False)
            check_evidence(docs, cutoff, snapshot['source_kind'])
            require_relevant_evidence(snapshot, docs)
            if snapshot['source_kind'] != 'synthetic':
                if too_old(cutoff, cfg, now):  # only reachable when retrospective mode is allowed
                    frozen = cfg['llm'].get('model_frozen_before')
                    if not frozen or timestamp(frozen) > cutoff:
                        raise ValueError('Historical LLM run refused: need verified frozen model predating cutoff')
                known = con.execute('SELECT 1 FROM resolutions WHERE market_id=? AND resolved_at<=?',
                                    (snapshot['market_id'], utcnow())).fetchone()
                if known and not cfg['llm']['allow_retrospective']:
                    raise ValueError('Outcome already resolved before forecast generation')
        except EXPECTED_FAILURES as exc:
            failure = dict(snapshot_id=snapshot['snapshot_id'], error=type(exc).__name__+': '+str(exc))
            failures.append(failure)
            event(con, 'agents', 'failed', failure)
            continue
        pending.append((snapshot, docs, no_social_docs,
                        context_for(snapshot, docs), context_for(snapshot, no_social_docs)))

    # Phase 2 (LLM calls only - no database access, so it can run in worker threads).
    def generate(item):
        snapshot, _, _, context, context_ns = item
        outputs = {}
        if client.exhausted:
            return outputs, 'budget'
        if (snapshot['source_kind'] != 'synthetic' and not cfg['llm']['allow_retrospective']
                and too_old(snapshot['cutoff'], cfg, utcnow())):
            return outputs, 'stale'  # waited too long in the queue; the freshness window is enforced at generation time
        try:
            forecast_all(client, cfg, context, context_ns, outputs)
        except BudgetExhausted:
            return outputs, 'budget'
        except EXPECTED_FAILURES as exc:
            return outputs, exc
        return outputs, None

    workers = max(1, int(cfg['llm'].get('concurrency', 1)))
    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    not_attempted = 0
    try:
        results = executor.map(generate, pending) if executor else map(generate, pending)
        # Phase 3 (main thread: database writes), in a deterministic order.
        for item, (outputs, problem) in zip(pending, results):
            snapshot, docs, no_social_docs = item[0], item[1], item[2]
            # Cache reuses successful API calls on resume. Failed forecast rows are logged
            # to pipeline_events, not permanently installed as a completed forecast.
            for system, output in outputs.items():
                evidence = [] if system in ('base_rate_agent', 'llm_no_retrieval') else (
                    no_social_docs if system == 'multi_no_social' else docs)
                record = dict(forecast_id=digest([run_id, snapshot['snapshot_id'], system]), run_id=run_id,
                    snapshot_id=snapshot['snapshot_id'], system=system, probability=output['probability'],
                    status='ok', result_json=canonical(output),
                    evidence_json=canonical([d['document_id'] for d in evidence]), generated_at=utcnow())
                if not con.execute('SELECT 1 FROM forecasts WHERE forecast_id=?', (record['forecast_id'],)).fetchone():
                    insert(con, 'forecasts', record)
            if problem == 'stale':
                skipped_stale += 1
            elif problem == 'budget':
                not_attempted += 1
            elif problem is not None:
                failure = dict(snapshot_id=snapshot['snapshot_id'], error=type(problem).__name__+': '+str(problem))
                failures.append(failure)
                event(con, 'agents', 'failed', failure)
    finally:
        if executor:
            executor.shutdown(wait=True)
    if not_attempted:
        failure = dict(error=f'BudgetExhausted: LLM call budget spent; {not_attempted} snapshot(s) not (fully) forecast; '
                             'raise llm.max_calls_per_run and rerun to resume', snapshots_not_forecast=not_attempted)
        failures.append(failure)
        event(con, 'agents', 'failed', failure)
    return dict(run_id=run_id, calls=client.calls, cache_hits=client.cache_hits, failures=failures,
                market_ids=sorted(str(m) for m in market_ids) if market_ids is not None else None,
                skipped_stale=skipped_stale, prompt_tokens=client.prompt_tokens,
                completion_tokens=client.completion_tokens, response_models=sorted(client.response_models))
