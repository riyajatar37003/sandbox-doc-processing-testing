# sandbox-doc-processing-testing

Unified automation for ServiceNow Now Assist document processing:

1. **`qna_eval/`** — upload a document (PDF, DOCX, image, PPTX, ...) and ask a question about it.
2. **`doc_gen/`** — generate a document (PPT/PDF/DOCX) from an utterance sequence and/or an attached source file.

---

# 🟢 Quick Start (for anyone, no coding background needed)

Follow these steps in order. Don't skip any. Every command below should be typed (or copy-pasted) exactly as shown into the **Terminal** app.

## Step 0: Open Terminal

- **Mac**: Press `Cmd + Space`, type `Terminal`, press Enter. A black/white window opens — this is where you'll type commands.
- **Windows**: Use "Git Bash" (install from step 1 below) instead of Command Prompt.

Everything below assumes you're typing into this Terminal window.

## Step 1: Check if the required tools are installed

Copy-paste each line below **one at a time**, press Enter, and read the output.

```bash
python3 --version
```
You should see something like `Python 3.9.x` or higher. If instead you see `command not found`:
- Mac: install Python from https://www.python.org/downloads/ (download the macOS installer, run it, click through the installer).
- Then close Terminal, reopen it, and re-run `python3 --version` to confirm.

```bash
git --version
```
You should see something like `git version 2.x`. If `command not found`:
- Mac: run `xcode-select --install` in Terminal, click "Install" in the popup, wait for it to finish, then re-run `git --version`.

## Step 2: Download (clone) this project

Pick a folder on your computer where you want this project to live (e.g. your Desktop), then run:

```bash
cd ~/Desktop
git clone git@github.com:riyajatar37003/sandbox-doc-processing-testing.git
cd sandbox-doc-processing-testing
```

If you get a permission/key error on `git clone`, ask whoever gave you access to this repo to add your GitHub account as a collaborator, or use the HTTPS link instead:
```bash
git clone https://github.com/riyajatar37003/sandbox-doc-processing-testing.git
```

You are now "inside" the project folder in Terminal. Every command from here on assumes you're still in this folder (if you close Terminal and reopen it later, run `cd ~/Desktop/sandbox-doc-processing-testing` again first).

## Step 3: Install the project's dependencies

This installs the small number of Python packages the scripts need (requests, python-dotenv, etc.):

```bash
pip3 install -r requirements.txt
```

If you see `pip3: command not found`, try `pip install -r requirements.txt` instead.

You should see lines ending in "Successfully installed ..." (or "Requirement already satisfied" if you have them already). Either is fine.

## Step 4: Set up your instance credentials (one-time)

The scripts need to know which ServiceNow instance to talk to and with what login. This information goes in a file that is **never uploaded to GitHub** (it's private to your machine).

```bash
cp config/.env.example qna_eval/.env.instance
```

Now open the new file `qna_eval/.env.instance` in any text editor (TextEdit, VS Code, Notepad, etc.) and fill in the real values:

```
SNC_HOST=your-instance.service-now.com
SNC_PROTOCOL=https
INSTANCE_NAME=your-instance
NW_USERNAME=your.username
NW_PASSWORD=your-password
OAUTH_CLIENT_ID=46e42d08770746f1802167828fcc6132
OAUTH_REDIRECT_URI=/api/snc/aiexauth/oauth/authorize
DEPLOYMENT_DOC_ID=c86a62e2c7022010099a308dc7c26022
VERIFY_SSL=false
```

- `SNC_HOST` / `INSTANCE_NAME`: your ServiceNow instance name, e.g. `nwdemo.service-now.com` / `nwdemo`.
- `NW_USERNAME` / `NW_PASSWORD`: your login for that instance.
- `DEPLOYMENT_DOC_ID`: leave the default unless you've been told to use a different one.
- Ask the project owner if you don't know what values to use here.

Save the file.

## Step 5: Test that your credentials work

```bash
python3 qna_eval/run_qna.py --check-session
```

If everything is correct, the last line printed will look like:
```
SESSION ALIVE. session=... user=...
```

If you instead see an error, check Step 4 — the most common mistake is a typo in `SNC_HOST`, `NW_USERNAME`, or `NW_PASSWORD`.

## Step 6a: Ask a question about a document (QnA)

```bash
python3 qna_eval/run_qna.py --file "/full/path/to/your/file.pdf" --question "What is the total budget?"
```

Replace `/full/path/to/your/file.pdf` with the actual path to your file (drag-and-drop the file into Terminal after typing `--file ` to auto-fill the path), and replace the question text with whatever you want to ask.

This supports PDF, DOCX, images (PNG/JPG), and PPTX files.

**Where is the answer?** After it finishes, look in the folder `qna_eval/results/`. You'll find a new file named like `qna_20261002_103000.json` — open it in any text editor or VS Code. It contains your question, the answer, and (if applicable) the code the system ran to analyze your file.

This can take 30 seconds to a few minutes depending on the file size and question — that's normal, just wait.

## Step 6b: Generate a document (PPT/PDF/DOCX) from a file

First, put your source file (e.g. a `.md` or `.pdf` with the content you want turned into a presentation) into the folder `doc_gen/datasets/sources/`.

Then run:

```bash
python3 doc_gen/run_doc_generation.py --file doc_gen/datasets/sources/your_file.md
```

**Where is the generated file?** Look in `doc_gen/datasets/generated/` — a new folder named with today's date/time will appear (e.g. `20261002_103000/`), containing:
- the generated PowerPoint (or PDF/DOCX)
- `_run.log` — the full step-by-step console output for that run (what was asked, what came back, what was saved)
- `_timing.json` — how long each step took: time-to-first-byte (`ttfb_ms`), total response time (`response_time_ms`), and wall-clock seconds (`elapsed_s`) for each turn, plus the total time for the whole file

This also sets `DOC_GEN_*` environment variables for the instance it should use — see the "Document generation" section below for details. If you don't set them, it falls back to the shared `nwdemo` test instance defaults, which may or may not work for you; ask the project owner for the right instance/login to use for generation if `run_doc_generation.py` fails with a "missing env vars" error.

## Troubleshooting

| Problem | What to do |
|---|---|
| `command not found: python3` | Install Python (Step 1). |
| `ModuleNotFoundError: No module named 'dotenv'` | Run `pip3 install -r requirements.txt` again (Step 3). |
| `--host/--username/--password ... are required` | You haven't filled in `qna_eval/.env.instance` correctly (Step 4), or you're running from the wrong folder. |
| Script seems "stuck" with no new output for a while | This is normal — the system is uploading your file and the AI is working. Wait a few minutes before assuming something is wrong. |
| `NotOpenSSLWarning` printed at the top | Harmless warning, ignore it — the script still works. |
| `missing env vars: DOC_GEN_PASSWORD, ...` | Set the `DOC_GEN_*` environment variables before running `run_doc_generation.py` (see below), or ask the project owner for the generation instance credentials. |

---

# 📘 Reference (for developers)

Both runners share one `nextwave/` client library (auth, session, SSE chatkit, AIA sandbox-trace fetch) extracted from the original `pptx-eval/deck_eval/generation` and `pptx-eval/qna-eval` codebases.

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

Config via env vars (export these in your shell before running, e.g. `export DOC_GEN_PASSWORD=...`): `DOC_GEN_INSTANCE`, `DOC_GEN_USERNAME`, `DOC_GEN_PASSWORD`, `DOC_GEN_DEPLOYMENT_DOC_ID`, `DOC_GEN_USERNAME_POOL` (comma-separated accounts used round-robin when `--workers > 1`, since the backend serializes conversation creation per account).

Each run writes, into its output folder, alongside the generated document(s):
- `_run.log` — full console transcript for that run (every `log()` line, timestamped)
- `_timing.json` — per-turn `ttfb_ms` / `response_time_ms` / `elapsed_s`, plus per-file total `elapsed_s` and `ok` status, one entry per source file processed in that run

## Project layout

```
sandbox-doc-processing-testing/
  nextwave/              # shared client lib: auth, session, transport, chatkit (SSE), parsing, aia trace
  qna_eval/
    run_qna.py           # upload + ask + answer + sandbox trace
    .env.instance        # your private credentials (not committed to git)
    results/             # output JSON per run
  doc_gen/
    run_doc_generation.py
    utterances.json      # turn sequence (editable, no code changes needed)
    datasets/
      sources/           # input files to batch-process
      generated/         # generated PPT/PDF/DOCX output, one timestamped folder per run
  config/.env.example    # template for qna_eval/.env.instance
```
