"""The multi-agent support graph (LangGraph).

        ┌──────────► order_agent ──┐
 START ─► supervisor ─► policy_agent ─┤  (each worker reports back to the supervisor)
        ▲   │                        │
        └───┼────────────────────────┘
            └─► writer ─► END

Every node reports what it received and produced via `step(...)` (see common/events.py),
which is what powers the live "request X-ray" UI.
"""
import logging
import os
import time
from typing import Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from services.agents import prompts
from services.agents.rag import retrieve
from services.agents.tools import get_order
from services.common.events import emit_event, step
from services.common.metrics import record_supervisor_decision, track_llm, track_node
from services.common.telemetry import get_tracer, set_span_attributes, traced

logger = logging.getLogger(__name__)

MAX_STEPS = 5  # safety net against supervisor loops
DEFAULT_MODEL = "gemini-4-argon"


class SupportState(TypedDict, total=False):
    """Shared memory every node reads from and writes to."""
    question: str
    order_info: str
    policy_info: str
    next_step: str
    last_step: str  # which worker ran most recently (used to draw the UI edge back to the supervisor)
    steps: int
    answer: str
    visited: list[str]      # which workers ran, in order (used by the evaluations and the X-ray)
    tools_used: list[str]   # which tools the order agent actually called


class Decision(BaseModel):
    """Structured output the supervisor must return."""
    next_step: Literal["order_agent", "policy_agent", "writer"]


def _model_name() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)


def _llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(model=_model_name(), temperature=0)


def _ask(parent: str, prompt: str, schema: type[BaseModel] | None = None):
    """Call Gemini once, reporting the prompt, response and latency as an event."""
    llm = _llm()
    started = time.perf_counter()
    # OpenTelemetry: one span per Gemini call, labelled with the model and which agent asked.
    with get_tracer().start_as_current_span("gemini.generate") as span:
        span.set_attribute("gen_ai.request.model", _model_name())
        span.set_attribute("shopsense.agent", parent)
        with track_llm(_model_name(), parent):  # Prometheus: count + time this call, even if it fails
            if schema is None:
                result = llm.invoke(prompt)
                response = result.text  # `.text` flattens Gemini's content blocks into a plain string
            else:
                result = llm.with_structured_output(schema).invoke(prompt)
                response = result.model_dump()
    logger.info("llm call finished", extra={"agent": parent, "model": _model_name(),
                                           "duration_ms": round((time.perf_counter() - started) * 1000)})
    emit_event(
        "gemini", "llm_call",
        {"from": parent, "model": _model_name(), "prompt": prompt, "response": response,
         "summary": f"{_model_name()} ← {parent}"},
        caller=parent, duration_ms=(time.perf_counter() - started) * 1000,
    )
    return result


@track_node("supervisor")
@traced("agent.supervisor")
def supervisor(state: SupportState) -> SupportState:
    steps = state.get("steps", 0) + 1
    seen = {
        "question": state["question"],
        "order_info": state.get("order_info") or "(none)",
        "policy_info": state.get("policy_info") or "(none)",
        "loop_count": steps,
    }
    with step("supervisor", state.get("last_step") or "gateway", {"received": seen}) as out:
        if steps > MAX_STEPS:
            logger.warning("supervisor step limit reached, forcing writer", extra={"loop_count": steps})
            out.update(next_step="writer", summary="step limit reached → writer")
            return {"next_step": "writer", "steps": steps}
        decision = _ask("supervisor", prompts.SUPERVISOR.format(**seen), Decision)
        choice = decision.next_step
        # Guardrail: LLMs sometimes re-pick a worker whose job is already done. Enforce it in code.
        already_done = {"order_agent": bool(state.get("order_info")), "policy_agent": bool(state.get("policy_info"))}
        if already_done.get(choice):
            out["llm_choice"] = choice
            choice = "writer"
            out["guardrail"] = "LLM re-picked a finished agent → overridden to writer"
            logger.warning("guardrail overrode the supervisor", extra={"llm_choice": out["llm_choice"], "final_choice": choice})
        overridden = "guardrail" in out
        record_supervisor_decision(choice, overridden)
        logger.info("supervisor decided", extra={"next_step": choice, "loop_count": steps, "guardrail_override": overridden})
        # Attach the decision to this node's span, so it is visible in the trace.
        set_span_attributes({"shopsense.next_step": choice, "shopsense.guardrail_override": overridden,
                             "shopsense.loop_count": steps})
        out.update(next_step=choice, summary=f"next → {choice}" + (" (guardrail override)" if overridden else ""))
    return {"next_step": choice, "steps": steps}


@track_node("order_agent")
@traced("agent.order_agent")
def order_agent(state: SupportState) -> SupportState:
    with step("order_agent", "supervisor", {"question": state["question"]}) as out:
        agent = create_agent(_llm(), tools=[get_order], system_prompt=prompts.ORDER_AGENT)
        result = agent.invoke({"messages": [HumanMessage(state["question"])]})
        info = result["messages"][-1].text
        out.update(order_info=info, summary=info[:90])
        logger.info("order agent finished", extra={"answer_chars": len(info)})
    return {"order_info": info, "last_step": "order_agent", "visited": state.get("visited", []) + ["order_agent"]}


@track_node("policy_agent")
@traced("agent.policy_agent")
def policy_agent(state: SupportState) -> SupportState:
    with step("policy_agent", "supervisor", {"question": state["question"]}) as out:
        with get_tracer().start_as_current_span("kb.retrieve") as span:
            excerpts = retrieve(state["question"])
            span.set_attribute("shopsense.sections_returned", len(excerpts))
        logger.info("policy sections retrieved", extra={"sections_returned": len(excerpts)})
        emit_event("kb", "retrieval",
                   {"sections": [e.splitlines()[0] for e in excerpts], "summary": f"{len(excerpts)} policy sections"},
                   caller="policy_agent")
        prompt = prompts.POLICY_AGENT.format(excerpts="\n\n".join(excerpts), question=state["question"])
        info = _ask("policy_agent", prompt).text
        out.update(policy_info=info, summary=info[:90])
    return {"policy_info": info, "last_step": "policy_agent", "visited": state.get("visited", []) + ["policy_agent"]}


@track_node("writer")
@traced("agent.writer")
def writer(state: SupportState) -> SupportState:
    with step("writer", "supervisor", {"order_info": state.get("order_info"), "policy_info": state.get("policy_info")}) as out:
        prompt = prompts.WRITER.format(
            question=state["question"],
            order_info=state.get("order_info") or "(not looked up)",
            policy_info=state.get("policy_info") or "(not looked up)",
        )
        answer = _ask("writer", prompt).text
        out.update(answer=answer, summary=answer[:90])
        logger.info("writer produced the final answer", extra={"answer_chars": len(answer)})
    return {"answer": answer, "last_step": "writer", "visited": state.get("visited", []) + ["writer"]}


def build_graph():
    g = StateGraph(SupportState)
    g.add_node("supervisor", supervisor)
    g.add_node("order_agent", order_agent)
    g.add_node("policy_agent", policy_agent)
    g.add_node("writer", writer)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next_step"])  # route by the chosen name
    g.add_edge("order_agent", "supervisor")
    g.add_edge("policy_agent", "supervisor")
    g.add_edge("writer", END)
    return g.compile()
