"""Tag-based Gamma event discovery for the collector, plus tracking queries.

Scope is a reviewed watchlist of Gamma tag slugs (configs/collect.yaml), not "highest volume".
Never infers historical metadata availability: every version is stamped with its observation time.
"""
import re
from collections import Counter

from src.common import timestamp, utcnow
from src.db.loaders import event, insert, rows
from src.ingestion.gamma import array, normalize_market

# A new metadata version is stored only when one of these changes (or the last is stale).
VERSION_FIELDS = ('question', 'rules', 'scheduled_end', 'category', 'is_open')


def yes_price(raw):
    """Gamma's current YES price; used only to decide whether a NEW market is worth tracking."""
    try:
        outcomes = [str(s).strip().lower() for s in array(raw.get('outcomes', []))]
        prices = array(raw.get('outcomePrices') or [])
        return float(prices[outcomes.index('yes')])
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def insert_version(con, version, refresh_seconds):
    """Store a metadata version only if it changed, or the previous one is older than refresh_seconds.

    Liquidity/volume drift constantly; refreshing them every few hours keeps as-of features current
    without re-storing multi-kilobyte rules text on every poll.
    """
    last = rows(con, '''SELECT * FROM market_versions WHERE market_id=?
        ORDER BY available_at DESC,version_id DESC LIMIT 1''', (version['market_id'],))
    if last:
        last = last[0]
        if (all(last[k] == version[k] for k in VERSION_FIELDS)
                and version['observed_at'] - last['observed_at'] < refresh_seconds):
            return False
    return insert(con, 'market_versions', version)


def _num(raw, *keys):
    for key in keys:
        if raw.get(key) not in (None, ''):
            return float(raw[key])
    return 0.0


def candidate_markets(events, settings, now):
    """Yield (event, category, raw_market, reason). reason is None for an eligible market."""
    exclude_tags = set(settings.get('exclude_tag_slugs') or [])
    patterns = [re.compile(p) for p in settings.get('exclude_question_patterns') or []]
    lo_days, hi_days = settings.get('min_days_to_end', 1.1), settings.get('max_days_to_end', 75)
    for ev in events:
        ev_tags = [t.get('slug') for t in ev.get('tags') or [] if isinstance(t, dict)]
        wanted = [t for t in settings['tag_slugs'] if t in ev_tags]
        for raw in ev.get('markets') or []:
            reason = None
            if exclude_tags & set(ev_tags):
                reason = 'excluded_tag'
            elif raw.get('closed') or not raw.get('active'):
                reason = 'not_open'
            elif any(p.search(raw.get('question', '')) for p in patterns):
                reason = 'question_pattern'
            else:
                try:
                    days = (timestamp(raw.get('endDate') or raw.get('endDateIso')) - now) / 86400
                except (ValueError, TypeError):
                    days = None
                if days is None:
                    reason = 'no_deadline'
                elif not lo_days <= days <= hi_days:
                    reason = 'deadline_window'
                elif _num(raw, 'liquidityNum', 'liquidity') < settings.get('min_liquidity', 0):
                    reason = 'liquidity'
                elif _num(raw, 'volumeNum', 'volume') < settings.get('min_volume', 0):
                    reason = 'volume'
                else:
                    price = yes_price(raw)
                    if price is not None and not settings.get('min_price', 0) <= price <= settings.get('max_price', 1):
                        reason = 'price_range'
            yield ev, (wanted[0] if wanted else 'unknown'), raw, reason


def collect_events(client, con, cfg):
    """Discover open markets under the configured tag slugs. Returns the number stored/seen."""
    settings = cfg['polymarket']
    counts, seen_events, chosen = Counter(), set(), {}
    for slug in settings['tag_slugs']:
        for page in range(settings['max_pages']):
            payload, observed, ref = client.get(settings['gamma_base'] + '/events', {
                'closed': 'false', 'tag_slug': slug, 'limit': settings['page_size'],
                'offset': page * settings['page_size'], 'order': 'volume24hr', 'ascending': 'false'})
            if not isinstance(payload, list):
                raise ValueError('Gamma events response schema changed: expected list')
            fresh = [e for e in payload if str(e.get('id')) not in seen_events]
            seen_events.update(str(e.get('id')) for e in fresh)
            for ev, category, raw, reason in candidate_markets(fresh, settings, observed):
                if reason:
                    counts[reason] += 1
                else:
                    chosen.setdefault(str(ev['id']), []).append((ev, category, raw, observed, ref))
            if len(payload) < settings['page_size']:
                break
    # Independent events matter more than many sub-markets of the same event.
    per_event, ranked = settings.get('max_markets_per_event', 5), []
    for items in chosen.values():
        items.sort(key=lambda x: -_num(x[2], 'liquidityNum', 'liquidity'))
        counts['per_event_cap'] += max(0, len(items) - per_event)
        ranked.extend(items[:per_event])
    ranked.sort(key=lambda x: -_num(x[2], 'liquidityNum', 'liquidity'))
    # Year-end "before 2027" markets share one deadline: without a cap they fill every slot and leave
    # no time spread for chronological train/validation/test splits.
    per_day = settings.get('max_markets_per_deadline_day')
    if per_day:
        seen_days, kept = Counter(), []
        for item in ranked:
            day = timestamp(item[2].get('endDate') or item[2].get('endDateIso')) // 86400
            seen_days[day] += 1
            if seen_days[day] <= per_day:
                kept.append(item)
            else:
                counts['per_deadline_day_cap'] += 1
        ranked = kept
    cap = settings.get('max_markets_per_discovery', 300)
    counts['beyond_discovery_cap'] += max(0, len(ranked) - cap)
    stored = 0
    for ev, category, raw, observed, ref in ranked[:cap]:
        raw = {**raw, 'events': [{'id': ev['id'], 'category': category}]}
        try:
            market, version = normalize_market(raw, observed, ref)
            overrides = settings.get('group_overrides', {})
            market['event_group'] = str(overrides.get(market['market_id'], market['event_group']))
            insert(con, 'markets', market)
            insert_version(con, version, settings.get('refresh_version_seconds', 21600))
            stored += 1
        except (ValueError, KeyError, TypeError) as exc:
            counts['normalize:' + str(exc)[:60]] += 1
    # One summary row per run instead of one row per excluded market.
    event(con, 'gamma', 'summary', {'events_seen': len(seen_events), 'stored': stored, 'excluded': dict(counts)})
    return stored


def tracked_markets(con, cfg, now=None):
    """Open real markets whose latest visible deadline is still ahead, most liquid first."""
    now = utcnow() if now is None else now
    return rows(con, '''SELECT m.* FROM markets m JOIN market_versions v USING(market_id)
        WHERE m.source_kind='real' AND v.is_open=1 AND v.scheduled_end>?
        AND v.available_at=(SELECT MAX(available_at) FROM market_versions WHERE market_id=m.market_id)
        GROUP BY m.market_id ORDER BY COALESCE(MAX(v.liquidity),0) DESC, m.market_id LIMIT ?''',
        (now, cfg['polymarket'].get('max_tracked', cfg['polymarket'].get('max_markets', 30))))


def unresolved_markets(con, cfg, now=None):
    """Markets with no stored label whose deadline is within the lookback window (or still ahead)."""
    now = utcnow() if now is None else now
    floor = now - cfg['polymarket'].get('resolution_lookback_days', 60) * 86400
    return rows(con, '''SELECT m.* FROM markets m JOIN market_versions v USING(market_id)
        WHERE m.source_kind='real' AND m.market_id NOT IN (SELECT market_id FROM resolutions)
        GROUP BY m.market_id HAVING MAX(v.scheduled_end)>? ORDER BY MAX(v.scheduled_end), m.market_id''', (floor,))
