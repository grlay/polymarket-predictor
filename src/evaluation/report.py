from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from src.common import digest, read_json, timestamp, utcnow, write_json, ROOT, config_digest, analysis_fingerprint
from src.dataset.splits import chronological_split, group_weights
from src.evaluation.metrics import score
from src.evaluation.calibration import reliability, plot_calibration
from src.evaluation.ablations import contrasts
from src.models.training import joined_forecasts


def evaluate(con, cfg, run_id, allow_exploratory=False):
    path = Path(cfg['artifact_dir'])/run_id[:12]
    # Load only models created locally by this project. Pickle/joblib is not a safe import format.
    bundle = joblib.load(path/'models.joblib')
    if bundle['config_hash'] != config_digest(cfg) or bundle['run_id'] != run_id:
        raise ValueError('Model/config/run identity mismatch')
    data = joined_forecasts(con, run_id)
    _, _, test, _ = chronological_split(data, timestamp(cfg['training']['train_end']),
                                       timestamp(cfg['training']['validation_end']))
    if test.empty:
        raise ValueError('No untouched test cohort')
    if set(test.snapshot_id) & set(bundle['trained_snapshot_ids']):
        raise ValueError('Training/test overlap')
    synthetic = bundle['source_kind'] == 'synthetic'
    if not synthetic and not allow_exploratory:
        lock = read_json(Path(cfg['artifact_dir'])/'protocol_lock.json')
        if lock['config_hash'] != config_digest(cfg) or lock['frozen_at'] > int(test.cutoff.min()):
            raise ValueError('Protocol was not frozen before test forecasts; use explicit exploratory flag')
        if lock['prompt_hash'] != con.execute('SELECT prompt_hash FROM forecast_runs WHERE run_id=?', (run_id,)).fetchone()[0]:
            raise ValueError('Prompts changed after protocol freeze')
        if lock['code_hash'] != analysis_fingerprint() or lock['protocol_hash'] != digest((ROOT/'docs'/'protocol.md').read_text()):
            raise ValueError('Code/protocol changed after freeze')
    test['market_only'] = test.market_price
    test['constant_50'] = .5
    for name, model in bundle['models'].items():
        try:
            if name == 'bayesian_correction':
                valid = test.multi_verified.notna()
            else:
                required = [c[len('logit_'):] for c in model.columns if c.startswith('logit_') and c != 'logit_market']
                valid = test[required].notna().all(axis=1) if required else pd.Series(True, index=test.index)
            test[name] = np.nan
            if valid.any():
                test.loc[valid, name] = model.predict(test[valid])
        except KeyError as exc:
            raise ValueError(f'Missing forecast columns: {exc}') from exc
    primary = cfg['evaluation']['primary_system']
    test['deployed_with_fallback'] = test[primary].fillna(test.market_price)
    test.to_csv(path/'test_predictions.csv', index=False)
    resolved = test[test.outcome.notna() & (test.label_available_at <= utcnow())].copy()
    if resolved.empty:
        raise ValueError('Predictions exported, but no test outcomes are resolved yet')
    names = ['constant_50', 'market_only', 'market_calibrated', 'stat_logreg', 'llm_no_retrieval',
             'single_agent_rag', 'single_agent_kx', 'multi_unverified', 'multi_verified',
             'multi_no_social', 'hybrid_single', 'hybrid_unverified', 'hybrid_verified',
             'hybrid_no_social', 'bayesian_correction', 'deployed_with_fallback']
    names = [n for n in names if n in resolved]
    common = resolved.dropna(subset=names)
    if common.empty:
        raise ValueError('No common evaluation cohort: inspect failed forecasts/coverage')
    results, curves = {}, {}
    weights = group_weights(common)
    for name in names:
        results[name] = {**score(common.outcome, common[name], weights, cfg['evaluation']['log_loss_epsilon']),
                         'available_rows': int(resolved[name].notna().sum()), 'common_rows': len(common)}
        curves[name] = reliability(common.outcome, common[name], weights, cfg['evaluation']['calibration_bins'])
    report = {'status': 'SYNTHETIC_DEMONSTRATION_ONLY' if synthetic else ('EXPLORATORY' if allow_exploratory else 'FROZEN_PROTOCOL_TEST'),
        'run_id': run_id, 'source_kind': bundle['source_kind'], 'total_test_rows': len(test),
        'resolved_rows': len(resolved), 'common_rows': len(common), 'event_groups': common.event_group.nunique(),
        'unresolved_rows': len(test)-len(resolved), 'scores_on_common_cohort': results,
        'fallback_full_resolved_cohort': score(resolved.outcome, resolved.deployed_with_fallback,
                                              group_weights(resolved), cfg['evaluation']['log_loss_epsilon']),
        'contrasts': contrasts(common, cfg), 'calibration': curves,
        'limitations': ['Event clusters do not remove all cross-event dependence.',
                        'Secondary contrasts are exploratory; no automatic claim from multiple comparisons.',
                        'LLM calls and tokens must be compared, not only agent counts.']}
    write_json(path/'evaluation.json', report)
    title = 'SYNTHETIC TEST - not research results' if synthetic else 'Held-out forecast calibration'
    plot_calibration({n: curves[n] for n in ['market_only','single_agent_rag','multi_verified','hybrid_verified']},
                     path/'calibration.png', title)
    return report
