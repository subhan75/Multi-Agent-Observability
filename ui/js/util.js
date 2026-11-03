// Small helpers shared by every view: build DOM/SVG elements safely, and format numbers.
// Everything is built with createElement + textContent (never innerHTML), so text that comes from
// the AI or from users can never be interpreted as HTML.

const SVG_NS = "http://www.w3.org/2000/svg";

/** h("div", {class: "x"}, "text", childElement) -> an HTML element. */
export function h(tag, attrs = {}, ...children) {
  return build(document.createElement(tag), attrs, children);
}

/** s("rect", {x: 1, y: 2}) -> an SVG element (SVG needs its own namespace). */
export function s(tag, attrs = {}, ...children) {
  return build(document.createElementNS(SVG_NS, tag), attrs, children);
}

function build(el, attrs, children) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.setAttribute("class", value);
    else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** 1234 ms -> "1.23 s"; 5.2 ms -> "5.2 ms" */
export function fmtMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "…";
  if (ms >= 10000) return (ms / 1000).toFixed(1) + " s";
  if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
  if (ms >= 100) return Math.round(ms) + " ms";
  if (ms >= 1) return ms.toFixed(1) + " ms";
  return ms.toFixed(2) + " ms";
}

export const fmtInt = (n) => (n ?? 0).toLocaleString();
export const fmtUsd = (n) => "$" + (n ?? 0).toFixed(n >= 0.1 ? 3 : 4);
export const clip = (text, n = 90) => (String(text).length > n ? String(text).slice(0, n - 1) + "…" : String(text));

/** Copy text to the clipboard, ignoring browsers that refuse. */
export async function copy(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

/** Pretty JSON for display. */
export const pretty = (value) => JSON.stringify(value, null, 2);
