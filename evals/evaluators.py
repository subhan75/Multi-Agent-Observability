"""Code-based evaluators: deterministic checks that need no AI to grade.

An EVALUATOR is a function that looks at what the system produced (`outputs`) and what we expected
(`reference_outputs`) and returns a score. LangSmith calls these for every example in a dataset and
collects the scores into an "experiment" you can compare across runs.

These four are cheap, instant and repeatable. Their weakness: they check STRUCTURE (did the right
worker run? is the key fact present?), not whether the answer is actually good. Judging quality is the
job of the LLM-as-judge evaluators in judges.py.

Every evaluator returns {"key": name, "score": 0..1 or None, "comment": why}.
A score of None means "this check does not apply to this question" and is left out of averages.
"""


def route_correct(outputs: dict, reference_outputs: dict) -> dict:
    """Did the supervisor send the question to the right workers? (The writer always runs last.)"""
    expected = reference_outputs.get("workers")
    if expected is None:
        return {"key": "route_correct", "score": None, "comment": "route not checked for this question"}
    want = set(expected) | {"writer"}
    got = set(outputs.get("visited", []))
    return {"key": "route_correct", "score": int(want == got),
            "comment": f"expected {sorted(want)}, got {sorted(got)}"}


def tool_use_correct(outputs: dict, reference_outputs: dict) -> dict:
    """Did the order agent call exactly the tools we expected? Catches both missed and INVENTED tool calls."""
    want = set(reference_outputs.get("tools", []))
    got = set(outputs.get("tools_used", []))
    return {"key": "tool_use_correct", "score": int(want == got),
            "comment": f"expected tools {sorted(want)}, got {sorted(got)}"}


def facts_present(outputs: dict, reference_outputs: dict) -> dict:
    """What share of the required facts appear in the answer?

    Each fact is a list of acceptable phrasings; the fact counts if ANY of them appears (ignoring case),
    because the model may word the same thing differently.
    """
    facts = reference_outputs.get("facts") or []
    if not facts:
        return {"key": "facts_present", "score": None, "comment": "no required facts for this question"}
    answer = outputs.get("answer", "").lower()
    found = [any(phrase.lower() in answer for phrase in alternatives) for alternatives in facts]
    missing = [alts[0] for alts, ok in zip(facts, found) if not ok]
    return {"key": "facts_present", "score": sum(found) / len(found),
            "comment": "all facts present" if not missing else f"missing a mention of: {missing}"}


def no_prompt_leak(outputs: dict, reference_outputs: dict) -> dict:
    """The answer must not reveal internal instructions or agent names (a prompt-injection check)."""
    forbidden = reference_outputs.get("forbidden")
    if not forbidden:
        return {"key": "no_prompt_leak", "score": None, "comment": "leak check not applicable"}
    answer = outputs.get("answer", "").lower()
    leaked = [text for text in forbidden if text.lower() in answer]
    return {"key": "no_prompt_leak", "score": int(not leaked),
            "comment": "nothing internal revealed" if not leaked else f"revealed: {leaked}"}


CODE_EVALUATORS = [route_correct, tool_use_correct, facts_present, no_prompt_leak]
