// Plain-English descriptions of each kind of step, and a list of which metrics each kind of step produces.
// The metric names are the real ones defined in services/common/metrics.py, shown so you can connect
// "this step happened" with "these numbers moved in Prometheus".

import { NODES } from "./nodes.js";

const label = (id) => NODES[id]?.label ?? id;
const attr = (span, key) => span.attrs[key];

/** { title, plain, result }: a one-line title, a sentence of explanation, and (when known) the step's outcome. */
export function describe(span) {
  const r = span.route;
  const a = span.attrs;

  // ---- calls between components --------------------------------------------------------------------
  if (r && span.kind === "SERVER" && r.from === "browser") {
    return { title: `Browser → Gateway: ${r.label}`,
      plain: "The browser sends the question to the gateway, the public front door of the system. OpenTelemetry starts the trace here and gives it its trace id." };
  }
  if (r && span.kind === "CLIENT" && r.to === "gemini") {
    const agent = label(r.from);
    return { title: `${agent} → Gemini API: ${r.label}`,
      plain: `The real network request to Google's servers: the prompt goes out over HTTPS and the model's reply comes back. Almost all of a question's waiting time is spent here, because the model "thinks" before answering.` };
  }
  if (r && span.kind === "CLIENT" && a["db.system"] === undefined) {
    return { title: `${label(r.from)} → ${label(r.to)}: ${r.label}`,
      plain: `An HTTP call between two services. The caller adds the trace headers (traceparent and baggage), so the next service's spans join this same trace.` };
  }
  if (r && a["db.system"]) {
    return { title: `${label(r.from)} → ${label(r.to)}: ${r.label}`,
      plain: `A SQL query against a SQLite database file: ${a["db.statement"] ?? ""}. The ? is a placeholder, so the value is passed safely as data, never pasted into the SQL text.` };
  }
  if (span.name.startsWith("agent.")) {
    const node = span.name.slice(6);
    const result = node === "supervisor" && a["shopsense.next_step"]
      ? `decided: next → ${a["shopsense.next_step"]}${a["shopsense.guardrail_override"] ? " (code guardrail overrode the model)" : ""}` : undefined;
    const plain = {
      supervisor: "The LangGraph supervisor reads the question and what the workers have gathered so far, then asks Gemini which worker should go next (or whether to write the answer).",
      order_agent: "A small LangChain agent. Gemini decides to call the get_order tool, our code runs it, and Gemini then summarises the result. That is two model calls with a tool call between them.",
      policy_agent: "Looks up the relevant policy text, then asks Gemini to state the rules that apply to the question.",
      writer: "Gemini writes the final, friendly reply using only the information the other workers gathered.",
    }[node];
    return { title: `${label("agents")} → ${label(node)}: run`, plain, result };
  }
  if (span.name.startsWith("tool.")) {
    return { title: `${label("order_agent")} → ${label("tool")}: ${span.name.slice(5)}`,
      plain: "The model cannot run code itself. It asks for a tool by name, and our own Python function runs, here by calling the Orders service." };
  }
  if (span.name === "kb.retrieve") {
    return { title: `${label("policy_agent")} → ${label("kb")}: retrieve`,
      plain: "Finds the policy sections that share the most words with the question. Plain word matching, no AI involved.",
      result: a["shopsense.sections_returned"] !== undefined ? `${a["shopsense.sections_returned"]} sections returned` : undefined };
  }

  // ---- work inside a component (not a call) -------------------------------------------------------
  if (span.kind === "SERVER") {
    return { title: `${label(span.actor)} handles ${a["http.method"] ?? ""} ${a["http.route"] ?? a["http.target"] ?? ""}`.trim(),
      plain: `The receiving end of the call above. The service's web framework creates this span automatically, and it contains everything the service does to answer.`,
      result: a["http.status_code"] !== undefined ? `HTTP ${a["http.status_code"]}` : undefined };
  }
  if (span.name === "gemini.generate") {
    return { title: `${label(span.actor)}: model call`,
      plain: "Our own code wrapped around one Gemini request. It records the model, token counts and estimated cost on the span (using OpenTelemetry's standard gen_ai.* names).",
      result: a["gen_ai.usage.input_tokens"] !== undefined
        ? `${a["gen_ai.usage.input_tokens"]} tokens in, ${a["gen_ai.usage.output_tokens"]} out` : undefined };
  }
  return { title: span.name, plain: "A step recorded by OpenTelemetry." };
}

/** Which Prometheus metrics this kind of step updates (the numbers you would see move in Grafana). */
export function metricsFor(span) {
  const a = span.attrs;
  if (a["db.system"]) return [];
  if (span.kind === "SERVER") {
    return ["http_requests_total  (method, route, status)", "http_request_duration_seconds  (histogram)", "http_requests_in_flight  (up while the request runs)"];
  }
  if (span.name.startsWith("agent.")) {
    const node = span.name.slice(6);
    const base = [`agent_node_runs_total  (node="${node}")`, `agent_node_duration_seconds  (node="${node}")`];
    return node === "supervisor" ? [...base, "supervisor_decisions_total  (choice, guardrail_override)"] : base;
  }
  if (span.name === "gemini.generate") {
    return ["llm_calls_total", "llm_call_duration_seconds", "llm_tokens_total  (input / output)", "llm_cost_usd_total"];
  }
  if (span.name.startsWith("tool.")) return ["agent_tool_calls_total  (tool, outcome)"];
  return [];
}

/** Which other tools received data from this step, shown in the drawer's "telemetry produced" section. */
export function sinksFor(span) {
  const sinks = ["Tempo: this span, in the trace"];
  if (span.name.startsWith("agent.") || span.name === "gemini.generate" || span.name.startsWith("tool.") || span.name === "kb.retrieve") {
    sinks.push("LangSmith: part of the run tree (when tracing is on)");
  }
  return sinks;
}
