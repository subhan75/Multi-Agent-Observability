// Time helpers shared by the views. "T" is the moment being shown, in seconds since 1970 (the same clock the
// spans use). Live, T follows the clock. When replaying, T comes from the scrubber.

/** What state a step is in at moment T: "future" (not started), "running", "done" or "error". */
export function stateAt(span, T) {
  if (span.start > T) return "future";
  if (span.end === null || span.end > T) return "running";
  return span.status === "ERROR" ? "error" : "done";
}

/** The end of a step as it should be DRAWN at moment T (a running step reaches "now"). */
export function endAt(span, T) {
  return span.end === null || span.end > T ? T : span.end;
}
