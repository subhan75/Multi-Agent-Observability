// The sequence diagram. One column ("lane") per real component. Time runs DOWN the page.
//   solid arrow  = a call (numbered in the order the calls began)
//   dashed arrow = the answer coming back, labelled with how long the call took
//   bar on a lane = that component is busy handling a call (bars nest when it is busy with several at once)
// Reading it top to bottom replays the request exactly as it happened.

import { LANES, NODES } from "./nodes.js";
import { stateAt } from "./time.js";
import { clip, fmtMs, s } from "./util.js";

const LANE_W = 76, HEAD_H = 46, ROW_H = 30, SIDE = 10;

/** Split a label into at most two lines at a space, e.g. "Inventory service" -> ["Inventory", "service"]. */
function wrap(text, max = 11) {
  if (text.length <= max || !text.includes(" ")) return [text];
  const words = text.split(" "), lines = [""];
  for (const w of words) {
    if ((lines[lines.length - 1] + " " + w).trim().length <= max || lines.length === 2) lines[lines.length - 1] = (lines[lines.length - 1] + " " + w).trim();
    else lines.push(w);
  }
  return lines;
}

export function renderSequence(container, model, ui) {
  const { spans } = model.derived();
  const T = ui.T;

  // Every call and every return becomes one "mark"; marks sorted by time become the rows, top to bottom.
  const calls = spans.filter((sp) => sp.route && stateAt(sp, T) !== "future");
  const marks = [];
  for (const sp of calls) {
    marks.push({ t: sp.start, kind: "call", sp });
    if (sp.end !== null && sp.end <= T) marks.push({ t: sp.end, kind: "return", sp });
  }
  // Same instant: returns first (innermost first), then calls (outermost first).
  marks.sort((a, b) => a.t - b.t || (a.kind === b.kind ? (a.kind === "return" ? b.sp.depth - a.sp.depth : a.sp.depth - b.sp.depth) : a.kind === "return" ? -1 : 1));
  const rowOf = new Map();
  marks.forEach((m, i) => rowOf.set(`${m.kind}:${m.sp.id}`, i));

  const width = SIDE * 2 + LANES.length * LANE_W;
  const bodyH = (marks.length + 1) * ROW_H + 24;
  const laneX = (id) => SIDE + LANES.indexOf(id) * LANE_W + LANE_W / 2;
  const rowY = (i) => 18 + i * ROW_H + ROW_H / 2;

  // ---- header (stays in view while the body scrolls) ----
  const head = s("svg", { width, height: HEAD_H, role: "presentation" });
  for (const id of LANES) {
    const n = NODES[id], x = laneX(id);
    const lines = wrap(n.label);
    head.append(s("g", { class: `lane-head g-${n.group}` }, s("title", {}, `${n.label}: ${n.sub}`),
      s("rect", { x: x - LANE_W / 2 + 3, y: 4, width: LANE_W - 6, height: HEAD_H - 10 }),
      ...lines.map((line, i) => s("text", { x, y: (lines.length === 1 ? 26 : 19) + i * 13 }, line))));
  }
  const headWrap = document.createElement("div");
  headWrap.style.cssText = "position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line)";
  headWrap.append(head);

  // ---- body ----
  const svg = s("svg", { width, height: bodyH, role: "img", "aria-label": "Sequence diagram of the request" });
  svg.append(s("defs", {}, s("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    s("path", { class: "msg-head", d: "M0,0 L10,5 L0,10 z" }))));

  // a faint backdrop behind the lanes that belong to the agents service
  const first = laneX("agents") - LANE_W / 2 + 3, last = laneX("writer") + LANE_W / 2 - 3;
  svg.append(s("rect", { x: first, y: 0, width: last - first, height: bodyH, fill: "var(--c-agents)", "fill-opacity": 0.05, rx: 8 }),
             s("text", { x: first + 8, y: 12, class: "wf-tick", "text-anchor": "start", style: "text-anchor:start;fill:var(--c-agents)" }, "inside the agents service"));
  for (const id of LANES) svg.append(s("line", { class: "lifeline", x1: laneX(id), x2: laneX(id), y1: 0, y2: bodyH }));

  // highlight the rows of the selected step
  for (const m of marks) if (m.sp.id === ui.selectedId) svg.append(s("rect", { class: "selected-row", x: 0, y: rowY(rowOf.get(`${m.kind}:${m.sp.id}`)) - ROW_H / 2, width, height: ROW_H }));

  // activation bars (drawn before arrows so arrows sit on top)
  const lastRow = Math.max(marks.length - 1, 0);
  for (const sp of calls) {
    const top = rowY(rowOf.get(`call:${sp.id}`)), ret = rowOf.get(`return:${sp.id}`);
    const bottom = ret === undefined ? rowY(lastRow) + ROW_H / 2 : rowY(ret);
    const nest = calls.filter((o) => o !== sp && o.route.to === sp.route.to && o.start <= sp.start && (o.end === null || o.end >= (sp.end ?? Infinity))
      && rowOf.get(`call:${o.id}`) < rowOf.get(`call:${sp.id}`)).length;
    const state = stateAt(sp, T), group = NODES[sp.route.to].group;
    svg.append(s("rect", { class: `activation g-${group}${state === "running" ? " running" : ""}${state === "error" ? " error" : ""}`, "data-span": sp.id,
      x: laneX(sp.route.to) - 5 + nest * 4, y: top - 2, width: 10, height: Math.max(bottom - top + 4, 10), rx: 3, onclick: () => ui.onSelect(sp.id) }));
  }

  // arrows + labels + click targets
  for (const m of marks) {
    const sp = m.sp, y = rowY(rowOf.get(`${m.kind}:${m.sp.id}`));
    const a = laneX(m.kind === "call" ? sp.route.from : sp.route.to), b = laneX(m.kind === "call" ? sp.route.to : sp.route.from);
    const dir = Math.sign(b - a) || 1, bad = sp.status === "ERROR" && m.kind === "return";
    const pad = 8 + (m.kind === "call" ? 0 : 0);
    svg.append(s("line", { class: `msg${m.kind === "return" ? " return" : ""}${bad ? " error" : ""}`, x1: a + dir * pad, x2: b - dir * pad, y1: y, y2: y, "marker-end": "url(#arrow)" }));
    const labelText = m.kind === "call" ? `${sp.step}. ${sp.route.label}` : (bad ? "error · " : "") + fmtMs((sp.end - sp.start) * 1000);
    svg.append(s("text", { class: `msg-label${m.kind === "return" ? " return" : ""}`, x: (a + b) / 2, y: y - 5 }, clip(labelText, 34)));
    svg.append(s("rect", { class: "hit", "data-span": sp.id, x: Math.min(a, b) - 4, y: y - ROW_H / 2 + 2, width: Math.abs(b - a) + 8, height: ROW_H - 4,
      tabindex: 0, role: "button", "aria-label": labelText, onclick: () => ui.onSelect(sp.id),
      onkeydown: (e) => { if (e.key === "Enter") ui.onSelect(sp.id); } }));
  }

  container.replaceChildren(headWrap, svg);
}
