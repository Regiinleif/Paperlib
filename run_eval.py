"""Run the PaperLib model-evaluation harness from the command line.

Scores the RAG pipeline against a labeled question set (see ``evals/questions.json``):

    python run_eval.py                     # retrieval metrics only (no API cost)
    python run_eval.py --answer            # also generate + grade answers (needs a key)
    python run_eval.py --k 8               # change how many passages are retrieved
    python run_eval.py --questions my.json # use a different question set

Retrieval scoring is pure local TF-IDF and needs no Anthropic key. The optional
``--answer`` pass generates a real cited answer per question and grades its
citations for grounding, so it needs a configured key and burns API credit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src import config, evaluate
from src.library import Library

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "evals" / "questions.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the PaperLib RAG pipeline.")
    parser.add_argument(
        "--questions", default=str(DEFAULT_QUESTIONS),
        help="Path to the labeled question set (JSON). Defaults to evals/questions.json.",
    )
    parser.add_argument(
        "--k", type=int, default=5, help="Passages to retrieve per question (default 5).",
    )
    parser.add_argument(
        "--answer", action="store_true",
        help="Also generate answers with Claude and grade their citations "
             "(needs an API key; costs credit).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Override the model for --answer (defaults to the configured model).",
    )
    args = parser.parse_args(argv)

    qaset = evaluate.load_question_set(args.questions)
    library = Library()
    papers = library.all_papers()
    try:
        if not any(p.get("text") for p in papers):
            print("No digested papers in the library - nothing to evaluate.",
                  file=sys.stderr)
            return 1

        retrieval = evaluate.evaluate_retrieval(papers, qaset, k=args.k)

        answer_report = None
        if args.answer:
            api_key = config.get_api_key()
            if not api_key:
                print("--answer needs an Anthropic API key (set ANTHROPIC_API_KEY).",
                      file=sys.stderr)
                return 1
            retriever = evaluate.build_retriever(papers)
            answer_fn = evaluate.ragchat_answer_fn(retriever, api_key, model=args.model)
            answer_report = evaluate.run_answer_eval(papers, qaset, answer_fn, k=args.k)

        print(evaluate.format_report(retrieval, answer_report))
        return 0
    finally:
        library.close()


if __name__ == "__main__":
    raise SystemExit(main())
