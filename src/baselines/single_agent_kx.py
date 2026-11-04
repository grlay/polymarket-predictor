import numpy as np

from src.baselines.single_agent_rag import forecast as single


def forecast(client, context, cfg):
    """K repeated single-agent calls, sampled (llm.k_temperature) so the members can actually differ.

    At temperature 0 repeated calls are identical, which would make this baseline a copy of the single
    agent and the specialization comparison unfair. Set k_temperature to null to reproduce that behaviour.
    """
    temperature = cfg['llm'].get('k_temperature', 0.7)
    predictions = [single(client, context, cfg, i + 1, temperature) for i in range(cfg['llm']['k'])]
    return dict(probability=float(np.mean([p['probability'] for p in predictions])),
                evidence_ids=sorted({d for p in predictions for d in p['evidence_ids']}),
                rationale='Arithmetic mean of repeated sampled single-agent calls.', flags=[], members=predictions)
