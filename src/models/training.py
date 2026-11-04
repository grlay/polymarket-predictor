import json
from pathlib import Path

import joblib
import pandas as pd

from src.agents.orchestrator import SYSTEMS
from src.common import (digest, timestamp, utcnow, write_json, read_json, config_digest, forecast_identity,
                        forecast_fingerprint, analysis_fingerprint)
from src.dataset.snapshots import frame as dataset_frame
from src.dataset.splits import chronological_split, group_weights
from src.evaluation.metrics import brier_score
from src.models.calibrator import MODEL_COLUMNS, logistic_model, BayesianLogisticCorrection


def joined_forecasts(con, run_id):
    # Only fixed-horizon snapshots of the dataset configuration this run forecast: --at-now pilots and snapshots
    # built under other dataset settings must never leak into fitting or evaluation.
    hashes = {r[0] for r in con.execute(
        'SELECT DISTINCT s.config_hash FROM snapshots s JOIN forecasts f USING(snapshot_id) WHERE f.run_id=?', (run_id,))}
    data = dataset_frame(con, pilot=False)
    data = data[data.config_hash.isin(hashes)].reset_index(drop=True) if not data.empty else data
    records = pd.read_sql_query('SELECT snapshot_id,system,probability FROM forecasts WHERE run_id=? AND status=?',
                                 con, params=(run_id, 'ok'))
    if data.empty or records.empty:
        raise ValueError('No data/forecasts available')
    wide = records.pivot(index='snapshot_id', columns='system', values='probability').reset_index()
    return data.merge(wide, on='snapshot_id', how='left', validate='one_to_one')


def fit(con, cfg, run_id):
    if not cfg['training']['train_end'] or not cfg['training']['validation_end']:
        raise ValueError('Set explicit UTC train_end and validation_end before fitting')
    data = joined_forecasts(con, run_id)
    if len(set(data.source_kind)) != 1:
        raise ValueError('Mixed source kinds')
    run = con.execute('SELECT * FROM forecast_runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None or run['config_hash'] != digest(forecast_identity(cfg)):
        raise ValueError('Forecast/config mismatch: model, prompts or retrieval settings changed since forecasting')
    metadata = read_json(Path(cfg['artifact_dir'])/run_id[:12]/'run_metadata.json')
    if metadata['code_hash'] != forecast_fingerprint():
        raise ValueError('Forecasting code changed after forecasting; start a new versioned run')
    for system in SYSTEMS:
        if system not in data:
            data[system] = float('nan')
    train_end, val_end = timestamp(cfg['training']['train_end']), timestamp(cfg['training']['validation_end'])
    train_all, validation_all, test, purged = chronological_split(data, train_end, val_end)
    train = train_all.dropna(subset=SYSTEMS).copy()
    validation = validation_all.dropna(subset=SYSTEMS).copy()
    if train.event_group.nunique() < cfg['training']['min_train_groups']:
        raise ValueError('Too few complete independent training groups; no research model fitted')
    if validation.event_group.nunique() < cfg['training']['min_validation_groups']:
        raise ValueError('Too few complete independent validation groups')
    if train.outcome.nunique() < 2:
        raise ValueError('Training set needs both outcome classes')
    artifact = Path(cfg['artifact_dir'])/run_id[:12]
    artifact.mkdir(parents=True, exist_ok=True)
    for name, subset in [('train',train), ('validation',validation), ('test',test), ('purged',purged)]:
        subset.to_csv(artifact/f'{name}_manifest.csv', index=False)
    models, selections, parameters = {}, {}, {}
    final_train = pd.concat([train, validation], ignore_index=True)
    for name in MODEL_COLUMNS:
        scores = []
        for c in cfg['training']['regularization_grid']:
            candidate = logistic_model(name, c).fit(train, group_weights(train))
            scores.append({'C': c, 'validation_brier': brier_score(validation.outcome,
                candidate.predict(validation), group_weights(validation))})
        chosen = min(scores, key=lambda x: (x['validation_brier'], x['C']))['C']
        models[name] = logistic_model(name, chosen).fit(final_train, group_weights(final_train))
        selections[name] = {'grid': scores, 'selected_C': chosen}
        parameters[name] = models[name].describe()
    models['bayesian_correction'] = BayesianLogisticCorrection(cfg['training']['bayesian_prior_sd']).fit(
        final_train, group_weights(final_train))
    parameters['bayesian_correction'] = models['bayesian_correction'].describe()
    bundle = {'models': models, 'fit_time': val_end, 'run_id': run_id, 'config_hash': config_digest(cfg),
              'source_kind': data.source_kind.iloc[0], 'trained_snapshot_ids': sorted(final_train.snapshot_id)}
    joblib.dump(bundle, artifact/'models.joblib')
    manifest = {'run_id': run_id, 'config_hash': config_digest(cfg), 'code_hash': metadata['code_hash'],
        'analysis_code_hash': analysis_fingerprint(), 'source_kind': bundle['source_kind'],
        'created_at': utcnow(), 'fit_asof': val_end, 'train_groups': train.event_group.nunique(),
        'validation_groups': validation.event_group.nunique(), 'test_groups': test.event_group.nunique(),
        'purged_groups': purged.event_group.nunique(), 'selection': selections, 'parameters': parameters,
        'excluded_incomplete_training_rows': len(train_all)-len(train),
        'excluded_incomplete_validation_rows': len(validation_all)-len(validation),
        'trained_snapshot_hash': digest(sorted(final_train.snapshot_id)),
        'status': 'SYNTHETIC_DEMONSTRATION_ONLY' if bundle['source_kind']=='synthetic' else 'REAL_DATA_MODEL'}
    write_json(artifact/'training_manifest.json', manifest)
    return artifact, manifest
