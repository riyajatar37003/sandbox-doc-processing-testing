# sandbox-doc-processing-testing

Unified automation for ServiceNow Now Assist document processing:

1. **`qna_eval/`** — upload a document (PDF, DOCX, image, PPTX, ...) and ask a question about it.
2. **`doc_gen/`** — generate a document (PPT/PDF/DOCX) from an utterance sequence and/or an attached source file.

Both share one `nextwave/` client library (auth, session, SSE chatkit, AIA sandbox-trace fetch) extracted from the original `pptx-eval/deck_eval/generation` and `pptx-eval/qna-eval` codebases.

## Setup

```bash
cd sandbox-doc-processing-testing
pip install -r requirements.txt
cp config/.env.example qna_eval/.env.instance   # fill in SNC_HOST / NW_USERNAME / NW_PASSWORD / DEPLOYMENT_DOC_ID
```

## 1. QnA: `qna_eval/run_qna.py`

```bash
# Single file, single question
python3 qna_eval/run_qna.py --file /path/to/report.pdf --question "What is the total budget?"

# Batch (JSON list of {id, file, question}), 4 parallel workers
python3 qna_eval/run_qna.py --cases-file cases.json --workers 4

# Just test auth
python3 qna_eval/run_qna.py --check-session
```

Each case uploads the file, asks the question over the ChatKit SSE endpoint, and (unless `--no-trace`) fetches the AIA execution trace to capture any sandbox (bash) code the agent ran against the file. Results are written incrementally to `qna_eval/results/qna_<timestamp>.json` (or `--output <path>`).

`cases.json` format:
```json
[
  {"id": "case-1", "file": "/path/to/a.pdf", "question": "..."},
  {"id": "case-2", "file": "/path/to/b.docx", "question": "..."}
]
```

Credentials come from `qna_eval/.env.instance` or CLI flags: `--host --username --password --deployment-doc-id` (env equivalents: `SNC_HOST`, `NW_USERNAME`, `NW_PASSWORD`, `DEPLOYMENT_DOC_ID`).

## 2. Document generation: `doc_gen/run_doc_generation.py`

```bash
# Full batch over doc_gen/datasets/sources/*.md, fresh timestamped output folder
python3 doc_gen/run_doc_generation.py

# One specific file
python3 doc_gen/run_doc_generation.py --file /path/to/one.md

# Multiple source files concurrently (one NextWave session per worker)
python3 doc_gen/run_doc_generation.py --workers 4

# Different source pattern (pdf, docx, etc.) and fixed/reusable output dir
python3 doc_gen/run_doc_generation.py --source-dir doc_gen/datasets/sources \
  --output-dir doc_gen/datasets/generated --pattern "*.pdf"
```

The turn sequence lives in `doc_gen/utterances.json` — by default: upload the source file and ask for key-point extraction, then (same thread) ask for a generated PowerPoint. Edit that file to add/change turns (e.g. ask for a PDF or DOCX instead) without touching the script.

Config via env vars: `DOC_GEN_INSTANCE`, `DOC_GEN_USERNAME`, `DOC_GEN_PASSWORD`, `DOC_GEN_DEPLOYMENT_DOC_ID`, `DOC_GEN_USERNAME_POOL` (comma-separated accounts used round-robin when `--workers > 1`, since the backend serializes conversation creation per account).

## Project layout

```
sandbox-doc-processing-testing/
  nextwave/              # shared client lib: auth, session, transport, chatkit (SSE), parsing, aia trace
  qna_eval/
    run_qna.py           # upload + ask + answer + sandbox trace
    results/             # output JSON per run
  doc_gen/
    run_doc_generation.py
    utterances.json      # turn sequence (editable, no code changes needed)
    datasets/
      sources/           # input files to batch-process
      generated/         # generated PPT/PDF/DOCX output, one timestamped folder per run
  config/.env.example    # template for qna_eval/.env.instance
```
