"""Run the quality evaluations against the real system.

    python -m evals.run_eval --local --limit 4        # try a few questions; no LangSmith account needed
    python -m evals.run_eval --local                  # all 20 questions, results printed here only
    python -m evals.run_eval                          # all 20, recorded as an EXPERIMENT in LangSmith
    python -m evals.run_eval --concurrency 4 --limit 10
    python -m evals.run_eval --only off-topic,status-43      # re-run just the questions you are investigating

Needs: the orders + inventory services running (scripts/run_all.ps1) and GOOGLE_API_KEY in .env.
LangSmith mode also needs LANGSMITH_API_KEY, and the dataset (python -m evals.create_dataset).

What happens, for each question: the real graph answers it, then every evaluator scores the result.
  * code evaluators:  route_correct, tool_use_correct, facts_present, no_prompt_leak   (free, instant)
  * judge evaluators: tone_ok, grounded                                               (a second model grades)
Re-run after changing a prompt or the model, and compare: that is how you tell whether a change helped.
"""
import argparse
import inspect
import os
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

from evals.create_dataset import DEFAULT_DATASET, load_questions  # noqa: E402
from evals.evaluators import CODE_EVALUATORS  # noqa: E402
from evals.judges import DEFAULT_JUDGE_MODEL, JUDGE_EVALUATORS  # noqa: E402
from evals.target import describe_setup, run_support_graph  # noqa: E402

ALL_EVALUATORS = CODE_EVALUATORS + JUDGE_EVALUATORS


def reference_for(question: dict) -> dict:
    """The 'expected' side of one question (same shape LangSmith stores as reference outputs)."""
    return {key: question[key] for key in ("workers", "tools", "facts", "forbidden") if key in question}


def call_evaluator(evaluator, inputs: dict, outputs: dict, reference: dict) -> dict:
    """Pass each evaluator only the arguments it asks for (LangSmith does the same by parameter name)."""
    wanted = inspect.signature(evaluator).parameters
    available = {"inputs": inputs, "outputs": outputs, "reference_outputs": reference}
    return evaluator(**{name: value for name, value in available.items() if name in wanted})


def summarize(rows: list[dict]) -> None:
    keys = [e.__name__ for e in ALL_EVALUATORS]
    print("\n" + "question".ljust(20) + "".join(k[:16].ljust(18) for k in keys))
    print("-" * (20 + 18 * len(keys)))
    for row in rows:
        cells = []
        for key in keys:
            score = row["scores"].get(key, (None, ""))[0]
            cells.append(("-" if score is None else f"{score:.2f}").ljust(18))
        print(str(row["id"]).ljust(20) + "".join(cells))

    print("\nAVERAGES (questions where the check applies):")
    for key in keys:
        values = [r["scores"][key][0] for r in rows if r["scores"].get(key, (None,))[0] is not None]
        print(f"  {key:<18} {sum(values) / len(values):.2f}   ({len(values)} questions)" if values else f"  {key:<18} n/a")

    print("\nPROBLEMS (any score below 1):")
    found = False
    for row in rows:
        for key in keys:
            score, comment = row["scores"].get(key, (None, ""))
            if score is not None and score < 1:
                found = True
                print(f"  {row['id']:<20} {key:<17} {score:.2f}  {comment[:110]}")
    if not found:
        print("  none")


def run_local(limit: int | None, only: set[str] | None, concurrency: int) -> list[dict]:
    questions = [q for q in load_questions() if not only or q["id"] in only][:limit]

    def one(q: dict) -> dict:
        inputs = {"question": q["question"]}
        outputs = run_support_graph(inputs)
        reference = reference_for(q)
        scores = {}
        for evaluator in ALL_EVALUATORS:
            result = call_evaluator(evaluator, inputs, outputs, reference)
            scores[evaluator.__name__] = (result["score"], result.get("comment", ""))
        print(f"  done: {q['id']}")
        return {"id": q["id"], "category": q["category"], "scores": scores, "answer": outputs["answer"]}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(one, questions))


def run_langsmith(dataset: str, limit: int | None, only: set[str] | None, concurrency: int) -> list[dict]:
    from langsmith import Client

    client = Client()
    setup = describe_setup()
    data = dataset
    if limit or only:  # a subset: pick examples ourselves instead of passing the whole dataset name
        examples = [e for e in client.list_examples(dataset_name=dataset) if not only or (e.metadata or {}).get("id") in only]
        data = examples[:limit]
    results = client.evaluate(
        run_support_graph,
        data=data,
        evaluators=ALL_EVALUATORS,
        experiment_prefix=f"{setup['model']}-prompt-{setup['prompt_version']}",
        max_concurrency=concurrency,
        metadata={**setup, "judge_model": os.getenv("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)},
    )
    rows = []
    for item in results:
        example, evaluation = item["example"], item["evaluation_results"]["results"]
        rows.append({"id": (example.metadata or {}).get("id", str(example.id)[:8]),
                     "category": (example.metadata or {}).get("category", ""),
                     "scores": {r.key: (r.score, r.comment or "") for r in evaluation}})
    print(f"\nExperiment recorded in LangSmith: {getattr(results, 'experiment_name', '(see the LangSmith UI)')}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", action="store_true", help="do not use LangSmith: run and print here only")
    parser.add_argument("--limit", type=int, help="only the first N questions")
    parser.add_argument("--only", help="comma-separated question ids to run, e.g. off-topic,status-43")
    parser.add_argument("--concurrency", type=int, default=2, help="questions answered at the same time (watch Gemini rate limits)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    args = parser.parse_args()

    setup = describe_setup()
    print(f"Evaluating model={setup['model']} prompt_version={setup['prompt_version']} "
          f"judge={os.getenv('JUDGE_MODEL', DEFAULT_JUDGE_MODEL)} mode={'local' if args.local else 'LangSmith'}")
    started = time.time()
    only = {i.strip() for i in args.only.split(",")} if args.only else None
    rows = (run_local(args.limit, only, args.concurrency) if args.local
            else run_langsmith(args.dataset, args.limit, only, args.concurrency))
    summarize(rows)
    print(f"\nfinished in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
