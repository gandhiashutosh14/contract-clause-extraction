# Development notes

How the extractor was built, refined and verified.

## Original development (spring 2025)

The extractor was written by Ashutosh Gandhi as a take-home for a "Research Scientist" hiring
exercise: extract every "Audit & Inspection Rights" clause from a supplied contract under a
written rubric and have the program write the results to Excel. Four script variants survived,
plus a fifth driven by Ollama:

- `audit_solution.py`: `google/flan-t5-large`, one prompt per paragraph, batched.
- `audit_solution_mistral.py`: `mistralai/Mistral-7B-Instruct-v0.1`, one prompt over the whole
  document with a `### Clauses ###` sentinel, sampling decoding.
- `audit_solution_mistral_quantized.py`: the same with a GPTQ 4-bit checkpoint.
- `audit_solution_t5_few_shot.py`: `google/flan-t5-base` with a keyword pre-filter, a `NO_MATCH`
  convention, greedy decoding, and traceability columns in the output.

Two things in the original scripts were not published: a Hugging Face access token hardcoded in
the two Mistral variants (removed; gated models now use `huggingface-cli login`), and the hiring
company's contract document (replaced by a public one).

## Refinement, 2026-09-16

### Planning

- On review, the four variants had output files but no comparison between them, the contract
  belonged to the hiring company, and a token was hardcoded.
- Plan: consolidate the variants into one CLI with both extraction modes, make everything except
  the model call unit-testable, replace the contract with a public one, run the CPU-feasible
  models for real, and report what they produced.

### Iterations

1. Found a public replacement contract through the SEC full-text search
   (`efts.sec.gov`, query "right to audit"): a Strategic Alliance Agreement filed as Exhibit
   10.17, which has a full Audit of Records article. Converted the HTML exhibit to one paragraph
   per line (189 paragraphs, cp1252 decoding to keep the curly quotes intact) and generated a
   `.docx` from it with `python-docx`, verifying the round trip is lossless.
2. Wrote `extract_clauses.py`: rubric constants, `.docx`/`.txt` reader, keyword pre-filter with
   1-based paragraph ids, prompt builders for both modes, output parsers, two backends, a
   model-agnostic `extract()` driver, Excel writer, and a CLI with `--dry-run`.
3. Wrote 11 tests driving the pipeline with a fake generator, including one against the bundled
   contract that asserts the pre-filter keeps a small fraction of paragraphs and retains the
   audit article.
4. Ran `google/flan-t5-base` on CPU, then `google/flan-t5-large`.

### Debugging

- `transformers` 5.17 removed the `text2text-generation` pipeline task, so the first real run
  crashed at model construction. Replaced both backends with direct `AutoTokenizer` +
  `AutoModel...generate` calls, which work on 4.x and 5.x.
- One test asserted the prompt contained exactly 1500 `x` characters; the rubric text itself
  contains the letter, so the assertion was wrong. Rewritten to check for the 1500-character run.
- FLAN-T5-base did not extract clauses; on 2 of 11 paragraphs it returned the literal label
  `'Audit and Inspection Rights'`. Added a label-echo guard to the paragraph parser with a test
  that pins the observed output, then re-ran (0 rows, correctly).

### Verification

| Check | Result |
|---|---|
| `pytest -q` | 12 passed in 0.64s |
| `--dry-run` on the sample `.docx` | 189 paragraphs, 11 pass the pre-filter |
| `google/flan-t5-base`, paragraph mode, CPU | load 11 s, inference 8.7 s, 0 clauses (label echoes dropped) |
| `google/flan-t5-large`, paragraph mode, CPU | load 223 s (incl. download), inference 89 s, 9 rows: 8 correct, 1 false positive (paragraph 11, the Confidential Information definition) |

Ground truth for "correct" is the author's own reading of Articles 9 and 10 against the rubric.

**Not verified:** the causal backend (`--mode document`) and the `--quantized` path; no GPU was
available. They are faithful ports of the original Mistral scripts but have not been executed
in this form.

### Outcome

The repository starts from a clean history: no token and no hiring-company document was ever
committed.
