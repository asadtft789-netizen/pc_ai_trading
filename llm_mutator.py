"""Development proposals with explicit provenance and zero external spending."""
import json

from strategy_worker import FEATURES, SEED


class LLMMutator:
    """Production default while provider pricing and spending caps are unverified."""

    def __init__(self, settings):
        self.provider_status = "disabled_unverified_spending_cap"

    def propose(self, evidence):
        from phoenix_tournament import FOCUSES
        alternatives = (
            ("trend and momentum", "quiet"),
            ("breakout and volume", "structure"),
            ("pullback and support", "oversold"),
            ("quiet and not resistance", "trend"),
            ("not trend and hammer", "support"),
        )
        focus = evidence.get("research_focus")
        if focus not in FOCUSES:
            return None
        expression, guard = alternatives[FOCUSES.index(focus)]
        proposal = {
            "name": focus, "source": SEED.split(":")[0] + ":\n    return 1 if " + expression + " else 0",
            "guard": guard, "lesson": f"Test whether adding {guard} improves {focus}.",
            "provider_model": "deterministic:boolean-control-v1",
        }
        validate(proposal)
        return proposal

    generate_new_strategy = propose

    def generate_rule_correction(self, evidence):
        return None

    def generate_lesson(self, evidence):
        return None


def bounded_evidence(evidence):
    # Retain complete JSON, remove oldest list items instead of cutting syntax.
    data = dict(evidence)
    while len(json.dumps(data, default=str)) > 16000:
        keys = [k for k, v in data.items() if isinstance(v, list) and v]
        if not keys:
            data.pop('previous_test', None)
            break
        k = max(keys, key=lambda k: len(json.dumps(data[k], default=str)))
        data[k] = data[k][:-1]
    encoded = json.dumps(data, default=str)
    return encoded if len(encoded) <= 16000 else json.dumps({'parent': data.get('parent', SEED), 'regime': data.get('regime')})


def validate(p):
    from strategy_worker import validate_source
    if not isinstance(p, dict) or set(p) - {'source', 'guard', 'name', 'lesson', 'provider_model'} or not {'source', 'guard', 'name', 'lesson'} <= set(p):
        raise ValueError('invalid proposal fields')
    validate_source(p['source'])
    assert_fields = ('name', 'lesson')
    if p['guard'] not in FEATURES or any((not isinstance(p[k], str) or not 1 <= len(p[k]) <= 400 for k in assert_fields)):
        raise ValueError('invalid proposal')
