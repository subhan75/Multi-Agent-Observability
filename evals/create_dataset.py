"""Upload evals/questions.json to LangSmith as a dataset (needs LANGSMITH_API_KEY in .env).

    python -m evals.create_dataset            # create it (does nothing if it already exists)
    python -m evals.create_dataset --force    # delete and recreate it (after editing questions.json)

A DATASET is a saved list of examples. Each example has INPUTS (the question we send) and REFERENCE
OUTPUTS (what we expect: which workers, which tool, which facts). Experiments are scored against it.
"""
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

DEFAULT_DATASET = "shopsense-support-v1"
QUESTIONS_FILE = Path(__file__).parent / "questions.json"


def load_questions() -> list[dict]:
    return json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))


def to_example(q: dict) -> dict:
    """One row of questions.json -> one LangSmith example."""
    return {
        "inputs": {"question": q["question"]},
        "outputs": {key: q.get(key) for key in ("workers", "tools", "facts", "forbidden") if key in q},
        "metadata": {"id": q["id"], "category": q["category"], "notes": q.get("notes", "")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=DEFAULT_DATASET)
    parser.add_argument("--force", action="store_true", help="delete the dataset first and recreate it")
    args = parser.parse_args()

    client = Client()
    if client.has_dataset(dataset_name=args.name):
        if not args.force:
            print(f"Dataset '{args.name}' already exists. Use --force to recreate it.")
            return
        client.delete_dataset(dataset_name=args.name)
        print(f"Deleted the old '{args.name}'.")

    client.create_dataset(args.name, description="Customer-support questions with expected workers, tools and facts (ShopSense).")
    examples = [to_example(q) for q in load_questions()]
    client.create_examples(dataset_name=args.name, examples=examples)
    print(f"Created dataset '{args.name}' with {len(examples)} examples.")


if __name__ == "__main__":
    main()
