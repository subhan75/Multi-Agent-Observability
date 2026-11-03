"""The thing being evaluated: our real support graph, run in-process (no web server involved).

NOTE: the order agent's tool calls the orders service over HTTP, so the orders and inventory services
must be running (scripts/run_all.ps1) while you evaluate. Gemini is called for real, so a run costs
a few API calls per question.
"""
from dotenv import load_dotenv

load_dotenv()  # the Gemini key (and LangSmith settings) come from .env, exactly as in the services

from services.agents import prompts  # noqa: E402
from services.agents.graph import build_graph, current_model_name  # noqa: E402

_graph = None


def run_support_graph(inputs: dict) -> dict:
    """Run one question through the graph and return the answer plus what the evaluators need to see."""
    global _graph
    if _graph is None:
        _graph = build_graph()  # compiled once, then shared by all (thread-safe) calls
    final = _graph.invoke({"question": inputs["question"]})
    return {
        "answer": final["answer"],
        "steps": final.get("steps", 0),
        "visited": final.get("visited", []),        # which workers ran
        "tools_used": final.get("tools_used", []),  # which tools were called
        "order_info": final.get("order_info", ""),  # what the agents retrieved: the judge compares the answer to this
        "policy_info": final.get("policy_info", ""),
    }


def describe_setup() -> dict:
    """What was being tested: recorded with every experiment so results can be compared later."""
    return {"model": current_model_name(), "prompt_version": prompts.PROMPT_VERSION}
