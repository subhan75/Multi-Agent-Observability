// The detail drawer: everything known about ONE selected step. Starts with a plain-English explanation,
// then timing, the facts OpenTelemetry recorded, the details our own code attached (prompts, tool
// arguments, graph state), and finally "what telemetry did this step produce?" (the span, metrics, logs).

import { NODES } from "./nodes.js";
import { describe, metricsFor, sinksFor } from "./describe.js";
import { stateAt } from "./time.js";
import { clip, copy, fmtInt, fmtMs, fmtUsd, h, pretty } from "./util.js";

const FACT_LABELS = {
  "http.method": "method", "http.url": "URL", "http.route": "route", "http.status_code": "status",
  "db.system": "database", "db.statement": "SQL",
  "gen_ai.request.model": "model", "gen_ai.provider.name": "provider", "gen_ai.usage.input_tokens": "tokens in",
  "gen_ai.usage.output_tokens": "tokens out", "gen_ai.request.temperature": "temperature",
  "shopsense.cost_usd": "est. cost (USD)", "shopsense.agent": "asked by", "shopsense.next_step": "decision",
  "shopsense.loop_count": "supervisor loop #", "shopsense.guardrail_override": "guardrail overrode?",
  "shopsense.sections_returned": "sections returned", "shopsense.llm_calls": "model calls", "shopsense.prompt_version": "prompt version",
  "langsmith.run_id": "LangSmith run",
};

export function renderDrawer(container, model, ui) {
  const span = ui.selectedId ? model.derived().byId.get(ui.selectedId) : null;
  if (!span) {
    container.replaceChildren(
      h("h2", {}, "Step details"),
      h("div", { class: "empty" }, h("h3", {}, "Click any step"),
        h("p", {}, "Select a box, an arrow or a bar to see what it is, how long it took, what it received and produced, and which telemetry it generated.")));
    return;
  }
  const { spans } = model.derived();
  const d = describe(span);
  const group = NODES[span.actor]?.group ?? "client";
  const state = stateAt(span, ui.T);
  const end = span.end ?? ui.T;
  const durMs = (end - span.start) * 1000;
  const total = Math.max(((model.derived().tEnd ?? span.start) - model.derived().t0) * 1000, 1);
  const idx = spans.indexOf(span);

  const pill = state === "error" ? h("span", { class: "pill err" }, "error")
    : state === "running" ? h("span", { class: "pill run" }, "running") : h("span", { class: "pill ok" }, "ok");

  container.replaceChildren(
    h("div", { class: `head g-${group}` },
      h("div", { class: "kicker" }, h("span", { class: "dot" }), span.step ? `Step ${span.step}` : "Inside a step", " · ", NODES[span.actor]?.label ?? span.service, pill),
      h("h3", {}, d.title),
      d.result ? h("div", {}, d.result) : null),
    h("div", { class: "plain" }, d.plain),
    h("div", { class: "nav" },
      h("button", { class: "btn small", onclick: () => ui.onSelect(spans[Math.max(idx - 1, 0)].id), disabled: idx === 0 }, "◀ previous"),
      h("button", { class: "btn small", onclick: () => ui.onSelect(spans[Math.min(idx + 1, spans.length - 1)].id), disabled: idx >= spans.length - 1 }, "next ▶"),
      span.parentId ? h("button", { class: "btn small", onclick: () => ui.onSelect(span.parentId) }, "▲ parent") : null),

    timingSection(span, group, durMs, total, model),
    factsSection(span),
    errorsSection(span),
    detailsSection(span),
    telemetrySection(span, model),
  );
}

function timingSection(span, group, durMs, total, model) {
  const t0 = model.derived().t0;
  const left = ((span.start - t0) * 1000 / total) * 100, width = Math.max((durMs / total) * 100, 0.8);
  return h("div", { class: `sect g-${group}` }, h("h4", {}, "Timing"),
    h("table", { class: "kv" },
      h("tr", {}, h("td", {}, "starts at"), h("td", {}, `+${fmtMs((span.start - t0) * 1000)} after the request began`)),
      h("tr", {}, h("td", {}, "duration"), h("td", {}, span.end === null ? "still running…" : fmtMs(durMs))),
      h("tr", {}, h("td", {}, "share of request"), h("td", {}, `${((durMs / total) * 100).toFixed(durMs / total < 0.1 ? 1 : 0)}%`))),
    h("div", { class: "bar", title: "where this step sits in the whole request" }, h("i", { style: `left:${left}%;width:${width}%` })));
}

function factsSection(span) {
  const rows = Object.entries(span.attrs).filter(([k]) => FACT_LABELS[k] !== undefined)
    .map(([k, v]) => h("tr", {}, h("td", {}, FACT_LABELS[k]), h("td", { class: k === "db.statement" || k === "http.url" ? "mono" : "" }, k === "shopsense.cost_usd" ? fmtUsd(v) : String(v))));
  if (!rows.length) return null;
  return h("div", { class: "sect" }, h("h4", {}, "Recorded by OpenTelemetry (span attributes)"), h("table", { class: "kv" }, ...rows));
}

function errorsSection(span) {
  if (span.status !== "ERROR") return null;
  return h("div", { class: "sect" }, h("h4", {}, "Error"),
    ...(span.exceptions.length ? span.exceptions.map((e) => h("div", {}, h("b", {}, e.type || "Exception"), ": ", e.message))
      : [h("div", {}, span.statusMessage || "This step ended with an error status.")]));
}

/** Details our own code attached to this exact span: prompts and replies, tool arguments, graph state. */
function detailsSection(span) {
  const parts = [];
  for (const ev of span.details) {
    const p = ev.payload;
    if (ev.kind === "llm_call") {
      parts.push(h("details", { class: "d", open: span.details.length <= 2 },
        h("summary", {}, `Model call · ${fmtInt(p.input_tokens)} tokens in, ${fmtInt(p.output_tokens)} out · ${fmtUsd(p.cost_usd)}`
          + (p.requested_tools?.length ? ` · asks for tool ${p.requested_tools.join(", ")}` : "")),
        h("pre", {}, "PROMPT\n" + (p.prompt || "(none)") + "\n\nREPLY\n" + (typeof p.response === "string" ? p.response : pretty(p.response)))));
    } else if (ev.kind === "tool_call") {
      parts.push(h("details", { class: "d", open: true }, h("summary", {}, `Tool ${p.tool}(${Object.values(p.args || {}).join(", ")})`),
        h("pre", {}, "ARGUMENTS\n" + pretty(p.args) + "\n\nRESULT\n" + (p.result ?? ""))));
    } else if (ev.kind === "retrieval") {
      parts.push(h("details", { class: "d", open: true }, h("summary", {}, `Retrieved ${p.sections?.length ?? 0} policy sections`), h("pre", {}, (p.excerpts || p.sections || []).join("\n\n"))));
    } else if (ev.kind === "step_start") {
      parts.push(h("details", { class: "d" }, h("summary", {}, span.name.startsWith("agent.") ? "What this step received (LangGraph state)" : "Input"), h("pre", {}, pretty(p))));
    } else if (ev.kind === "step_end") {
      parts.push(h("details", { class: "d" }, h("summary", {}, "What this step produced"), h("pre", {}, pretty(p))));
    } else if (ev.kind === "db_read") {
      parts.push(h("details", { class: "d", open: true }, h("summary", {}, "Row returned"), h("pre", {}, p.summary || pretty(p))));
    }
  }
  return parts.length ? h("div", { class: "sect" }, h("h4", {}, "Details from our own code"), ...parts) : null;
}

/** The observability lesson: what did this one step leave behind in each tool? */
function telemetrySection(span, model) {
  const idRow = (label, value) => h("div", { class: "idrow" }, h("span", { class: "muted" }, label), h("span", {}, clip(value, 40)),
    h("button", { class: "btn small", title: "copy", onclick: (e) => copy(value).then(() => (e.target.textContent = "✓")) }, "copy"));
  const metrics = metricsFor(span), logs = model.logsFor(span.id), sinks = sinksFor(span);
  return h("div", { class: "sect" }, h("h4", {}, "Telemetry this step produced"),
    h("div", {}, h("b", {}, "Trace (Tempo): "), "this step is one span"),
    idRow("span id", span.id), idRow("trace id", model.traceId || ""),
    h("div", { style: "margin-top:8px" }, h("b", {}, "Metrics (Prometheus): ")),
    metrics.length ? h("ul", { class: "mono", style: "margin:2px 0 0 18px;padding:0" }, ...metrics.map((m) => h("li", {}, m)))
      : h("div", { class: "muted" }, "none: this kind of step is only visible in traces (and logs)."),
    h("div", { style: "margin-top:8px" }, h("b", {}, "Logs (Loki): "), model.logs === null ? h("span", { class: "muted" }, "loading…")
      : logs.length ? `${logs.length} line${logs.length > 1 ? "s" : ""} written during this step` : h("span", { class: "muted" }, "no log lines for this step")),
    ...logs.map((l) => h("div", { class: "logline" }, h("span", { class: `lvl ${l.level}` }, String(l.level).toUpperCase()), l.message, " ",
      h("span", { class: "muted" }, Object.entries(l.fields).map(([k, v]) => `${k}=${v}`).join(" ")))),
    sinks.length > 1 ? h("div", { style: "margin-top:8px" }, h("b", {}, "Also sent to: "), sinks.slice(1).join("; ")) : null);
}
