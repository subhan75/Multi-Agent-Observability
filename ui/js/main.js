// Wires everything together: the question form, the live stream, history and replay, the playback clock,
// which step is selected, and which view is showing. The views themselves only DRAW; they never fetch.

import { TraceModel } from "./model.js";
import { renderArchitecture } from "./view-architecture.js";
import { renderSequence } from "./view-sequence.js";
import { renderWaterfall } from "./view-waterfall.js";
import { renderDrawer } from "./drawer.js";
import { describe } from "./describe.js";
import { stateAt } from "./time.js";
import { NODES } from "./nodes.js";
import { clip, fill, fmtInt, fmtMs, fmtUsd, h } from "./util.js";

const GRAFANA = "http://localhost:3000";
const VIEWS = { architecture: renderArchitecture, sequence: renderSequence, waterfall: renderWaterfall };
const $ = (id) => document.getElementById(id);

const model = new TraceModel();
const ui = { T: 0, selectedId: null, view: "sequence", showTelemetry: false, live: false, onSelect: select };
let currentId = null, source = null, liveTimer = null, replayFrame = null, playing = false, speed = 5, renderQueued = false, version = -1;

// ---------------------------------------------------------------------------------------- rendering
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

function bounds() {
  const { t0, tEnd } = model.derived();
  return { t0, tEnd };
}

function render() {
  const hasData = model.derived().spans.length > 0;
  $("stage").classList.toggle("hidden", false);
  if (!hasData) {
    $("stage").replaceChildren(h("div", { class: "empty" }, h("h3", {}, "Ask a question to watch it travel"),
      h("p", {}, "Every box, arrow and bar you will see is a real step that OpenTelemetry recorded: nothing is simulated or summarised."),
      h("p", { class: "muted" }, "Pick an example above or type your own, then press Send. Past requests appear on the left and can be replayed.")));
    $("drawer").replaceChildren(h("h2", {}, "Step details"), h("div", { class: "empty" }, h("p", {}, "Click any step once there is one.")));
    renderSummary(); renderCaption(); renderPlayback(); renderAnswer();
    return;
  }
  VIEWS[ui.view]($("stage"), model, ui);
  renderDrawer($("drawer"), model, ui);
  renderSummary(); renderCaption(); renderPlayback(); renderAnswer();
}

function renderSummary() {
  const st = model.stats(ui.live && !model.done ? ui.T : null);
  const status = model.error ? ["failed", "Failed"] : model.done ? ["done", "Finished"] : ui.live ? ["running", "Running…"] : ["idle", "Idle"];
  const stat = (label, value) => h("div", { class: "stat" }, h("b", {}, value), h("span", {}, label));
  const hasData = st.spans > 0;
  const links = [];
  if (model.traceId) {
    const pane = (ds, type, expr, queryType) => encodeURIComponent(JSON.stringify({ a: { datasource: ds, range: { from: "now-6h", to: "now" },
      queries: [{ refId: "A", datasource: { type, uid: ds }, queryType, ...(expr.startsWith("{") ? { expr } : { query: expr }) }] } }));
    links.push(h("a", { href: `${GRAFANA}/explore?schemaVersion=1&orgId=1&panes=${pane("tempo", "tempo", model.traceId, "traceql")}`, target: "_blank", rel: "noopener" }, "trace in Grafana ↗"),
      h("a", { href: `${GRAFANA}/explore?schemaVersion=1&orgId=1&panes=${pane("loki", "loki", `{service_name=~".+"} | trace_id="${model.traceId}"`, "range")}`, target: "_blank", rel: "noopener" }, "logs ↗"));
  }
  fill($("summary"),
    h("span", { class: `status-pill ${status[0]}` }, status[1]),
    hasData ? stat("total time", fmtMs(st.duration)) : null,
    hasData ? stat("steps (calls)", String(model.derived().steps)) : null,
    hasData ? stat("spans", `${st.spans} in ${st.services} services`) : null,
    hasData ? stat("model calls", String(st.llmCalls)) : null,
    model.result ? stat("tokens", `${fmtInt(st.inputTokens + st.outputTokens)}`) : null,
    model.result ? stat("est. cost", fmtUsd(st.cost)) : null,
    st.errors ? stat("errors", String(st.errors)) : null,
    h("div", { id: "links" }, ...links));
}

function renderCaption() {
  const { spans, steps } = model.derived();
  const el = $("caption");
  const calls = spans.filter((s) => s.route && stateAt(s, ui.T) !== "future");
  const last = calls[calls.length - 1];
  if (!last) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const state = stateAt(last, ui.T), d = describe(last);
  const live = state === "running";
  el.replaceChildren(h("b", {}, `Step ${last.step} of ${steps}`), h("span", {}, d.title),
    h("span", { class: `pill ${state === "error" ? "err" : live ? "run" : "ok"}` }, state === "error" ? "error" : live ? "in progress" : "done"),
    h("span", { class: "muted" }, clip(d.result || d.plain, 170)));
}

function renderPlayback() {
  const { t0, tEnd } = bounds();
  const scrub = $("scrub");
  if (t0 === null) { scrub.disabled = true; $("clock").textContent = "0.0 s"; return; }
  scrub.disabled = ui.live && !model.done;
  scrub.min = 0; scrub.max = Math.max((tEnd - t0) * 1000, 1);
  scrub.value = Math.min((ui.T - t0) * 1000, scrub.max);
  $("clock").textContent = ui.live && !model.done ? `${fmtMs(Math.max(ui.T - t0, 0) * 1000)} elapsed`
    : `${fmtMs(Math.max(ui.T - t0, 0) * 1000)} / ${fmtMs((tEnd - t0) * 1000)}`;
  $("play").textContent = playing ? "⏸" : "▶";
  $("play").disabled = ui.live && !model.done;
  $("step").disabled = $("back").disabled = ui.live && !model.done;
}

function renderAnswer() {
  const box = $("answer");
  if (!model.result && !model.error) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.classList.toggle("error", Boolean(model.error));
  const r = model.result;
  $("answer-title").textContent = model.error ? "Request failed" : "Final answer";
  $("answer-text").textContent = model.error ? model.error : r.answer;
  const parts = [];
  if (r) {
    if (r.visited?.length) parts.push("workers: " + r.visited.join(" → "));
    parts.push("tools: " + (r.tools_used?.length ? r.tools_used.join(", ") : "none"));
    if (r.llm_calls) parts.push(`${r.llm_calls} model calls, ${fmtInt(r.input_tokens + r.output_tokens)} tokens (${fmtInt(r.input_tokens)} in / ${fmtInt(r.output_tokens)} out), est. ${fmtUsd(r.cost_usd)}`);
    if (r.prompt_version) parts.push("prompt version " + r.prompt_version);
    if (r.langsmith_run_id) parts.push("LangSmith run " + r.langsmith_run_id);
  }
  $("answer-meta").textContent = parts.join("  ·  ");
  $("feedback").classList.toggle("hidden", !r);
}

// ---------------------------------------------------------------------------------------- selection
function select(id) {
  ui.selectedId = id;
  scheduleRender();
  document.querySelector(`[data-span="${id}"]`)?.scrollIntoView?.({ block: "nearest" });
}

function stepSelection(delta) {
  const { spans } = model.derived();
  const i = spans.findIndex((s) => s.id === ui.selectedId);
  select(spans[Math.min(Math.max((i < 0 ? (delta > 0 ? -1 : spans.length) : i) + delta, 0), spans.length - 1)]?.id ?? null);
}

// ---------------------------------------------------------------------------------------- time control
function setT(t) { ui.T = t; scheduleRender(); }

function stopPlayback() { playing = false; cancelAnimationFrame(replayFrame); renderPlayback(); }

function startPlayback() {
  const { t0, tEnd } = bounds();
  if (t0 === null) return;
  if (ui.T >= tEnd - 0.01) ui.T = t0;
  playing = true;
  let last = performance.now();
  const frame = (now) => {
    if (!playing) return;
    ui.T = Math.min(ui.T + ((now - last) / 1000) * speed, bounds().tEnd);
    last = now;
    render();
    if (ui.T >= bounds().tEnd) { stopPlayback(); return; }
    replayFrame = requestAnimationFrame(frame);
  };
  replayFrame = requestAnimationFrame(frame);
}

/** Jump to the moment the next (or previous) call begins. */
function jumpToCall(direction) {
  stopPlayback();
  const calls = model.derived().spans.filter((s) => s.route);
  const target = direction > 0 ? calls.find((s) => s.start > ui.T + 0.0005) : [...calls].reverse().find((s) => s.start < ui.T - 0.0005);
  if (target) { ui.T = target.start + 0.0005; select(target.id); }
  else if (direction > 0) ui.T = bounds().tEnd;
  scheduleRender();
}

// ---------------------------------------------------------------------------------------- live + replay
function closeStream() { source?.close(); source = null; clearInterval(liveTimer); }

async function loadLogs() {
  if (!model.traceId) return;
  try {
    const res = await fetch(`/api/logs/${model.traceId}`);
    const data = await res.json();
    model.setLogs(data.available ? data.logs : []);
  } catch { model.setLogs([]); }
  scheduleRender();
}

function startLive(requestId) {
  closeStream(); stopPlayback();
  model.reset(); ui.selectedId = null; ui.live = true; currentId = requestId; ui.T = Date.now() / 1000;
  markCurrent(); scheduleRender();
  source = new EventSource(`/events/${requestId}/stream`);
  source.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    model.apply(ev);
    if (ev.kind === "request_done" || ev.kind === "request_error") {
      setTimeout(() => { closeStream(); ui.live = false; ui.T = bounds().tEnd; loadLogs(); loadHistory(); scheduleRender(); }, 2500);
    }
    scheduleRender();
  };
  liveTimer = setInterval(() => {
    if (!ui.live) return;
    ui.T = Math.max(Date.now() / 1000, bounds().tEnd ?? 0);
    scheduleRender();
  }, 120);
}

async function loadRequest(id) {
  closeStream(); stopPlayback();
  currentId = id; ui.live = false; ui.selectedId = null; markCurrent();
  const events = await (await fetch(`/requests/${id}/events`)).json();
  model.reset();
  events.sort((a, b) => a.ts - b.ts).forEach((e) => model.apply(e));
  ui.T = bounds().tEnd ?? 0;
  scheduleRender(); loadLogs();
  const first = model.derived().spans.find((s) => s.route);
  if (first) select(first.id);
}

async function loadHistory() {
  const list = await (await fetch("/requests")).json();
  const select = $("history-select");
  select.replaceChildren(h("option", { value: "" }, `Past requests (${list.length})…`));
  for (const r of list) {
    const when = new Date(r.created * 1000).toLocaleTimeString();
    select.append(h("option", { value: r.request_id }, `${when} · ${clip(r.question || "(no question)", 40)}${r.finished ? "" : " (running)"}`));
  }
  markCurrent();
}
function markCurrent() { $("history-select").value = [...$("history-select").options].some((o) => o.value === currentId) ? currentId : ""; }

// ---------------------------------------------------------------------------------------- feedback
async function sendFeedback(score) {
  $("fb-up").disabled = $("fb-down").disabled = true;
  try {
    const res = await fetch("/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: currentId, score }) });
    const out = await res.json();
    $("fb-msg").textContent = res.ok ? "Thanks! " + out.detail : `Could not send feedback (${res.status})`;
  } catch { $("fb-msg").textContent = "Network error"; }
}

// ---------------------------------------------------------------------------------------- wiring
$("history-select").addEventListener("change", (e) => { if (e.target.value) loadRequest(e.target.value); });
$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("q").value.trim();
  if (question.length < 3) return;
  const requestId = "r" + Math.random().toString(36).slice(2, 8);
  $("fb-msg").textContent = ""; $("fb-up").disabled = $("fb-down").disabled = false;
  startLive(requestId);                                   // open the stream FIRST...
  $("go").disabled = true;
  try {
    const res = await fetch("/support", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, request_id: requestId }) });
    if (!res.ok && !model.error) model.error = `The gateway answered ${res.status}`;
  } catch { model.error = "Could not reach the gateway"; }
  finally { $("go").disabled = false; loadHistory(); scheduleRender(); }   // ...then send the request
});

for (const text of ["Where is order 42, and can I return it?", "Is the product in order 43 in stock?", "How long does express shipping take?", "Where is order 999?"]) {
  $("chips").append(h("button", { type: "button", class: "chip", onclick: () => { $("q").value = text; $("q").focus(); } }, text));
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  ui.view = b.dataset.view;
  document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  $("opt-telemetry-wrap").classList.toggle("hidden", ui.view !== "architecture");
  scheduleRender();
}));
$("opt-telemetry").addEventListener("change", (e) => { ui.showTelemetry = e.target.checked; scheduleRender(); });
$("play").addEventListener("click", () => (playing ? stopPlayback() : startPlayback()));
$("step").addEventListener("click", () => jumpToCall(1));
$("back").addEventListener("click", () => jumpToCall(-1));
$("speed").addEventListener("change", (e) => { speed = Number(e.target.value); });
$("scrub").addEventListener("input", (e) => { stopPlayback(); setT(bounds().t0 + Number(e.target.value) / 1000); });
$("fb-up").addEventListener("click", () => sendFeedback(1));
$("fb-down").addEventListener("click", () => sendFeedback(0));
$("stage").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); stepSelection(1); }
  if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); stepSelection(-1); }
  if (e.key === "Escape") select(null);
});
window.addEventListener("resize", scheduleRender);

loadHistory();
scheduleRender();
