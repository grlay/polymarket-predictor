"""Run public collectors; failures are persisted and cause exit status 2."""
import argparse
import json
from collections import Counter
from pathlib import Path

from src.common import load_config, utcnow, write_json
from src.db.loaders import connect, rows, event
from src.ingestion.http import HTTPClient
from src.ingestion import gamma, discovery, clob, data_api, news, social

STAGES = {'markets', 'quotes', 'history', 'news', 'rss', 'gdelt', 'social', 'resolutions'}


def select_markets(con, cfg):
    if cfg['polymarket'].get('discovery') == 'events':
        return discovery.tracked_markets(con, cfg)
    return rows(con, '''SELECT m.* FROM markets m JOIN market_versions v USING(market_id)
                   WHERE source_kind=? GROUP BY m.market_id ORDER BY MAX(v.observed_at) DESC,m.market_id LIMIT ?''',
                ('real', cfg['polymarket']['max_markets']))


def summarize(report, max_samples=5):
    """Compact per-stage view for the health log and pipeline_events (full detail stays in the JSON report)."""
    ok, zero, failed = Counter(), Counter(), Counter()
    for item in report['successes']:
        ok[item['stage']] += 1
        if item['result'] == 0:
            zero[item['stage']] += 1
    for item in report['failures']:
        failed[item['stage']] += 1
    return {'started_at': report['started_at'], 'finished_at': report.get('finished_at'),
            'ok': dict(ok), 'zero_results': dict(zero), 'failed': dict(failed),
            'sample_failures': report['failures'][:max_samples]}


def run(cfg, stages, bucket='ingest'):
    con, client = connect(cfg['database']), HTTPClient(cfg, bucket)
    report = {'started_at': utcnow(), 'successes': [], 'failures': []}

    def attempt(stage, identity, action):
        try:
            output = action()
            report['successes'].append({'stage': stage, 'identity': identity,
                                        'result': output if isinstance(output, int) else 1})
        except Exception as exc:
            failure = {'stage': stage, 'identity': identity, 'error': type(exc).__name__ + ': ' + str(exc)}
            report['failures'].append(failure)
            if stage != 'quotes':  # an expected trickle (closed books) must not flood the log
                event(con, stage, 'failed', failure)

    if 'markets' in stages:
        collector = discovery.collect_events if cfg['polymarket'].get('discovery') == 'events' else gamma.collect
        attempt('markets', 'gamma', lambda: collector(client, con, cfg))
    if stages & {'quotes', 'history'}:
        for market in select_markets(con, cfg):
            if 'quotes' in stages:
                attempt('quotes', market['market_id'], lambda m=market: clob.collect_quote(client, con, cfg, m))
            if 'history' in stages:
                backend = cfg['polymarket']['history_backend']
                if backend not in ('v2', 'legacy_clob'):
                    raise ValueError('history_backend must be v2 or legacy_clob')
                collect = data_api.collect_history if backend == 'v2' else clob.legacy_history
                end = utcnow()
                attempt('history', market['market_id'], lambda m=market, c=collect: c(
                    client, con, cfg, m, end - int(cfg['polymarket']['history_days'] * 86400), end))
    if 'resolutions' in stages:
        pending = discovery.unresolved_markets(con, cfg)
        for start in range(0, len(pending), 20):
            attempt('resolutions', f"batch{start // 20}",
                    lambda chunk=pending[start:start + 20]: data_api.collect_resolutions(client, con, cfg, chunk))
    if stages & {'news', 'rss'}:
        for url in cfg['news']['rss_urls']:
            attempt('rss', url, lambda u=url: news.collect_rss(client, con, u))
    if stages & {'news', 'gdelt'}:
        for query in cfg['news']['gdelt_queries']:
            attempt('gdelt', query, lambda q=query: news.collect_gdelt(client, con, q))
    if 'social' in stages:
        for subreddit in cfg['social']['subreddits']:
            attempt('reddit', subreddit, lambda s=subreddit: social.collect_reddit(client, con, s))
    report['finished_at'] = utcnow()
    write_json(Path(cfg['artifact_dir']) / 'ingestion_latest.json', report)
    event(con, 'ingest', 'partial' if report['failures'] else 'complete', summarize(report))
    con.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='configs/config.yaml')
    parser.add_argument('--stages', default='markets,quotes,news',
                        help='Comma-separated: ' + ','.join(sorted(STAGES)))
    args = parser.parse_args()
    stages = set(args.stages.split(','))
    if not stages <= STAGES:
        parser.error('Unknown stage')
    result = run(load_config(args.config), stages)
    print(json.dumps(result, indent=2))
    return 2 if result['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
