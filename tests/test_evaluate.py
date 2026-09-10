"""Tests for the model-evaluation harness (src/evaluate.py).

Everything here is offline: retrieval is real (local TF-IDF), and the answer
eval is driven by an injected stub ``answer_fn`` so no Anthropic client is ever
constructed and no network call is made.
"""

import json

import pytest

from src import evaluate
from src.evaluate import QAItem


# --- a tiny synthetic library -----------------------------------------------
#
# Three papers with clearly-separated vocabulary so retrieval is deterministic.
# Shaped like Library.all_papers() rows: id / filename / title / text.

def _papers():
    return [
        {
            "id": 1,
            "filename": "volcano_muography.pdf",
            "title": "Muon Tomography of Volcanoes",
            "text": "muon tomography imaging volcano density interior magma " * 30,
        },
        {
            "id": 2,
            "filename": "sipm_scifi.pdf",
            "title": "SiPM Readout of Scintillating Fibres",
            "text": "sipm scintillating fibre detector photon readout tracking " * 30,
        },
        {
            "id": 3,
            "filename": "arfken_methods.pdf",
            "title": "Mathematical Methods for Physicists",
            "text": "integral function series bessel special function equation " * 30,
        },
    ]


# --- question-set loading ----------------------------------------------------

def test_load_question_set(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps([
        {"question": "how do volcanoes work", "relevant": ["volcano"], "note": "x"},
        {"question": "special functions", "relevant": ["arfken", "Mathematical"]},
    ]), encoding="utf-8")

    items = evaluate.load_question_set(p)
    assert len(items) == 2
    assert items[0].question == "how do volcanoes work"
    assert items[0].relevant == ["volcano"]
    assert items[0].note == "x"
    assert items[1].note == ""  # defaulted


def test_load_question_set_rejects_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([{"relevant": ["x"]}]), encoding="utf-8")  # no question
    with pytest.raises(ValueError):
        evaluate.load_question_set(p)

    p.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate.load_question_set(p)

    p.write_text(json.dumps([{"question": "q", "relevant": "notalist"}]), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate.load_question_set(p)


# --- label matching ----------------------------------------------------------

def test_paper_matches_filename_and_title_case_insensitively():
    paper = _papers()[0]
    assert evaluate.paper_matches(paper, "volcano")       # filename
    assert evaluate.paper_matches(paper, "TOMOGRAPHY")     # title, case-insensitive
    assert not evaluate.paper_matches(paper, "borehole")


def test_relevant_ids_collects_all_matching_papers():
    papers = _papers()
    item = QAItem("q", ["volcano", "arfken"])
    assert evaluate.relevant_ids(papers, item) == {1, 3}


# --- retrieval evaluation ----------------------------------------------------

def test_evaluate_retrieval_recall_and_hit():
    papers = _papers()
    qaset = [
        QAItem("muon tomography volcano density", ["volcano"]),
        QAItem("sipm scintillating fibre photon readout", ["sipm"]),
    ]
    report = evaluate.evaluate_retrieval(papers, qaset, k=3)

    assert report.k == 3
    assert report.hit_rate == 1.0
    assert report.mean_recall == 1.0
    assert report.results[0].found == {1}
    assert report.results[1].found == {2}


def test_evaluate_retrieval_misses_are_counted():
    papers = _papers()
    # Ask a volcano question but (wrongly) label the Arfken paper relevant.
    qaset = [QAItem("muon tomography volcano magma interior", ["arfken"])]
    report = evaluate.evaluate_retrieval(papers, qaset, k=1)

    r = report.results[0]
    assert r.expected == {3}
    assert r.hit is False
    assert r.recall == 0.0
    assert report.hit_rate == 0.0


def test_retrieval_skips_unlabeled_questions_in_averages():
    papers = _papers()
    qaset = [
        QAItem("muon tomography volcano density", ["volcano"]),   # scored, hit
        QAItem("something about quantum gravity", ["nonexistent"]),  # skipped
    ]
    report = evaluate.evaluate_retrieval(papers, qaset, k=3)

    assert report.skipped == 1
    assert len(report.scored) == 1
    # The skipped question must not drag the averages to 50%.
    assert report.mean_recall == 1.0
    assert report.hit_rate == 1.0


def test_retrieved_ids_are_distinct_and_best_first():
    papers = _papers()
    report = evaluate.evaluate_retrieval(
        papers, [QAItem("muon tomography volcano", ["volcano"])], k=5)
    ids = report.results[0].retrieved_ids
    assert len(ids) == len(set(ids))     # no duplicate paper ids
    assert ids[0] == 1                   # best match first


# --- citation parsing --------------------------------------------------------

def test_parse_citations():
    assert evaluate.parse_citations("Grounded [S1] and also [S3], plus [S1].") == {1, 3}
    assert evaluate.parse_citations("no citations here") == set()


def test_legend_titles():
    legend = ["[S1] Muon Tomography of Volcanoes", "[S2] SiPM Readout"]
    assert evaluate.legend_titles(legend) == {
        1: "Muon Tomography of Volcanoes",
        2: "SiPM Readout",
    }


# --- answer scoring ----------------------------------------------------------

def test_score_answer_faithful_relevant_and_grounded():
    papers = _papers()
    item = QAItem("volcano imaging", ["volcano"])
    answer = "Muography images volcanoes via density contrast [S1]."
    legend = ["[S1] Muon Tomography of Volcanoes", "[S2] SiPM Readout of Scintillating Fibres"]

    r = evaluate.score_answer(papers, item, answer, legend)
    assert r.cited == {1}
    assert r.valid_cited == {1}
    assert r.faithful is True
    assert r.grounded is True
    assert r.relevant is True


def test_score_answer_detects_hallucinated_citation():
    papers = _papers()
    item = QAItem("volcano imaging", ["volcano"])
    # [S5] does not exist in the legend -> not faithful.
    answer = "Volcanoes are imaged [S1] using cosmic rays [S5]."
    legend = ["[S1] Muon Tomography of Volcanoes"]

    r = evaluate.score_answer(papers, item, answer, legend)
    assert r.cited == {1, 5}
    assert r.valid_cited == {1}
    assert r.faithful is False       # [S5] is hallucinated
    assert r.grounded is True        # but it did cite a real source
    assert r.relevant is True


def test_score_answer_cited_off_label_source():
    papers = _papers()
    item = QAItem("volcano imaging", ["volcano"])
    # Cites a real source, but the SiPM paper is not labeled relevant here.
    answer = "See the fibre detector [S1]."
    legend = ["[S1] SiPM Readout of Scintillating Fibres"]

    r = evaluate.score_answer(papers, item, answer, legend)
    assert r.faithful is True
    assert r.grounded is True
    assert r.relevant is False       # cited source isn't a labeled paper


def test_score_answer_ungrounded():
    papers = _papers()
    item = QAItem("volcano imaging", ["volcano"])
    r = evaluate.score_answer(papers, item, "No sources cited at all.", [])
    assert r.cited == set()
    assert r.grounded is False
    assert r.faithful is True         # vacuously: no bad citations
    assert r.relevant is False


# --- end-to-end answer eval with an injected stub ---------------------------

def test_run_answer_eval_aggregates():
    papers = _papers()
    qaset = [
        QAItem("volcano imaging", ["volcano"]),
        QAItem("fibre readout", ["sipm"]),
    ]

    # A deterministic stub answer_fn: perfect, grounded, relevant answers.
    def answer_fn(question, k):
        if "volcano" in question:
            return ("Imaged via density [S1].",
                    ["[S1] Muon Tomography of Volcanoes"])
        return ("Photons read out [S1].",
                ["[S1] SiPM Readout of Scintillating Fibres"])

    report = evaluate.run_answer_eval(papers, qaset, answer_fn, k=3)
    assert report.faithfulness == 1.0
    assert report.grounded_rate == 1.0
    assert report.relevance == 1.0


def test_run_answer_eval_mixed_scores():
    papers = _papers()
    qaset = [
        QAItem("volcano imaging", ["volcano"]),   # good
        QAItem("fibre readout", ["sipm"]),        # hallucinated citation
    ]

    def answer_fn(question, k):
        if "volcano" in question:
            return ("Density [S1].", ["[S1] Muon Tomography of Volcanoes"])
        return ("Readout [S1] and [S9].", ["[S1] SiPM Readout of Scintillating Fibres"])

    report = evaluate.run_answer_eval(papers, qaset, answer_fn, k=3)
    assert report.faithfulness == 0.5     # one of two answers hallucinated
    assert report.grounded_rate == 1.0    # both cited a real source
    assert report.relevance == 1.0


# --- reporting ---------------------------------------------------------------

def test_format_report_runs_and_mentions_metrics():
    papers = _papers()
    qaset = [QAItem("muon tomography volcano", ["volcano"])]
    retrieval = evaluate.evaluate_retrieval(papers, qaset, k=3)

    text = evaluate.format_report(retrieval)
    assert "RETRIEVAL" in text
    assert "recall@3" in text
    assert "ANSWER" not in text          # no answer report passed

    def answer_fn(q, k):
        return ("Density [S1].", ["[S1] Muon Tomography of Volcanoes"])

    answer = evaluate.run_answer_eval(papers, qaset, answer_fn, k=3)
    text2 = evaluate.format_report(retrieval, answer)
    assert "ANSWER" in text2
    assert "faithfulness" in text2


# --- the shipped default question set is valid and loadable -----------------

def test_default_question_set_is_valid():
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "evals" / "questions.json"
    items = evaluate.load_question_set(path)
    assert len(items) >= 5
    assert all(item.question and item.relevant for item in items)
