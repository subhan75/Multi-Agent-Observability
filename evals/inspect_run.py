"""Show what LangSmith recorded for one run: its tree, metadata, token counts and feedback.

    python -m evals.inspect_run <run_id>

Get the run id from the X-ray page (shown under the answer) or from the `run_id` field returned by POST /support.
A quick way to confirm your LangSmith setup works end to end. Prints structure and ids only,
never your keys and never the customer's text.
"""
import argparse
import sys
import time

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()


def show_tree(node, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = budget if budget is not None else [60]
    if budget[0] <= 0 or depth > 5:
        return
    budget[0] -= 1
    tokens = f"  tokens={node.total_tokens}" if node.total_tokens else ""
    print(f"{'  ' * depth}{node.name:<30} [{node.run_type}]{tokens}")
    for child in sorted(node.child_runs or [], key=lambda c: c.start_time):
        show_tree(child, depth + 1, budget)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id")
    args = parser.parse_args()

    client = Client()
    run = None
    for _ in range(18):  # LangSmith ingests in the background, so a brand-new run may take a few seconds to appear
        try:
            run = client.read_run(args.run_id, load_child_runs=True)
            if run.child_runs and run.end_time:
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  waiting for LangSmith ({type(exc).__name__})...")
        time.sleep(5)
    if run is None:
        sys.exit("The run did not appear. Check LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env, then restart the agents service.")

    meta = (run.extra or {}).get("metadata", {})
    print(f"run: {run.name} [{run.run_type}]   tags: {run.tags}")
    print("metadata keys:", sorted(k for k in meta if not k.startswith("ls_")))
    print("otel_trace_id (match this to the Tempo trace):", meta.get("otel_trace_id"))
    print("model / prompt_version:", meta.get("model"), "/", meta.get("prompt_version"))
    if run.end_time:
        print(f"latency: {(run.end_time - run.start_time).total_seconds():.1f}s   total tokens: {run.total_tokens}")
    print("\nrun tree:")
    show_tree(run)
    feedback = list(client.list_feedback(run_ids=[args.run_id]))
    print("\nfeedback on this run:", [(f.key, f.score) for f in feedback] or "none")


if __name__ == "__main__":
    main()
