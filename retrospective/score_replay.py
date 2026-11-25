"""B9 replay scoring (decisions log R3, R5, R6, R9, R14, R16). Read-only on the replay database.

    python -m retrospective.score_replay        # prints the tables; writes data/retro/out/scores.json + scores_tables.tex

Unit market x cutoff; 24 h primary, 168 h secondary, reported separately. Brier averaged within market, then within race
cluster, equal weight per cluster. Paired difference against the raw market with the event-cluster percentile bootstrap
(2000 draws, seed 2605). Cohort A = snapshots where every compared agent system has a forecast; cohort B = all snapshots
(no-evidence system, market and naive baselines). Forecasts written after the R8 hard stop are excluded. Exploratory:
fixed 50/50 log-odds average of market and multi_verified (decisions log 2026-11-12).
"""
import csv
import json
import pathlib
import sqlite3
import sys

import numpy as np
import pandas as pd

from src.evaluation.metrics import log_loss
from src.evaluation.stats_tests import paired_cluster_bootstrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / 'data' / 'retro' / 'retro.sqlite'
SELECTION = ROOT / 'retrospective' / 'selection.csv'
HARD_STOP = 1795535940                     # 2026-11-24T15:59:00Z (R8)
AGENTS = ['llm_no_retrieval', 'single_agent_rag', 'single_agent_kx', 'multi_unverified', 'multi_verified']
COMPONENTS = ['news_agent', 'base_rate_agent']
CANDIDATES = ['single_agent_rag', 'single_agent_kx', 'multi_unverified', 'multi_verified']    # R6
CALLS = {'single_agent_rag': 1, 'multi_unverified': 2, 'multi_verified': 3, 'single_agent_kx': 3}  # R16
BASELINES = ['market', 'half', 'uniform']
EXPLORATORY = ['fixed_50_50']


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p / (1 - p))


def load(db=DB, selection=SELECTION):
    con = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        snaps = pd.read_sql_query('''SELECT s.snapshot_id, s.market_id, s.horizon_hours, s.market_price AS market,
            m.event_group, r.outcome FROM snapshots s JOIN markets m USING(market_id)
            JOIN resolutions r USING(market_id)''', con)
        fc = pd.read_sql_query('''SELECT snapshot_id, system, probability FROM forecasts
            WHERE status='ok' AND generated_at <= ?''', con, params=(HARD_STOP,))
        n_snaps = con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
    finally:
        con.close()
    if len(snaps) != n_snaps:
        raise SystemExit(f'{n_snaps - len(snaps)} snapshot(s) have no label: import all labels first')
    wide = fc.pivot_table(index='snapshot_id', columns='system', values='probability', aggfunc='first')
    frame = snaps.merge(wide, left_on='snapshot_id', right_index=True, how='left')
    frame['horizon'] = frame.horizon_hours.round().astype(int)
    with open(selection, encoding='utf-8') as f:
        event = {r['market_id']: r['event_id'] for r in csv.DictReader(f)
                 if 'yes' in (r['eligible_168h'], r['eligible_24h'])}
    k = pd.Series(event).value_counts()                     # selected markets per race (R3)
    frame['half'] = 0.5
    frame['uniform'] = 1.0 / frame.market_id.map(event).map(k)
    if 'multi_verified' in frame:
        frame['fixed_50_50'] = 1 / (1 + np.exp(-(0.5 * logit(frame.market) + 0.5 * logit(frame.multi_verified))))
    return frame


def weighted(frame, column, fn):
    """Score averaged within market, then within cluster, equal weight per cluster (R5)."""
    per_row = fn(frame.outcome.to_numpy(float), frame[column].to_numpy(float))
    tmp = frame.assign(_s=per_row)
    return float(tmp.groupby(['event_group', 'market_id'])._s.mean().groupby(level=0).mean().mean())


def brier_rows(y, p):
    return (p - y) ** 2


def logloss_rows(y, p):
    return np.array([log_loss([a], [b]) for a, b in zip(y, p)])


def table(frame, systems):
    out = {}
    for s in systems:
        if s not in frame or frame[s].isna().any():
            continue
        row = dict(brier=weighted(frame, s, brier_rows), log_loss=weighted(frame, s, logloss_rows),
                   snapshots=int(len(frame)), clusters=int(frame.event_group.nunique()))
        if s != 'market':
            boot = paired_cluster_bootstrap(frame, s, 'market')
            row.update(diff_vs_market=boot['mean_brier_difference'], ci95=boot['ci95'])
        out[s] = row
    return out


def select_t3(frame, scores):
    """R6 + R16 on cohort A at 24 h."""
    briers = {c: scores[c]['brier'] for c in CANDIDATES}
    provisional = min(briers, key=briers.get)
    ties = [provisional]
    for c in CANDIDATES:
        if c == provisional:
            continue
        ci = paired_cluster_bootstrap(frame, c, provisional)['ci95']
        if ci is not None and ci[0] <= 0 <= ci[1]:
            ties.append(c)
    winner = min(ties, key=lambda c: (CALLS[c], briers[c]))
    return dict(provisional=provisional, tied_with_provisional=sorted(ties), winner=winner)


def contamination(scores_b24):
    """R9 + R16: warning if the CI of llm_no_retrieval minus market includes 0 or lies below 0."""
    ci = scores_b24['llm_no_retrieval']['ci95']
    warning = ci is not None and (ci[0] <= 0 <= ci[1] or ci[1] < 0)
    return dict(ci95=ci, warning=bool(warning))


def score(frame):
    report = {}
    for h in (24, 168):
        f = frame[frame.horizon == h]
        a = f.dropna(subset=AGENTS)
        report[f'{h}h'] = dict(
            cohort_A=table(a, BASELINES + AGENTS + COMPONENTS + EXPLORATORY),
            cohort_B=table(f.dropna(subset=['llm_no_retrieval']), BASELINES + ['llm_no_retrieval']),
            snapshots_total=int(len(f)), snapshots_A=int(len(a)))
    a24 = frame[(frame.horizon == 24)].dropna(subset=AGENTS)
    report['T3'] = select_t3(a24, report['24h']['cohort_A'])
    report['R9_contamination'] = contamination(report['24h']['cohort_B'])
    return report


def latex(report):
    lines = []
    for h in ('24h', '168h'):
        lines += [f'% {h}, cohort A ({report[h]["snapshots_A"]} snapshots)',
                  '\\begin{tabular}{l r r r l}', '\\toprule',
                  'System & Brier & Log loss & $\\Delta$ vs market & 95\\% CI \\\\', '\\midrule']
        for s, r in report[h]['cohort_A'].items():
            ci = r.get('ci95')
            lines.append(f"\\code{{{s.replace('_', chr(92) + '_')}}} & {r['brier']:.4f} & {r['log_loss']:.3f} & "
                         + (f"{r['diff_vs_market']:+.4f} & [{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else '-- & --') + ' \\\\')
        lines += ['\\bottomrule', '\\end{tabular}', '']
    return '\n'.join(lines)


def main():
    report = score(load())
    out = ROOT / 'data' / 'retro' / 'out'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'scores.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    (out / 'scores_tables.tex').write_text(latex(report), encoding='utf-8')
    for h in ('24h', '168h'):
        for cohort in ('cohort_A', 'cohort_B'):
            rows = report[h][cohort]
            n = next(iter(rows.values()))
            print(f"\n{h} {cohort}: {n['snapshots']} snapshots, {n['clusters']} clusters")
            for s, r in rows.items():
                ci = r.get('ci95')
                extra = f"  diff {r['diff_vs_market']:+.4f} CI [{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else ''
                print(f"  {s:<18} Brier {r['brier']:.4f}  logloss {r['log_loss']:.3f}{extra}")
    print('\nT3 (R6/R16):', report['T3'])
    print('R9 contamination check (R16):', report['R9_contamination'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
