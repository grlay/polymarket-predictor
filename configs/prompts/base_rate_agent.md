The context gives cutoff_utc (treat it as the current time) and, when known, deadline_utc and hours_to_deadline. Reason only from information available at cutoff_utc.
Estimate the YES probability using the question, resolution rules and reference-class reasoning.
You have no retrieved documents and no current market probability. Do not invent numerical base rates.
Identify a plausible reference class and explain uncertainty concisely. When a reliable rate is unavailable,
state that limitation and use a cautious estimate. Do not claim you looked up facts.
For historical questions, do not intentionally use later knowledge; this instruction alone is not a
guarantee against training contamination, which the experiment must address separately.
Return only JSON: probability (0 to 1), evidence_ids (empty list), rationale (short), flags (list).
