import argparse
from pathlib import Path

from src.common import digest, load_config, utcnow, write_json, ROOT, config_digest, analysis_fingerprint


def main():
    parser = argparse.ArgumentParser(description='Create immutable local protocol hash; publish/commit for external timestamping.')
    parser.add_argument('--config', default='configs/config.yaml')
    args = parser.parse_args()
    cfg = load_config(args.config)
    path = Path(cfg['artifact_dir'])/'protocol_lock.json'
    if path.exists():
        raise ValueError('Protocol already frozen. Use a new experiment directory for changes.')
    if not cfg['training']['train_end'] or not cfg['training']['validation_end']:
        raise ValueError('Set split dates before freezing')
    prompt_hash = digest({p.name: p.read_text() for p in sorted(Path(cfg['llm']['prompt_dir']).glob('*.md'))})
    write_json(path, {'config_hash': config_digest(cfg), 'frozen_at': utcnow(), 'prompt_hash': prompt_hash,
        'protocol_hash': digest((ROOT/'docs'/'protocol.md').read_text()), 'code_hash': analysis_fingerprint(),
        'note': 'Local hash is not independent preregistration. Commit/publish before test forecasting.'})
    print(path)


if __name__ == '__main__':
    main()
