// The trace model: turns the stream of events from the server into steps (spans), decides WHICH real
// component each step belongs to, and which steps are calls between components (the arrows).
//
// Two kinds of events arrive:
//   * span_start / span_end : what OpenTelemetry recorded, one pair per span. The source of truth.
//   * llm_call, tool_call, retrieval, step_start, step_end, db_read : extra DETAILS written by our own code
//     (prompts, replies, tool arguments, the graph's state). Each carries the id of the span it happened in,
//     so we can attach it to exactly the right step.
//
// This file has no DOM code, so it can be tested on its own with plain Node.

const SERVICE_NODE = { gateway: "gateway", agents: "agents", orders: "orders", inventory: "inventory" };
const PORT_NODE = { 8000: "gateway", 8001: "orders", 8002: "inventory", 8003: "agents" };
const DETAIL_KINDS = new Set(["llm_call", "tool_call", "retrieval", "step_start", "step_end", "db_read"]);

export class TraceModel {
  constructor() { this.reset(); }

  reset() {
    this.spans = new Map();          // span id -> span
    this.details = new Map();        // span id -> [detail events]
    this.requestId = null;
    this.traceId = null;
    this.question = "";
    this.result = null;              // the request_done payload (answer, tokens, cost, run id ...)
    this.error = null;
    this.feedback = [];
    this.logs = null;                // log lines fetched from Loki
    this.version = 0;                // bumps on every change, so views know when to redraw
    this._cache = null;
  }

  /** Feed one event from the server. */
  apply(ev) {
    this.version++;
    this._cache = null;
    this.requestId = this.requestId || ev.request_id;
    this.traceId = this.traceId || ev.otel_trace_id || null;

    if (ev.kind === "span_start" || ev.kind === "span_end") return this._applySpan(ev);
    if (ev.kind === "request_done") { this.result = ev.payload; this.traceId = ev.payload.trace_id || this.traceId; return; }
    if (ev.kind === "request_error") { this.error = ev.payload.error || "failed"; return; }
    if (ev.kind === "feedback") { this.feedback.push(ev.payload); return; }
    if (ev.kind === "step_start" && ev.component === "gateway" && ev.payload.question) this.question = ev.payload.question;
    if (DETAIL_KINDS.has(ev.kind) && ev.otel_span_id) {
      if (!this.details.has(ev.otel_span_id)) this.details.set(ev.otel_span_id, []);
      this.details.get(ev.otel_span_id).push(ev);
    }
  }

  _applySpan(ev) {
    const p = ev.payload;
    const span = this.spans.get(p.span_id) || { id: p.span_id, end: null, status: "RUNNING", exceptions: [], attrs: {} };
    span.parentId = p.parent_id || null;
    span.name = p.name;
    span.service = p.service;
    span.kind = p.span_kind;
    span.start = p.start_ns / 1e9;
    span.attrs = { ...span.attrs, ...p.attrs };
    if (ev.kind === "span_end") {
      span.end = p.end_ns / 1e9;
      span.status = p.status === "ERROR" ? "ERROR" : "OK";
      span.statusMessage = p.status_message || "";
      span.exceptions = p.exceptions || [];
    }
    this.spans.set(span.id, span);
  }

  get done() { return Boolean(this.result || this.error); }

  /**
   * Everything the views need, computed from the raw spans and cached until the next event:
   * ordered spans, parent/child links, the component each span belongs to, the arrows and their step numbers.
   */
  derived() {
    if (this._cache) return this._cache;
    // Order by start time. When two spans start at the same instant (a parent and its first child often do),
    // the one that lasts longer is the parent, so it must come first.
    const endOrLater = (s) => s.end ?? Number.MAX_SAFE_INTEGER;
    const spans = [...this.spans.values()].sort((a, b) => a.start - b.start || endOrLater(b) - endOrLater(a));
    const byId = new Map(spans.map((s) => [s.id, s]));
    for (const s of spans) { s.children = []; s.depth = 0; }
    const roots = [];
    for (const s of spans) {
      const parent = byId.get(s.parentId);
      if (parent) parent.children.push(s); else roots.push(s);
    }
    const setDepth = (s, d) => { s.depth = d; s.children.forEach((c) => setDepth(c, d + 1)); };
    roots.forEach((r) => setDepth(r, 0));

    const actorCache = new Map();
    const actorOf = (s) => {
      if (!s) return null;
      if (actorCache.has(s.id)) return actorCache.get(s.id);
      const parent = byId.get(s.parentId);
      let actor;
      if (s.attrs["db.system"]) actor = `${s.service}_db`;
      else if (s.kind === "SERVER") actor = SERVICE_NODE[s.service];
      else if (s.name.startsWith("agent.")) actor = s.name.slice(6);
      else if (s.name.startsWith("tool.")) actor = "tool";
      else if (s.name === "kb.retrieve") actor = "kb";
      else actor = actorOf(parent) || SERVICE_NODE[s.service];   // helper and client spans belong to whoever started them
      actorCache.set(s.id, actor);
      return actor;
    };

    for (const s of spans) {
      s.actor = actorOf(s);
      s.route = routeOf(s, byId.get(s.parentId), actorOf);
      s.step = null;
      s.details = this.details.get(s.id) || [];
    }
    // Step numbers go to the spans that are calls between components, in the order they began.
    let step = 0;
    for (const s of spans) if (s.route) s.step = ++step;

    const t0 = spans.length ? Math.min(...spans.map((s) => s.start)) : null;
    const tEnd = spans.length ? Math.max(...spans.map((s) => s.end ?? s.start)) : null;
    this._cache = { spans, byId, roots, t0, tEnd, steps: step };
    return this._cache;
  }

  /** Totals shown in the summary strip. */
  stats() {
    const { spans, t0, tEnd } = this.derived();
    const llmCalls = spans.filter((s) => s.kind === "CLIENT" && urlOf(s).includes("googleapis.com")).length;
    const r = this.result;
    return {
      spans: spans.length,
      services: new Set(spans.map((s) => s.service)).size,
      duration: t0 === null ? null : (tEnd - t0) * 1000,
      llmCalls: r?.llm_calls ?? llmCalls,
      inputTokens: r?.input_tokens ?? 0,
      outputTokens: r?.output_tokens ?? 0,
      cost: r?.cost_usd ?? 0,
      errors: spans.filter((s) => s.status === "ERROR").length,
    };
  }

  setLogs(logs) { this.logs = logs; this.version++; }
  logsFor(spanId) { return (this.logs || []).filter((l) => l.span_id === spanId); }
}

// ---- helpers: which component is called, and what the call is named -------------------------------

const urlOf = (s) => s.attrs["http.url"] || "";

function targetOfUrl(url) {
  if (url.includes("googleapis.com")) return "gemini";
  const port = url.match(/:(\d+)\//)?.[1];
  return PORT_NODE[port] || "gemini";
}

function pathOfUrl(url) {
  try {
    const u = new URL(url);
    const last = u.pathname.split("/").pop();
    return u.host.includes("googleapis.com") ? last.split(":").pop() : u.pathname;   // "...:generateContent" -> "generateContent"
  } catch { return url; }
}

function dbLabel(statement = "") {
  const verb = statement.trim().split(/\s+/)[0] || "query";
  const table = statement.match(/\bfrom\s+(\w+)/i)?.[1];
  return table ? `${verb} ${table}` : verb;
}

/** The call between components this span represents, or null if it isn't a call (just work inside one component). */
function routeOf(span, parent, actorOf) {
  const a = span.attrs;
  const callerOf = () => actorOf(parent) || SERVICE_NODE[span.service];
  if (a["db.system"]) return { from: callerOf(), to: `${span.service}_db`, label: dbLabel(a["db.statement"]) };
  if (span.kind === "CLIENT") {
    return { from: callerOf(), to: targetOfUrl(urlOf(span)), label: `${a["http.method"] || ""} ${pathOfUrl(urlOf(span))}`.trim() };
  }
  if (span.kind === "SERVER") {
    // A server span whose parent is a client span elsewhere is just the other end of THAT call: no new arrow.
    return parent ? null : { from: "browser", to: SERVICE_NODE[span.service], label: `${a["http.method"] || ""} ${a["http.route"] || a["http.target"] || ""}`.trim() };
  }
  const own = actorOf(span), caller = actorOf(parent);
  if (caller && own && caller !== own) {
    const label = span.name.startsWith("agent.") ? "run" : span.name === "kb.retrieve" ? "retrieve" : span.name.replace(/^tool\./, "");
    return { from: caller, to: own, label };
  }
  return null;
}
