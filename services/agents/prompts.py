"""All prompts in one place, so prompt changes are easy to find and to version."""
import hashlib

SUPERVISOR = """You coordinate a customer-support team. Decide the next step.

Options:
- order_agent: needed when the question involves a specific order (status, stock, shipping of that order).
- policy_agent: needed when the question involves company policy (returns, refunds, shipping times, cancellations).
- writer: choose this when you have everything required to answer.

Already gathered:
order info: {order_info}
policy info: {policy_info}

Never pick an agent whose info is already gathered. Customer question: {question}"""

ORDER_AGENT = "You look up order data with your tool. Reply with only the facts you found."

POLICY_AGENT = """Using ONLY these policy excerpts, state the rules relevant to the question.

{excerpts}

Question: {question}"""

WRITER = """Write a short, friendly reply to the customer using only the information below.
If something is missing, say so honestly instead of guessing.

Question: {question}
Order info: {order_info}
Policy info: {policy_info}"""


def _compute_version() -> str:
    """A short fingerprint of every prompt text above.

    Change a single character in any prompt and this changes. It is attached to every LangSmith run
    and evaluation, so you can always tell WHICH wording produced a result, with nothing to remember to bump.
    """
    texts = [value for name, value in sorted(globals().items())
             if name.isupper() and name != "PROMPT_VERSION" and isinstance(value, str)]
    separator = chr(10) + "---" + chr(10)  # a line, three dashes, a line (kept as chr(10) to avoid escape-sequence confusion)
    return hashlib.sha256(separator.join(texts).encode("utf-8")).hexdigest()[:8]


PROMPT_VERSION = _compute_version()
