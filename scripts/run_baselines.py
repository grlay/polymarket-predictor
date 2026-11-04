"""Market benchmarks without any LLM credentials; fitting requires mature labels."""
import argparse
from pathlib import Path

import joblib
import pandas as pd

from src.common import load_config, timestamp, write_json, config_digest
from src.db.loaders import connect
from src.dataset.snapshots import frame
from src.dataset.splits import chronological_split, group_weights
from src.models.calibrator import logistic_model
from src.evaluation.metrics import brier_score


def run(con, cfg):
    data = frame(con)
    if data.empty:
        raise ValueError('No eligible snapshots; run ingestion and build_dataset first')
    out = Path(cfg['artifact_dir'])/'market_baselines'
    out.mkdir(parents=True, exist_ok=True)
    data['market_only'] = data.market_price
    data['constant_50'] = .5
    data[['snapshot_id','cutoff','event_group','market_id','market_only','constant_50']].to_csv(out/'predictions.csv',index=False)
    manifest = {'status':'RAW_BASELINES_READY_NO_FITTED_MODEL','rows':len(data),
                'source_kind':sorted(set(data.source_kind)), 'config_hash':config_digest(cfg)}
    if cfg['training']['train_end'] and cfg['training']['validation_end']:
        train,val,test,purged=chronological_split(data,timestamp(cfg['training']['train_end']),timestamp(cfg['training']['validation_end']))
        if train.event_group.nunique() >= cfg['training']['min_train_groups'] and val.event_group.nunique() >= cfg['training']['min_validation_groups']:
            fitted={}
            for name in ('market_calibrated','stat_logreg'):
                scores=[]
                for c in cfg['training']['regularization_grid']:
                    model=logistic_model(name,c).fit(train,group_weights(train))
                    scores.append((brier_score(val.outcome,model.predict(val),group_weights(val)),c))
                chosen=min(scores)[1]
                final=pd.concat([train,val],ignore_index=True)
                fitted[name]=logistic_model(name,chosen).fit(final,group_weights(final))
                if not test.empty:
                    test[name]=fitted[name].predict(test)
            joblib.dump(fitted,out/'market_models.joblib')
            test.to_csv(out/'test_predictions.csv',index=False)
            manifest.update(status='MARKET_MODELS_FITTED',train_groups=train.event_group.nunique(),
                            validation_groups=val.event_group.nunique(),test_groups=test.event_group.nunique())
        else:
            manifest['blocking_reason']='Insufficient mature train/validation groups'
    else:
        manifest['blocking_reason']='Split dates are not configured'
    write_json(out/'manifest.json',manifest)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='configs/config.yaml')
    args=parser.parse_args()
    cfg=load_config(args.config)
    con=connect(cfg['database'])
    print(run(con,cfg))
    con.close()
