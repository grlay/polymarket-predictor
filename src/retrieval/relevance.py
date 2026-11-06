"""Conservative, auditable topic gates.

Two kinds of gate, both scoped lexical rules rather than semantic relevance guarantees:
  * the original US House-control gate (hard-coded below);
  * hand-written profiles loaded from configs/relevance_profiles.yaml (entities, institution terms, action terms,
    window), one per storyline, matched to a market by a regex on its question.
Markets with neither keep the original retrieval behaviour and need their own reviewed profile.
"""
import functools
import re
from pathlib import Path

import yaml

from src.common import RELEVANCE_PROFILES

HOUSE = 'us_house_control'
REQUIRED = ('id', 'subtype', 'question_regex', 'window_days', 'entities', 'institution_terms', 'action_terms')


def matches(pattern, text):
    return re.search(pattern, text, re.IGNORECASE) is not None


# ---- profile loading -------------------------------------------------------------------------------------------

def term_pattern(term):
    """Whole-word, case-insensitive pattern. A trailing * allows any word ending ("resign*" -> resigns, resignation)."""
    term = str(term).strip()
    if not term or term == '*':
        raise ValueError('Empty relevance term')
    prefix = term.endswith('*')
    body = re.escape(term[:-1] if prefix else term)
    return re.compile(r'(?<!\w)' + body + ('' if prefix else r'(?!\w)'), re.IGNORECASE)


def compile_profile(raw):
    missing = [key for key in REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"Relevance profile {raw.get('id', '?')} is missing: {', '.join(missing)}")
    if raw['id'] == HOUSE:
        raise ValueError(f'Relevance profile id {HOUSE} is reserved')
    entities = [term_pattern(t) for t in raw['entities'] or []]
    institution = [term_pattern(t) for t in raw['institution_terms'] or []]
    action = [term_pattern(t) for t in raw['action_terms'] or []]
    if not entities:
        raise ValueError(f"Relevance profile {raw['id']} needs at least one entity")
    if not (institution or action):
        raise ValueError(f"Relevance profile {raw['id']} needs institution or action terms")
    days = float(raw['window_days'])
    if days <= 0:
        raise ValueError(f"Relevance profile {raw['id']} needs a positive window_days")
    return dict(id=raw['id'], subtype=raw['subtype'], question=re.compile(raw['question_regex']),
                window_seconds=int(days * 86400), entities=entities, institution=institution, action=action)


def load_profiles(path=None):
    """Compile the profile file. A missing file means no profiles; a malformed one is an error, never ignored."""
    path = Path(path or RELEVANCE_PROFILES)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    profiles = [compile_profile(raw) for raw in data.get('profiles') or []]
    ids = [p['id'] for p in profiles]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate relevance profile ids')
    return profiles


@functools.lru_cache(maxsize=1)
def default_profiles():
    return tuple(load_profiles())


def matching_profile(snapshot, profiles=None):
    profiles = default_profiles() if profiles is None else profiles
    for profile in profiles:
        if profile['question'].search(snapshot['question']):
            return profile
    return None


# ---- gates -----------------------------------------------------------------------------------------------------

def relevance_profile(snapshot, profiles=None):
    q = snapshot['question']
    house = matches(r'\bhouse\b', q)
    control = matches(r'\b(control|majority|win)\b', q)
    us_context = matches(r'\b(democratic|republican|democrats|republicans|gop|midterms?|united states)\b|\bu\.?s\.?\b', q)
    if house and control and us_context:
        return HOUSE
    profile = matching_profile(snapshot, profiles)
    return profile['id'] if profile else None


def house_rejection(snapshot, document):
    title = document['title']
    # Inspect only the text actually sent to the agent, not hidden/truncated text.
    text = title + ' ' + document['body']
    explicit_house = matches(r'\b(?:u\.?s\.?|united states|american)\s+house\b', text)
    chamber = matches(r'\bhouse(?:\s+of\s+representatives)?\b', text)
    us_context = matches(r'\b(democrats?|democratic|republicans?|gop|united states|american)\b|\bu\.?s\.?\b', text)
    electoral = matches(r'\b(elections?|midterms?|polls?|polling|ballot|seats?|majority|control|races?|campaigns?)\b', text)
    if not (explicit_house or (chamber and us_context)):
        return 'missing_us_house_context'
    if not electoral:
        return 'missing_election_context'
    target_years = set(re.findall(r'\b20\d{2}\b', snapshot['question']))
    headline_cycles = set(re.findall(
        r'\b(20\d{2})\s+(?:(?:us|u\.s\.|american|congressional|house|midterm)\s+){0,3}elections?\b',
        title, re.IGNORECASE))
    if target_years and headline_cycles and not target_years.intersection(headline_cycles):
        return 'different_headline_election_year'
    return None


def profile_rejection(profile, snapshot, document):
    """One reason per rejected document, checked in a fixed order; None means accepted."""
    cutoff, published = int(snapshot['cutoff']), int(document['published_at'])
    if published > cutoff:
        return 'after_cutoff'
    if published < cutoff - profile['window_seconds']:
        return 'outside_window'
    # Inspect only the text actually sent to the agent (the retriever truncates the body before this gate).
    text = document['title'] + ' ' + document['body']
    if not any(p.search(text) for p in profile['entities']):
        return 'no_entity'
    if not any(p.search(text) for p in profile['institution'] + profile['action']):
        return 'no_institution_or_action'
    return None


def rejection_reason(snapshot, document, profiles=None):
    profile = relevance_profile(snapshot, profiles)
    if profile is None:
        return None
    if profile == HOUSE:
        return house_rejection(snapshot, document)
    return profile_rejection(matching_profile(snapshot, profiles), snapshot, document)


def require_relevant_evidence(snapshot, documents, profiles=None):
    profile = relevance_profile(snapshot, profiles)
    if profile and not documents:
        if profile == HOUSE:
            raise ValueError('insufficient_relevant_evidence: no eligible US House election evidence; no LLM calls made for this snapshot')
        raise ValueError(f'insufficient_relevant_evidence: no eligible evidence for profile {profile}; no LLM calls made for this snapshot')
