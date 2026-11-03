"""LLM-as-judge evaluators: a second model grades the first model's answers.

Some qualities cannot be checked with code: "is this polite?", "did it make something up?". So we ask a
(cheap, fast) model to grade, following a written rubric, and to explain its verdict. This is very
common in practice, with a few honest caveats:
  * the judge is itself an AI and can be wrong: read the `comment` on surprising scores;
  * use a DIFFERENT, smaller model than the one being graded where possible;
  * a judge with a clear, narrow rubric (like these) is far more reliable than a vague one.

The judge model is set by the JUDGE_MODEL environment variable.
"""
import os

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

DEFAULT_JUDGE_MODEL = "gemini-3.5-flash-lite"


class Verdict(BaseModel):
    """What the judge must return. `reasoning` comes first so the model thinks before it decides."""
    reasoning: str = Field(description="One or two sentences explaining the verdict.")
    passed: bool = Field(description="True if the reply meets the rubric.")


TONE_RUBRIC = """You are grading a reply written by a customer-support assistant.

PASS only if ALL of these are true:
- It is polite and professional.
- It is concise: roughly 120 words or fewer, with no rambling.
- It speaks to what the customer actually asked.

Customer question: {question}

Reply to grade:
{reply}"""

GROUNDED_RUBRIC = """You are checking a customer-support reply for MADE-UP information (hallucination).

You are given the CONTEXT the assistant had (order data and policy text) and its REPLY.
Check every factual claim in the reply (order status, quantities, stock levels, policy rules, numbers,
days) against the context.

PASS if every factual claim is supported by the context.
PASS if the reply honestly says it lacks some information, or asks the customer for more details.
FAIL if the reply states ANY fact that is not in the context, contradicts it, or invents details
(for example an order status, a tracking number, or a policy that was not provided).
Greetings and politeness are not factual claims.

CONTEXT
Order information: {order_info}
Policy information: {policy_info}

Customer question: {question}

REPLY
{reply}"""


def _judge():
    model = os.getenv("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    return ChatGoogleGenerativeAI(model=model, temperature=0).with_structured_output(Verdict)


def _grade(key: str, prompt: str) -> dict:
    try:
        verdict = _judge().invoke(prompt)
    except Exception as exc:  # noqa: BLE001 - a flaky judge must not crash the whole evaluation run
        return {"key": key, "score": None, "comment": f"judge failed: {type(exc).__name__}"}
    return {"key": key, "score": int(verdict.passed), "comment": verdict.reasoning}


def tone_ok(inputs: dict, outputs: dict) -> dict:
    """Polite, professional, concise, on-topic."""
    return _grade("tone_ok", TONE_RUBRIC.format(question=inputs["question"], reply=outputs["answer"]))


def grounded(inputs: dict, outputs: dict) -> dict:
    """No invented facts: every claim in the reply is supported by what the agents retrieved."""
    return _grade("grounded", GROUNDED_RUBRIC.format(
        question=inputs["question"], reply=outputs["answer"],
        order_info=outputs.get("order_info") or "(none retrieved)",
        policy_info=outputs.get("policy_info") or "(none retrieved)"))


JUDGE_EVALUATORS = [tone_ok, grounded]
