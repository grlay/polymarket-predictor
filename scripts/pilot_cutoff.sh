#!/bin/sh
# Daily pilot cutoff from 2026-11-11 (decisions log, 2026-11-10): build the snapshots ONCE, then run three
# separate model series on them in parallel. Each series has its own lock and its own log; none replaces another.
#
#   sh scripts/pilot_cutoff.sh cutoff   # cron 05:00 UTC: build at <today>T04:59:00Z, then run all series
#   sh scripts/pilot_cutoff.sh retry    # cron 05:06 / 05:11 UTC: resume unfinished series only (no build)
#   sh scripts/pilot_cutoff.sh dry      # by hand: build at now, run the two CANDIDATES only (never 3.6 Flash)
#
# Series:
#   lite      gemini-3.5-flash-lite, both markets   (Gemini base URL and key from .env)
#   ref       gemini-3.6-flash, Rubio only          (free tier: 20 requests per day)
#   deepseek  deepseek-v4-pro, both markets         ONLY if $HOME/.deepseek.env exists (LLM_API_BASE,
#             LLM_API_KEY, LLM_MODEL). To switch DeepSeek off, rename that file; no cron edit is needed.
# Variables set here take priority over .env (src/common.py load_env uses setdefault).
set -u
MODE="${1:-}"
cd "$HOME/polymarket-predictor" || exit 1
PY=.venv/bin/python
CFG=configs/config_pilot.yaml
BOTH="6723099 6282802"
RUBIO="6723099"
LOGDIR=artifacts/research

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

series() {  # name, markets; runs in a subshell so its variables never leak into another series
    name="$1"; markets="$2"
    (
        case "$name" in
            lite) LLM_MODEL=gemini-3.5-flash-lite; export LLM_MODEL ;;
            ref) LLM_MODEL=gemini-3.6-flash; export LLM_MODEL ;;
            deepseek) set -a; . "$HOME/.deepseek.env"; set +a ;;
        esac
        echo "=== $(stamp) $MODE $name model=$LLM_MODEL markets=$markets"
        flock -n "/tmp/pilot_$name.lock" $PY -m scripts.run_agents --config $CFG --market-id $markets \
            || echo "=== $(stamp) $name: exit $? (lock held by another pass, or a run error above)"
    ) >> "$LOGDIR/pilot_$name.log" 2>&1
}

case "$MODE" in
    cutoff) AT="$(date -u +%Y-%m-%d)T04:59:00Z" ;;
    dry) AT=now ;;
    retry) AT="" ;;
    *) echo "usage: sh scripts/pilot_cutoff.sh cutoff|retry|dry"; exit 2 ;;
esac

echo "=== $(stamp) mode=$MODE"
if [ -n "$AT" ]; then
    echo "=== $(stamp) build --at $AT"
    $PY -m scripts.build_dataset --config $CFG --at "$AT" --market-id $BOTH || echo "=== build failed: exit $?"
fi

series lite "$BOTH" &
if [ "$MODE" != dry ]; then
    series ref "$RUBIO" &
fi
if [ -f "$HOME/.deepseek.env" ]; then
    series deepseek "$BOTH" &
else
    echo "=== $(stamp) deepseek skipped: no $HOME/.deepseek.env"
fi
wait
echo "=== $(stamp) done (series logs: $LOGDIR/pilot_lite.log, pilot_ref.log, pilot_deepseek.log)"
