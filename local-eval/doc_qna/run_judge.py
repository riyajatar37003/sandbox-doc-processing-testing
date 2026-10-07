"""Unified judge script for all eval datasets — run after collecting responses with pytest.

Usage:
    python tests/qna/run_eval_judge.py --dataset images [--instance ALIAS] [--limit N]
    python tests/qna/run_eval_judge.py --dataset office [--instance ALIAS]

Reads:  tests/resources/{dataset_dir}/responses_{instance}.json  (images)
        tests/resources/{dataset_dir}/responses_office_{instance}.json  (office)
        tests/qna/judge_prompt.md
Writes: tests/resources/{dataset_dir}/judge_results_{instance}.json
        tests/resources/{dataset_dir}/judge_summary_{instance}.txt

Skips cases where unsupported_format=true (FILE_NOT_FOUND).
"""
from __future__ import annotations

import argparse
import json
import time
import requests
import urllib3
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESOURCES_DIR = Path(__file__).resolve().parent
JUDGE_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "judge_prompt.md"

API_URL = "https://llmproxy-subprod2-gateway-skuld005.ycg3.service-now.com/azure/openai/deployments/gpt-4.1/chat/completions?api-version=2024-12-01-preview"
API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Auth-Sub-Identities": "scope=sn_azure_openai,user=system,source=javascript,client=InternalRESTClient",
    "x-now-transaction-id": "dummy-transaction-id",
    "x-now-azure-openai-spoke-version": "dummy-version",
    "X-SNC-INTEGRATION-SOURCE": "dummy-integration-source",
}

DATASET_CONFIG = {
    "office": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_office_{instance}.json",
        "file_key": "file",
    },
    "pdf": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_pdf_{instance}.json",
        "file_key": "file",
    },
    "combo": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_combo_{instance}.json",
        "file_key": "file",
    },
    "images": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_images_{instance}.json",
        "file_key": "file",
    },
    "image": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_image_{instance}.json",
        "file_key": "file",
    },
    "multifile": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_multifile_{instance}.json",
        "file_key": "file",
    },
    "routing": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_routing_{instance}.json",
        "file_key": "file",
    },
    "split": {
        "dir": ".",
        "responses_filename": lambda instance: f"responses_split_{instance}.json",
        "file_key": "file",
    },
}


def load_judge_prompt() -> str:
    return JUDGE_PROMPT_FILE.read_text()


def call_judge(cases: list[dict]) -> list[dict]:
    judge_prompt = load_judge_prompt()

    user_content = json.dumps([
        {
            "question_id": c["case_id"],
            "question": c["question"],
            "predicted_answer": c["bot_response"],
            "ground_truth": c["gold_standard"],
        }
        for c in cases
    ], indent=2)

    payload = {
        "messages": [
            {"role": "system", "content": judge_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4096,
        "temperature": 0,
        "seed": 0,
    }

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(API_URL, headers=API_HEADERS, json=payload, verify=False, timeout=600)
            response.raise_for_status()
            raw = response.json()

            text = raw["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text.strip())
            return parsed["evaluations"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                wait = 10 * attempt
                print(f"    Retry {attempt}/{max_retries} after timeout ({wait}s wait)...")
                time.sleep(wait)
            else:
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, choices=list(DATASET_CONFIG.keys()), help="Dataset to judge")
    parser.add_argument("--instance", default="local", help="Instance alias (default: local)")
    parser.add_argument("--limit", type=int, default=None, help="Only judge first N scoreable cases (for trial runs)")
    args = parser.parse_args()

    config = DATASET_CONFIG[args.dataset]
    dataset_dir = RESOURCES_DIR / config["dir"]
    file_key = config["file_key"]
    instance = args.instance

    responses_file = dataset_dir / config["responses_filename"](instance)
    results_file = dataset_dir / f"judge_results_{instance}.json"
    summary_file = dataset_dir / f"judge_summary_{instance}.txt"

    if not responses_file.exists():
        print(f"No responses file found at {responses_file}")
        return

    with open(responses_file) as f:
        responses = json.load(f)

    scoreable = {k: v for k, v in responses.items() if not v.get("unsupported_format")}
    unsupported = {k: v for k, v in responses.items() if v.get("unsupported_format")}

    if args.limit:
        scoreable = dict(list(sorted(scoreable.items(), key=lambda x: x[0]))[:args.limit])

    print(f"Dataset: {args.dataset} | Instance: {instance}")
    print(f"Judging {len(scoreable)} responses ({len(unsupported)} skipped as unsupported)...\n")

    cases = [
        {"case_id": k, **v}
        for k, v in sorted(scoreable.items(), key=lambda x: x[0])
    ]

    # Judge in chunks — one call with all cases overflows max_tokens (truncated JSON =>
    # parse error => every case wrongly marked judge_error). Chunking keeps each reply
    # small; a failed chunk only affects its own cases. Final tally is over ALL verdicts,
    # so the accuracy number is identical to a single call — it just doesn't truncate.
    judgments_by_id: dict[str, dict] = {}
    CHUNK = 8
    for start in range(0, len(cases), CHUNK):
        batch = cases[start:start + CHUNK]
        try:
            for ev in call_judge(batch):
                judgments_by_id[str(ev["question_id"])] = ev
        except Exception as e:
            print(f"Judge chunk {start // CHUNK + 1} failed: {e}")
            for case in batch:
                judgments_by_id[case["case_id"]] = {
                    "question_id": case["case_id"],
                    "is_correct": False,
                    "explanation": f"Judge error: {e}",
                    "errors": [{"class": "judge_error", "explanation": str(e)}],
                    "warnings": [],
                }

    results = []
    for case in cases:
        cid = case["case_id"]
        ev = judgments_by_id.get(cid, {})
        status = "PASS" if ev.get("is_correct") else "FAIL"
        print(f"  Case {cid:>2} — {case.get('query_type', ''):<25} {status}  {ev.get('explanation', '')[:80]}")
        results.append({
            "case_id": cid,
            file_key: case.get(file_key, ""),
            "query_type": case.get("query_type", ""),
            "question": case["question"],
            "bot_response": case["bot_response"],
            "gold_standard": case["gold_standard"],
            "format_size": case.get("format_size", ""),
            "is_correct": ev.get("is_correct", False),
            "explanation": ev.get("explanation", ""),
            "errors": ev.get("errors", []),
            "warnings": ev.get("warnings", []),
            "unsupported_format": False,
            "ttft_ms": case.get("ttft_ms"),
            "sandbox_runs": case.get("sandbox_runs", []),
            "num_sandbox_runs": case.get("num_sandbox_runs", 0),
            "all_sandbox_passed": case.get("all_sandbox_passed", False),
        })

    for case_id, entry in sorted(unsupported.items(), key=lambda x: x[0]):
        results.append({
            "case_id": case_id,
            file_key: entry.get(file_key, ""),
            "query_type": entry.get("query_type", ""),
            "question": entry["question"],
            "bot_response": entry["bot_response"],
            "gold_standard": entry["gold_standard"],
            "format_size": entry.get("format_size", ""),
            "is_correct": None,
            "explanation": "File not present — format unsupported or not provided",
            "errors": [],
            "warnings": [],
            "unsupported_format": True,
            "ttft_ms": entry.get("ttft_ms"),
            "sandbox_runs": entry.get("sandbox_runs", []),
            "num_sandbox_runs": entry.get("num_sandbox_runs", 0),
            "all_sandbox_passed": entry.get("all_sandbox_passed", False),
        })

    results.sort(key=lambda r: r["case_id"])

    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    scored = [r for r in results if r["is_correct"] is not None]
    passed = sum(1 for r in scored if r["is_correct"])
    total = len(scored)
    total_cases = len(results)
    pct = passed / total * 100 if total else 0
    support_pct = total / total_cases * 100 if total_cases else 0

    # Latency: time-to-first-token (TTFT) across cases that actually produced a response.
    ttfts = sorted(r["ttft_ms"] for r in results if isinstance(r.get("ttft_ms"), (int, float)))
    summary_lines = [
        f"{args.dataset.capitalize()} Eval Results ({instance})",
        "=" * 60,
        f"Format support rate: {total}/{total_cases} cases ({support_pct:.1f}%)",
        f"Answer accuracy:     {passed}/{total} scored ({pct:.1f}%)",
    ]
    if ttfts:
        n = len(ttfts)
        mean = sum(ttfts) / n
        median = ttfts[n // 2] if n % 2 else (ttfts[n // 2 - 1] + ttfts[n // 2]) / 2
        p95 = ttfts[min(n - 1, int(round(0.95 * (n - 1))))]
        summary_lines.append(
            f"Latency (TTFT ms):   n={n}  mean={mean:.0f}  median={median:.0f}  "
            f"min={ttfts[0]:.0f}  max={ttfts[-1]:.0f}  p95={p95:.0f}"
        )
    else:
        summary_lines.append("Latency (TTFT ms):   no ttft data recorded")

    sandbox_total = sum(r.get("num_sandbox_runs", 0) for r in results)
    sandbox_passed = sum(1 for r in results if r.get("all_sandbox_passed"))
    sandbox_cases = sum(1 for r in results if r.get("num_sandbox_runs", 0) > 0)
    summary_lines.append(
        f"Sandbox runs:        {sandbox_total} total across {sandbox_cases} cases  "
        f"({sandbox_passed}/{sandbox_cases} cases all-passed)"
    )
    summary_lines.append("")

    by_file: dict[str, list] = {}
    for r in results:
        by_file.setdefault(r[file_key], []).append(r)

    for fname, cases_for_file in sorted(by_file.items()):
        scored_cases = [c for c in cases_for_file if c["is_correct"] is not None]
        if scored_cases:
            fp = sum(1 for c in scored_cases if c["is_correct"])
            summary_lines.append(f"{fname}: {fp}/{len(scored_cases)}")
        else:
            summary_lines.append(f"{fname}: FILE_NOT_FOUND")
        for c in cases_for_file:
            if c["is_correct"] is None:
                summary_lines.append(f"  - [{c['case_id']:>2}] {c['query_type']} (unsupported)")
            else:
                mark = "✓" if c["is_correct"] else "✗"
                ttft = c.get("ttft_ms")
                ttft_str = f"  ({ttft:.0f}ms)" if isinstance(ttft, (int, float)) else ""
                n_runs = c.get("num_sandbox_runs", 0)
                sb_str = f"  [sandbox: {n_runs} run{'s' if n_runs != 1 else ''}]" if n_runs else ""
                summary_lines.append(f"  {mark} [{c['case_id']:>2}] {c['query_type']}{ttft_str}{sb_str}")
                if not c["is_correct"]:
                    summary_lines.append(f"       {c['explanation'][:100]}")
                    for err in c["errors"]:
                        summary_lines.append(f"       [{err['class']}] {err['explanation'][:80]}")
    summary_lines.append("")

    failures = [r for r in scored if not r["is_correct"]]
    if failures:
        summary_lines.append(f"FAILURES ({len(failures)}):")
        summary_lines.append("-" * 60)
        for r in failures:
            summary_lines.append(f"\nCase {r['case_id']} — {r['query_type']} — {r[file_key]}")
            summary_lines.append(f"Q: {r['question']}")
            summary_lines.append(f"Bot: {r['bot_response'][:300]}")
            summary_lines.append(f"Gold: {r['gold_standard']}")
            summary_lines.append(f"Judge: {r['explanation']}")
            for err in r["errors"]:
                summary_lines.append(f"  [{err['class']}] {err['explanation']}")

    summary = "\n".join(summary_lines)
    print("\n" + summary)

    with open(summary_file, "w") as f:
        f.write(summary)

    print(f"\nSaved results to {results_file}")
    print(f"Saved summary to {summary_file}")


if __name__ == "__main__":
    main()
