"""
Extract "Audit & Inspection Rights" clauses from a contract with an open LLM,
writing the results straight to Excel.

Two extraction modes, selectable with --mode:

  paragraph   one prompt per (pre-filtered) paragraph; suits small seq2seq
              models such as google/flan-t5-base that run on a laptop CPU.
  document    one prompt with the whole contract; suits instruction-tuned
              causal models such as Mistral-7B-Instruct (GPU, optionally 4-bit).

Everything that is not the model call (reading the .docx, the keyword
pre-filter, prompt building, output parsing, Excel writing) is a plain
function so it can be unit-tested with a fake generator.

Usage:
  python extract_clauses.py --input contracts/sample_contract.docx --output out.xlsx
  python extract_clauses.py --input contracts/sample_contract.docx --output out.xlsx \
      --mode document --model mistralai/Mistral-7B-Instruct-v0.2 --quantized
  python extract_clauses.py --input contracts/sample_contract.docx --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The extraction rubric (what counts, what does not)
# ---------------------------------------------------------------------------
INCLUDE = [
    "The right of one party to audit the books and records of another party.",
    "The right of one party to give or receive access to the books and records of another party.",
    "The right to inspect or examine the premises, books, accounts, records, papers, or other specified items.",
    "The obligation of one party to maintain or retain records, files, books, accounts, or papers for the purpose of audit or inspection.",
    "The obligation of a party to follow certain procedures and rules with respect to conducting the audit.",
]
EXCLUDE = [
    "Governmental and regulatory audits and inspections.",
    "Inspection rights on the delivery of products specifically if they are related to acceptance testing.",
    "Definitions for 'auditor'.",
]

# Cheap lexical gate: a paragraph with none of these words cannot be an audit clause,
# so it never reaches the model. Cuts the number of model calls by roughly 10x.
KEYWORDS = {"audit", "inspect", "record", "access", "retain", "examine", "books", "premises"}

NO_MATCH = "NO_MATCH"
DOC_SENTINEL = "### Clauses ###"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def read_paragraphs(path: Path) -> List[str]:
    """Non-empty paragraphs from a .docx or a plain-text file (one paragraph per line)."""
    if path.suffix.lower() == ".docx":
        from docx import Document  # imported here so tests on .txt need no python-docx
        return [p.text.strip() for p in Document(str(path)).paragraphs if p.text.strip()]
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prefilter(paragraphs: Sequence[str], keywords: Iterable[str] = KEYWORDS) -> List[Tuple[int, str]]:
    """Keep (1-based index, text) for paragraphs that mention at least one keyword."""
    kws = [k.lower() for k in keywords]
    return [(i + 1, p) for i, p in enumerate(paragraphs) if any(k in p.lower() for k in kws)]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _rubric_block() -> str:
    inc = "\n".join(f"- {x}" for x in INCLUDE)
    exc = "\n".join(f"- {x}" for x in EXCLUDE)
    return f"INCLUDE:\n{inc}\n\nEXCLUDE:\n{exc}"


def build_paragraph_prompt(paragraph: str, max_chars: int = 1500) -> str:
    clean = re.sub(r"\s+", " ", paragraph)[:max_chars]
    return (
        "Extract ONLY the 'Audit and Inspection Rights' clause text from this contract paragraph.\n\n"
        f"{_rubric_block()}\n\n"
        f"Paragraph:\n{clean}\n\n"
        f"Return the exact matching text fragments, one per line. If nothing matches, return {NO_MATCH}."
    )


def build_document_prompt(text: str) -> str:
    return (
        "Extract all occurrences of the 'Audit and Inspection Rights' clause from the contract below.\n\n"
        f"{_rubric_block()}\n\n"
        "Return each matching clause verbatim on its own line, with no commentary.\n\n"
        f"Contract:\n{text}\n\n"
        f"Output the extracted clauses after this line:\n{DOC_SENTINEL}\n"
    )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------
_LABEL_ECHO = re.compile(r"^[\s'\"“”‘’]*audit\s*(and|&)\s*inspection\s*rights?[\s'\"“”‘’.:]*$", re.I)


def parse_paragraph_output(text: str) -> List[str]:
    """Lines of a per-paragraph answer that look like clause text.

    Drops NO_MATCH, chatter without clause vocabulary, and the failure mode small
    models show most: echoing the label 'Audit and Inspection Rights' back instead
    of quoting the paragraph.
    """
    if not text or NO_MATCH in text:
        return []
    lines = [l.strip(" -*•\t") for l in text.splitlines()]
    return [
        l for l in lines
        if len(l) > 20
        and re.search(r"audit|inspect|record|examine|access", l, re.I)
        and not _LABEL_ECHO.match(l)
    ]


def parse_document_output(text: str) -> List[str]:
    """Clauses listed after the sentinel in a whole-document answer."""
    if not text:
        return []
    idx = text.rfind(DOC_SENTINEL)
    body = text[idx + len(DOC_SENTINEL):] if idx != -1 else text
    return [l.strip(" -*•\t") for l in body.splitlines() if l.strip(" -*•\t")]


# ---------------------------------------------------------------------------
# Model backends (only these touch transformers/torch)
# ---------------------------------------------------------------------------
class Seq2SeqBackend:
    """FLAN-T5 style text2text models. Deterministic decoding (beam search, no sampling).

    Uses the tokenizer and model directly rather than the `text2text-generation`
    pipeline, which transformers 5.x removed.
    """

    def __init__(self, model_name: str = "google/flan-t5-base", max_new_tokens: int = 256):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(self.device).eval()
        self.max_new_tokens = max_new_tokens

    def __call__(self, prompt: str) -> str:
        import torch
        inputs = self.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=1024).to(self.device)
        with torch.no_grad():
            out = self.model.generate(**inputs, do_sample=False, num_beams=2, max_new_tokens=self.max_new_tokens)
        return self.tokenizer.decode(out[0], skip_special_tokens=True)


class CausalBackend:
    """Instruction-tuned causal models (Mistral, Llama...). Gated models need `huggingface-cli login`."""

    def __init__(self, model_name: str = "mistralai/Mistral-7B-Instruct-v0.2",
                 quantized: bool = False, max_new_tokens: int = 1000):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        kwargs = {"device_map": "auto", "torch_dtype": torch.float16, "low_cpu_mem_usage": True}
        if quantized:
            from transformers import BitsAndBytesConfig
            kwargs["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True,
                                                               bnb_4bit_compute_dtype=torch.float16)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(model_name, **kwargs).eval()
        self.max_new_tokens = max_new_tokens

    def __call__(self, prompt: str) -> str:
        import torch
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(**inputs, do_sample=False, max_new_tokens=self.max_new_tokens)
        return self.tokenizer.decode(out[0], skip_special_tokens=True)


# ---------------------------------------------------------------------------
# Extraction driver (model-agnostic)
# ---------------------------------------------------------------------------
def extract(
    paragraphs: Sequence[str],
    generate: Callable[[str], str],
    *,
    mode: str = "paragraph",
    use_prefilter: bool = True,
    progress: Optional[Callable[[Iterable], Iterable]] = None,
) -> List[Dict[str, object]]:
    """Return rows of {"Paragraph ID", "Source Text", "Extracted Content"}."""
    rows: List[Dict[str, object]] = []

    if mode == "document":
        text = "\n".join(paragraphs)
        for clause in parse_document_output(generate(build_document_prompt(text))):
            rows.append({"Paragraph ID": "", "Source Text": "", "Extracted Content": clause})
        return rows

    candidates = prefilter(paragraphs) if use_prefilter else [(i + 1, p) for i, p in enumerate(paragraphs)]
    iterator = progress(candidates) if progress else candidates
    for pid, para in iterator:
        for clause in parse_paragraph_output(generate(build_paragraph_prompt(para))):
            rows.append({"Paragraph ID": pid, "Source Text": para[:500], "Extracted Content": clause})
    return rows


def write_excel(rows: List[Dict[str, object]], path: Path) -> None:
    import pandas as pd
    columns = ["Paragraph ID", "Source Text", "Extracted Content"]
    pd.DataFrame(rows, columns=columns).to_excel(path, index=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Extract Audit & Inspection Rights clauses from a contract to Excel.")
    ap.add_argument("--input", required=True, type=Path, help=".docx or .txt contract")
    ap.add_argument("--output", type=Path, default=Path("audit_clauses.xlsx"))
    ap.add_argument("--mode", choices=["paragraph", "document"], default="paragraph")
    ap.add_argument("--backend", choices=["flan-t5", "causal"], default=None,
                    help="default: flan-t5 for paragraph mode, causal for document mode")
    ap.add_argument("--model", default=None, help="Hugging Face model id (backend default if omitted)")
    ap.add_argument("--quantized", action="store_true", help="load causal model in 4-bit (bitsandbytes)")
    ap.add_argument("--no-prefilter", action="store_true", help="send every paragraph to the model")
    ap.add_argument("--dry-run", action="store_true", help="only report what the pre-filter would send")
    args = ap.parse_args(argv)

    paragraphs = read_paragraphs(args.input)
    kept = prefilter(paragraphs)
    print(f"{len(paragraphs)} paragraphs, {len(kept)} pass the keyword pre-filter")
    if args.dry_run:
        for pid, para in kept:
            print(f"  [{pid}] {para[:100]}")
        return 0

    backend = args.backend or ("causal" if args.mode == "document" else "flan-t5")
    t0 = time.perf_counter()
    if backend == "flan-t5":
        generate = Seq2SeqBackend(args.model or "google/flan-t5-base")
    else:
        generate = CausalBackend(args.model or "mistralai/Mistral-7B-Instruct-v0.2", quantized=args.quantized)
    print(f"model loaded in {time.perf_counter() - t0:.1f}s")

    try:
        from tqdm import tqdm
        progress = lambda it: tqdm(list(it), desc="extracting")  # noqa: E731
    except ImportError:
        progress = None

    t1 = time.perf_counter()
    rows = extract(paragraphs, generate, mode=args.mode, use_prefilter=not args.no_prefilter, progress=progress)
    write_excel(rows, args.output)
    print(f"{len(rows)} clause fragments written to {args.output} in {time.perf_counter() - t1:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
