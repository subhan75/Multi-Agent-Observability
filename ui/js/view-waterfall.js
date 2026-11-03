// The waterfall: every step of the trace as a horizontal bar on a shared time axis.
// Indentation shows "who called whom"; bar position and length show WHEN and for HOW LONG.
// Reading it: the widest bars are where the time went; bars side by side in time ran at the same moment.

import { NODES } from "./nodes.js";
import { shortLabel } from "./describe.js";
import { endAt, stateAt } from "./time.js";
import { fmtMs, s } from "./util.js";

const ROW = 25, LABEL_W = 360, RIGHT_PAD = 86, TOP = 30;

export function renderWaterfall(container, model, ui) {
  const { spans, t0 } = model.derived();
  const visible = spans.filter((sp) => stateAt(sp, ui.T) !== "future");
  const width = Math.max(container.clientWidth - 4, 760);
  const trackW = width - LABEL_W - RIGHT_PAD;
  const tEnd = Math.max(ui.T, ...visible.map((sp) => sp.end ?? sp.start)) ;
  const span = Math.max(tEnd - t0, 0.05);
  const x = (t) => LABEL_W + ((t - t0) / span) * trackW;

  const svg = s("svg", { width, height: TOP + visible.length * ROW + 12, viewBox: `0 0 ${width} ${TOP + visible.length * ROW + 12}`, role: "img",
    "aria-label": "Waterfall of every step in the trace" });

  // time axis with a few ticks
  for (let i = 0; i <= 5; i++) {
    const t = t0 + (span * i) / 5, px = x(t);
    svg.append(s("line", { class: "wf-axis", x1: px, x2: px, y1: TOP - 6, y2: TOP + visible.length * ROW }),
               s("text", { class: "wf-tick", x: px, y: 14 }, fmtMs((t - t0) * 1000)));
  }

  visible.forEach((sp, i) => {
    const y = TOP + i * ROW, state = stateAt(sp, ui.T), end = endAt(sp, ui.T);
    // colour by the component the step is AIMED at (Gemini pink, orders green...), not by who made the call
    const group = NODES[sp.route?.to ?? sp.actor]?.group ?? "client";
    const bx = x(sp.start), bw = Math.max(x(end) - bx, 2.5);
    const row = s("g", { class: `wf-row g-${group}${sp.id === ui.selectedId ? " selected" : ""}`, "data-span": sp.id, tabindex: 0, role: "button",
      "aria-label": `${shortLabel(sp)}, ${fmtMs((end - sp.start) * 1000)}`,
      onclick: () => ui.onSelect(sp.id), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ui.onSelect(sp.id); } } },
      s("rect", { class: "wf-bg", x: 0, y, width, height: ROW }),
      sp.step ? s("text", { class: "wf-step", x: 6, y: y + 16 }, `#${sp.step}`) : null,
      s("text", { class: "wf-name", x: 34 + sp.depth * 12, y: y + 16 }, shortLabel(sp)),
      s("rect", { class: `wf-bar${state === "error" ? " error" : ""}${state === "running" ? " running" : ""}`, x: bx, y: y + 5, width: bw, height: ROW - 10 }),
      s("text", { class: "wf-dur", x: Math.min(bx + bw + 6, width - RIGHT_PAD + 4), y: y + 16 }, state === "running" ? "running…" : fmtMs((end - sp.start) * 1000)));
    svg.append(row);
  });
  container.replaceChildren(svg);
}
