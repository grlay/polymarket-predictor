"""Shared serialization, configuration and UTC contracts. No network side effects."""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def utcnow():
    return int(datetime.now(timezone.utc).timestamp())


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0 or value > 1e11:
            raise ValueError("Expected finite epoch SECONDS, not milliseconds")
        return int(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing timestamp")
    if value.isdigit():
        return timestamp(int(value))
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required: use UTC Z or explicit offset")
    return int(dt.timestamp())


def iso(value):
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        for n, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"Invalid JSON at line {n}") from exc


def load_config(path="configs/config.yaml"):
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("database", "data_dir", "artifact_dir", "market_filters"):
        if key not in cfg:  # collector configs carry no filters/LLM settings
            continue
        value = Path(cfg[key])
        cfg[key] = str(value if value.is_absolute() else ROOT / value)
    if "llm" in cfg:
        cfg["llm"]["prompt_dir"] = str(ROOT / cfg["llm"]["prompt_dir"])
    cfg["config_path"] = str(path)
    return cfg


def load_env(path=None):
    """Small .env reader: no shell evaluation, interpolation or overwriting env."""
    path = Path(path or ROOT / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def probability(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a probability")
    p = float(value)
    if not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError("Probability must be finite in [0, 1]")
    return p


def source_fingerprint():
    """Whole-project hash. Too broad for run identity (any collector edit would change it); kept for audits."""
    return _fingerprint(('src', 'scripts'))


# Machine-specific keys must not change an experiment's identity (laptop vs server paths).
PATH_KEYS = ('database', 'data_dir', 'artifact_dir', 'market_filters', 'config_path')
# Settings that change how a run is operated, not what it forecasts.
OPERATIONAL_LLM = ('max_calls_per_run', 'timeout_seconds', 'concurrency', 'retries', 'format_retries',
                   'retry_wait_seconds', 'prompt_dir')

# Only code that decides what the LLM sees and how forecasts are made belongs to a forecast run's identity.
FORECAST_CODE = ('src/agents', 'src/retrieval', 'src/baselines/llm_no_retrieval.py',
                 'src/baselines/single_agent_rag.py', 'src/baselines/single_agent_kx.py')
# Frozen analysis = forecasting code plus everything that fits models and scores them. Ingestion,
# the database layer and the dashboard are deliberately excluded so collector fixes never void a freeze.
ANALYSIS_CODE = FORECAST_CODE + ('src/baselines/stat_logreg.py', 'src/baselines/market_only.py', 'src/dataset',
                                 'src/features', 'src/models', 'src/evaluation')


def portable(cfg):
    out = {k: v for k, v in cfg.items() if k not in PATH_KEYS}
    if 'llm' in out:
        out['llm'] = {k: v for k, v in out['llm'].items() if k != 'prompt_dir'}
    return out


def config_digest(cfg):
    """Whole configuration, independent of where the project lives."""
    return digest(portable(cfg))


# Hand-written evidence profiles decide which documents an agent may see, so they belong to a forecast's identity.
RELEVANCE_PROFILES = ROOT / 'configs' / 'relevance_profiles.yaml'


def profiles_digest():
    """Hash of the relevance-profile file (None if absent). Text-mode read keeps it identical on Windows and Linux."""
    return digest(RELEVANCE_PROFILES.read_text(encoding='utf-8')) if RELEVANCE_PROFILES.exists() else None


def forecast_identity(cfg):
    """What determines a forecast: LLM settings, retrieval rules, evidence profiles, mode. Split dates and paths are not part of it."""
    llm = {k: v for k, v in cfg['llm'].items() if k not in OPERATIONAL_LLM}
    return {'mode': cfg['mode'], 'llm': llm, 'retrieval': cfg['retrieval'],
            'lookback_days': cfg['dataset']['lookback_days'], 'relevance_profiles': profiles_digest()}


def _fingerprint(paths):
    files = set()
    for rel in paths:
        target = ROOT / rel
        files.update(target.rglob('*.py') if target.is_dir() else [target])
    # Forward-slash keys and text-mode reads keep the hash identical on Windows and Linux.
    return digest({f.relative_to(ROOT).as_posix(): f.read_text(encoding='utf-8') for f in sorted(files)})


def forecast_fingerprint():
    return _fingerprint(FORECAST_CODE)


def analysis_fingerprint():
    return _fingerprint(ANALYSIS_CODE)


def runtime_info():
    import importlib.metadata
    import platform
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'packages': {name: importlib.metadata.version(name) for name in
                         ('numpy','scipy','pandas','scikit-learn','PyYAML','joblib','matplotlib')}}
