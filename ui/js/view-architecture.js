// The architecture map: the real components of the system, laid out as a diagram.
// As the request travels, boxes light up, and the lines between them show calls in progress (moving dashes),
// finished calls (green) and failed calls (red). A badge on each box counts how many times it was called
// and for how long. The optional "telemetry pipeline" layer adds Collector, Tempo, Loki, Prometheus, Grafana
// and LangSmith, so you can see where the data about the request ends up.

import { EDGES, GROUP_BOXES, LAYOUT, NODES, VIEWBOX } from "./nodes.js";
import { stateAt } from "./time.js";
import { clip, fmtMs, s } from "./util.js";

/** A polyline with softly rounded corners, as an SVG path. */
function pathD(points, r = 9) {
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [px, py] = points[i - 1], [cx, cy] = points[i], [nx, ny] = points[i + 1];
    const a = Math.min(r, Math.hypot(cx - px, cy - py) / 2), b = Math.min(r, Math.hypot(nx - cx, ny - cy) / 2);
    const ux = Math.sign(cx - px), uy = Math.sign(cy - py), vx = Math.sign(nx - cx), vy = Math.sign(ny - cy);
    d += ` L${cx - ux * a},${cy - uy * a} Q${cx},${cy} ${cx + vx * b},${cy + vy * b}`;
  }
  const [lx, ly] = points[points.length - 1];
  return d + ` L${lx},${ly}`;
}

export function renderArchitecture(container, model, ui) {
  const { spans } = model.derived();
  const T = ui.T;
  const visible = spans.filter((sp) => stateAt(sp, T) !== "future");
  const calls = visible.filter((sp) => sp.route);

  // ---- what has happened to each component and each line ----
  const nodeInfo = {}, edgeInfo = {};
  for (const sp of calls) {
    const state = stateAt(sp, T), end = sp.end ?? T;
    const n = (nodeInfo[sp.route.to] ??= { calls: 0, running: 0, errors: 0, ms: 0 });
    n.calls++; n.ms += (end - sp.start) * 1000;
    if (state === "running") n.running++;
    if (state === "error") n.errors++;
    const key = `${sp.route.from}>${sp.route.to}`;
    const e = (edgeInfo[key] ??= { calls: 0, running: 0, errors: 0 });
    e.calls++;
    if (state === "running") e.running++;
    if (state === "error") e.errors++;
  }
  const selected = ui.selectedId ? model.derived().byId.get(ui.selectedId) : null;
  const selectedNode = selected?.route?.to ?? selected?.actor ?? null;
  const [vw, vh] = ui.showTelemetry ? VIEWBOX.withTelemetry : VIEWBOX.app;

  // telemetry: lines light up when there is something to send. (The real Collector batches for about 2 seconds, so this is approximate.)
  const finishedAny = visible.some((sp) => sp.end !== null && sp.end <= T);
  const servicesSeen = new Set(visible.map((sp) => sp.service));
  const telemetry = {
    "gateway>collector": servicesSeen.has("gateway"), "agents>collector": servicesSeen.has("agents"),
    "orders>collector": servicesSeen.has("orders"), "inventory>collector": servicesSeen.has("inventory"),
    "collector>tempo": finishedAny, "collector>prometheus": visible.some((sp) => sp.kind === "SERVER" && sp.end !== null && sp.end <= T),
    "collector>loki": finishedAny, "tempo>grafana": finishedAny, "prometheus>grafana": finishedAny, "loki>grafana": finishedAny,
    "agents>langsmith": Boolean(model.result?.langsmith_run_id) || (servicesSeen.has("agents") && visible.some((sp) => sp.name.startsWith("agent."))),
  };

  const svg = s("svg", { viewBox: `0 0 ${vw} ${vh}`, width: "100%", style: `max-width:${vw}px`, role: "img", "aria-label": "Architecture map of the request" });
  svg.append(s("defs", {}, s("marker", { id: "arch-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto" },
    s("path", { d: "M0,0 L10,5 L0,10 z", fill: "context-stroke" }))));

  for (const g of GROUP_BOXES) {
    svg.append(s("rect", { class: "group-box", x: g.x, y: g.y, width: g.w, height: g.h }), s("text", { class: "group-label", x: g.x + 12, y: g.y + g.h - 10 }, g.label));
  }

  // ---- lines ----
  const showEdge = (edge) => (edge.plane === "telemetry" ? ui.showTelemetry : true);
  for (const edge of EDGES.filter(showEdge)) {
    const info = edgeInfo[edge.id];
    let cls = "edge";
    if (edge.plane === "telemetry") cls += telemetry[edge.id] ? " seen telemetry" : " telemetry";
    else if (info) cls += info.errors ? " error" : info.running ? " calling" : " returned";
    svg.append(s("path", { class: cls, d: pathD(edge.points), "marker-end": "url(#arch-arrow)" }));
    // put labels on the final stretch of the line, close to the box it points at
    const [ax, ay] = edge.points[edge.points.length - 2], [bx, by] = edge.points[edge.points.length - 1];
    const lx = bx + (ax - bx) * 0.45, ly = by + (ay - by) * 0.45;
    if (info?.calls > 1) svg.append(s("text", { class: "edge-count", x: lx, y: ly - 5 }, `×${info.calls}`));
    else if (edge.label && edge.plane === "telemetry") svg.append(s("text", { class: "edge-label", x: lx, y: ly - 5 }, edge.label));
  }

  // ---- boxes ----
  for (const [id, n] of Object.entries(NODES)) {
    if (n.plane === "telemetry" && !ui.showTelemetry) continue;
    const box = LAYOUT[id], info = nodeInfo[id];
    const seen = Boolean(info) || (id === "browser" && calls.length > 0) || (n.plane === "telemetry" && telemetry[`${id}>grafana`] !== undefined && finishedAny)
      || (id === "collector" && finishedAny) || (id === "langsmith" && telemetry["agents>langsmith"]);
    const state = info?.errors ? "error" : info?.running ? "running" : info ? "done" : "";
    const g = s("g", { class: `node g-${n.group} ${seen ? "seen" : ""} ${state} ${id === selectedNode ? "selected" : ""}`, "data-node": id, tabindex: 0, role: "button",
      "aria-label": `${n.label}${info ? `, called ${info.calls} times` : ""}`,
      onclick: () => {
        const mine = calls.filter((sp) => sp.route.to === id);
        if (mine.length) ui.onSelect(mine[mine.length - 1].id);
      } },
      s("rect", { class: "body", x: box.x, y: box.y, width: box.w, height: box.h }),
      s("text", { class: "t", x: box.x + box.w / 2, y: box.y + box.h / 2 - 2 }, clip(n.label, 20)),
      s("text", { class: "sub", x: box.x + box.w / 2, y: box.y + box.h / 2 + 13 }, clip(n.sub, 30)));
    if (info) {
      const text = `×${info.calls} · ${fmtMs(info.ms)}`, bw = Math.max(text.length * 6 + 14, 48);
      g.append(s("g", { class: "badge" }, s("rect", { x: box.x + box.w - bw + 8, y: box.y - 9, width: bw, height: 17 }),
        s("text", { x: box.x + box.w - bw / 2 + 8, y: box.y + 3 }, text)));
    }
    svg.append(g);
  }
  container.replaceChildren(svg);
}
