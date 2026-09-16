import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import extract_clauses as ec  # noqa: E402

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "sample_contract.txt"


# ---------------------------------------------------------------------------
# Input + pre-filter
# ---------------------------------------------------------------------------
def test_read_paragraphs_txt_drops_blank_lines(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("one\n\n  two  \n\n", encoding="utf-8")
    assert ec.read_paragraphs(p) == ["one", "two"]


def test_prefilter_keeps_only_keyword_paragraphs_with_original_ids():
    paras = ["Definitions.", "The Licensee shall keep complete books and records.", "Governing law is Delaware.",
             "Licensor may inspect the premises."]
    kept = ec.prefilter(paras)
    assert kept == [(2, paras[1]), (4, paras[3])]


def test_prefilter_on_the_bundled_public_contract():
    paras = ec.read_paragraphs(CONTRACT)
    kept = ec.prefilter(paras)
    assert len(paras) > 100
    assert 0 < len(kept) < len(paras) / 3          # the gate removes most of the document
    assert any("Audit of Records" in p for _, p in kept)


# ---------------------------------------------------------------------------
# Prompts + parsing
# ---------------------------------------------------------------------------
def test_paragraph_prompt_contains_rubric_and_truncates():
    prompt = ec.build_paragraph_prompt("x" * 5000)
    assert "EXCLUDE" in prompt and "Governmental" in prompt
    assert "x" * 1500 in prompt and "x" * 1501 not in prompt
    assert ec.NO_MATCH in prompt


def test_parse_paragraph_output_filters_chatter_and_no_match():
    assert ec.parse_paragraph_output("NO_MATCH") == []
    assert ec.parse_paragraph_output("") == []
    text = ("Sure! Here are the clauses:\n"
            "- Licensor may audit the books and records of Licensee once per year.\n"
            "Short\n"
            "The parties agree that this Agreement is governed by Delaware law.\n")
    assert ec.parse_paragraph_output(text) == [
        "Licensor may audit the books and records of Licensee once per year."
    ]


def test_parse_paragraph_output_drops_label_echo():
    # Observed with google/flan-t5-base on the sample contract: the model returns the
    # rubric label instead of the clause. That is not an extraction.
    assert ec.parse_paragraph_output("'Audit and Inspection Rights'") == []
    assert ec.parse_paragraph_output("Audit & Inspection Rights.") == []
    kept = ec.parse_paragraph_output("Audit and Inspection Rights: Licensor may audit the records of Licensee.")
    assert kept == ["Audit and Inspection Rights: Licensor may audit the records of Licensee."]


def test_parse_document_output_takes_lines_after_sentinel():
    text = "blah blah prompt echo\n### Clauses ###\n- clause one\n\n* clause two\n"
    assert ec.parse_document_output(text) == ["clause one", "clause two"]


def test_parse_document_output_without_sentinel_uses_whole_text():
    assert ec.parse_document_output("a\nb") == ["a", "b"]


# ---------------------------------------------------------------------------
# End to end with a fake model
# ---------------------------------------------------------------------------
def fake_generate(prompt: str) -> str:
    """Pretend model: echoes any sentence mentioning 'audit' from the paragraph, else NO_MATCH."""
    body = prompt.split("Paragraph:\n", 1)[-1].split("\n\nReturn", 1)[0]
    hits = [s.strip() for s in body.split(". ") if "audit" in s.lower()]
    return "\n".join(h if h.endswith(".") else h + "." for h in hits) if hits else ec.NO_MATCH


def test_extract_paragraph_mode_traces_each_clause_to_its_paragraph():
    paras = ["Preamble.", "Licensor may audit the records of Licensee. Notice is thirty days.",
             "Payment terms are net 30."]
    rows = ec.extract(paras, fake_generate)
    assert rows == [{
        "Paragraph ID": 2,
        "Source Text": paras[1],
        "Extracted Content": "Licensor may audit the records of Licensee.",
    }]


def test_extract_document_mode_uses_one_prompt():
    calls = []

    def gen(prompt):
        calls.append(prompt)
        return f"{ec.DOC_SENTINEL}\nclause A\nclause B"

    rows = ec.extract(["p1", "p2", "p3"], gen, mode="document")
    assert len(calls) == 1 and "p1\np2\np3" in calls[0]
    assert [r["Extracted Content"] for r in rows] == ["clause A", "clause B"]


def test_write_excel_has_expected_columns(tmp_path):
    import pandas as pd
    out = tmp_path / "o.xlsx"
    ec.write_excel([{"Paragraph ID": 1, "Source Text": "s", "Extracted Content": "c"}], out)
    df = pd.read_excel(out)
    assert list(df.columns) == ["Paragraph ID", "Source Text", "Extracted Content"]
    assert df.iloc[0]["Extracted Content"] == "c"


def test_cli_dry_run_reports_prefilter(capsys):
    assert ec.main(["--input", str(CONTRACT), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "pass the keyword pre-filter" in out
