"""Conservative, auditable topic gates.

Two kinds of gate, both scoped lexical rules rather than semantic relevance guarantees:
  * the original US House-control gate (hard-coded below);
  * the 2026 midterm election gate (hard-coded below; decisions log R4/R4a): Senate control, Balance of Power,
    and one gate per state Senate/Governor race;
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
    if raw['id'] in (HOUSE, SENATE_CONTROL, BALANCE) or str(raw['id']).startswith('election_'):
        raise ValueError(f"Relevance profile id {raw['id']} is reserved")
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


# ---- election gate (B11; decisions log R4 + R4a, 2026-11-18) ---------------------------------------------------
# The 2026 midterm replay markets have no hand-written profile. Party markets ("Will the Democrats win the Georgia
# Senate race in 2026?") carry no nominee names, so a state race is matched on full state name + office; the only
# candidate names are those in the eligible markets' own questions (retrospective/selection.csv, see the tests).

SENATE_CONTROL = 'us_senate_control'
BALANCE = 'us_balance_of_power'
ELECTION_YEAR = '2026'
RACE_WINDOW_DAYS = 30   # same window as the pilot profiles and dataset.lookback_days
STATES = ('Alabama', 'Alaska', 'Arizona', 'Arkansas', 'California', 'Colorado', 'Connecticut', 'Delaware', 'Florida',
          'Georgia', 'Hawaii', 'Idaho', 'Illinois', 'Indiana', 'Iowa', 'Kansas', 'Kentucky', 'Louisiana', 'Maine',
          'Maryland', 'Massachusetts', 'Michigan', 'Minnesota', 'Mississippi', 'Missouri', 'Montana', 'Nebraska',
          'Nevada', 'New Hampshire', 'New Jersey', 'New Mexico', 'New York', 'North Carolina', 'North Dakota', 'Ohio',
          'Oklahoma', 'Oregon', 'Pennsylvania', 'Rhode Island', 'South Carolina', 'South Dakota', 'Tennessee', 'Texas',
          'Utah', 'Vermont', 'Virginia', 'Washington', 'West Virginia', 'Wisconsin', 'Wyoming')
RACE_CANDIDATES = {('California', 'governor'): ('Steve Hilton', 'Xavier Becerra'),
                   ('Idaho', 'senate'): ('Todd Achilles',),
                   ('South Dakota', 'senate'): ('Brian Bengs',)}
OFFICE_TERMS = {'senate': [term_pattern(t) for t in ('Senate', 'senator*', 'senatorial', 'Sen.')],
                'governor': [term_pattern(t) for t in ('governor*', 'gubernatorial', 'Gov.')]}
# Deliberately no "vote"/"votes"/"debates": "Texas Senate votes on (debates) a bill" is the state legislature.
ELECTION_TERMS = [term_pattern(t) for t in (
    'election*', 'race', 'races', 'campaign*', 'candidate*', 'nominee*', 'poll', 'polls', 'polling', 'pollster*',
    'ballot*', 'voters', 'midterm*', 'debate', 'rally', 'rallies', 'early voting', 'turnout')]
RACE_QUESTION = re.compile(r'^Will (?P<who>.+?) win the (?P<state>[A-Z][A-Za-z ]+?) (?P<office>Senate|governor|Governor) '
                           r'(?:race|Election) in (?P<year>20\d{2})\?$')
CONTROL_QUESTION = re.compile(r'\bcontrol the Senate after the 2026 Midterm elections\?$', re.IGNORECASE)
BALANCE_QUESTION = re.compile(r'^2026 Balance of Power: [DR] Senate, [DR] House$')
YEAR_NEAR_ELECTION = (r'\b(20\d{2})\s+(?:[\w.-]+\s+){0,3}(?:elections?|races?|midterms?|campaigns?)\b',
                      r'\b(?:elections?|races?|midterms?|campaigns?)\s+(?:of|in)\s+(20\d{2})\b')


def election_race(question):
    """(state, office, candidates) for a 2026 Senate/Governor race market, else None."""
    m = RACE_QUESTION.match(question.strip())
    if not m or m['state'] not in STATES or m['year'] != ELECTION_YEAR:
        return None
    office = m['office'].lower()
    return m['state'], office, RACE_CANDIDATES.get((m['state'], office), ())


def other_election_year(title):
    years = {y for pattern in YEAR_NEAR_ELECTION for y in re.findall(pattern, title, re.IGNORECASE)}
    return bool(years - {ELECTION_YEAR})


def race_rejection(race, snapshot, document):
    """One reason per rejected document, checked in the fixed order of decisions-log row R4a; None = admitted."""
    state, office, candidates = race
    cutoff, published = int(snapshot['cutoff']), int(document['published_at'])
    if published > cutoff or int(document.get('available_at', published)) > cutoff:
        return 'after_cutoff'
    if published < cutoff - RACE_WINDOW_DAYS * 86400:
        return 'outside_window'
    text = document['title'] + ' ' + document['body']   # only what the agent would see
    by_candidate = any(term_pattern(name).search(text) for name in candidates)
    if not (by_candidate or term_pattern(state).search(text)):
        return 'no_race_entity'
    if not by_candidate and not any(p.search(text) for p in OFFICE_TERMS[office]):
        return 'no_office'
    if not any(p.search(text) for p in ELECTION_TERMS):
        return 'no_election_context'
    if other_election_year(document['title']):
        return 'different_election_year'
    return None


def senate_rejection(snapshot, document):
    """US Senate control: the House gate with Senate in place of House."""
    title = document['title']
    text = title + ' ' + document['body']
    explicit = matches(r'\b(?:u\.?s\.?|united states|american)\s+senate\b', text)
    chamber = matches(r'\bsenate\b', text)
    us_context = matches(r'\b(democrats?|democratic|republicans?|gop|united states|american)\b|\bu\.?s\.?\b', text)
    electoral = matches(r'\b(elections?|midterms?|polls?|polling|ballot|seats?|majority|control|races?|campaigns?)\b', text)
    if not (explicit or (chamber and us_context)):
        return 'missing_us_senate_context'
    if not electoral:
        return 'missing_election_context'
    if other_election_year(title):
        return 'different_headline_election_year'
    return None


def balance_rejection(snapshot, document):
    """Balance of Power markets depend on both chambers: admitted if either chamber's gate admits."""
    house, senate = house_rejection(snapshot, document), senate_rejection(snapshot, document)
    if house is None or senate is None:
        return None
    if house == 'missing_us_house_context' and senate == 'missing_us_senate_context':
        return 'missing_us_congress_context'
    return senate if senate != 'missing_us_senate_context' else house


def election_profile(question):
    if CONTROL_QUESTION.search(question):
        return SENATE_CONTROL
    if BALANCE_QUESTION.match(question.strip()):
        return BALANCE
    race = election_race(question)
    return f'election_{race[1]}_{race[0].lower().replace(" ", "_")}' if race else None


# ---- gates -----------------------------------------------------------------------------------------------------

def relevance_profile(snapshot, profiles=None):
    q = snapshot['question']
    house = matches(r'\bhouse\b', q)
    control = matches(r'\b(control|majority|win)\b', q)
    us_context = matches(r'\b(democratic|republican|democrats|republicans|gop|midterms?|united states)\b|\bu\.?s\.?\b', q)
    if house and control and us_context:
        return HOUSE
    election = election_profile(q)
    if election:
        return election
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
    if profile == SENATE_CONTROL:
        return senate_rejection(snapshot, document)
    if profile == BALANCE:
        return balance_rejection(snapshot, document)
    if profile.startswith('election_'):
        return race_rejection(election_race(snapshot['question']), snapshot, document)
    return profile_rejection(matching_profile(snapshot, profiles), snapshot, document)


def require_relevant_evidence(snapshot, documents, profiles=None):
    profile = relevance_profile(snapshot, profiles)
    if profile and not documents:
        if profile == HOUSE:
            raise ValueError('insufficient_relevant_evidence: no eligible US House election evidence; no LLM calls made for this snapshot')
        raise ValueError(f'insufficient_relevant_evidence: no eligible evidence for profile {profile}; no LLM calls made for this snapshot')
