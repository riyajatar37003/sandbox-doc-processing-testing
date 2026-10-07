"""Batch judge: converts JSONL eval results → judge JSON format, runs judge for each run.

Usage:
    python run_judge_batch.py --datasets combo pdf office
    python run_judge_batch.py --datasets combo --runs 1 3 5
    python run_judge_batch.py --jsonl results/office/withskills/  # all JSONL in dir
    python run_judge_batch.py --jsonl path/to/file.jsonl           # single file
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from run_judge import main as judge_main

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"


def jsonl_to_judge_json(jsonl_path: Path) -> dict:
    """Convert JSONL eval records to the dict format run_judge expects."""
    records: dict[str, dict] = {}
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            cid = rec["id"]
            has_error = bool(rec.get("error"))
            records[cid] = {
                "question": rec["test_query"],
                "bot_response": rec.get("final_answer", ""),
                "gold_standard": rec["gold_standard"],
                "query_type": rec.get("query_type", ""),
                "file": rec.get("file", ""),
                "format_size": rec.get("file_size", ""),
                "unsupported_format": has_error and "not found" in rec.get("error", "").lower(),
                "ttft_ms": rec.get("elapsed_s", 0) * 1000 if rec.get("elapsed_s") else None,
                "sandbox_runs": rec.get("runs", []),
                "num_sandbox_runs": rec.get("num_runs", 0),
                "all_sandbox_passed": rec.get("all_runs_passed", False),
            }
    return records


def _judge_jsonl(jsonl_path: Path, dataset: str, run_label: str) -> None:
    """Run judge on a single JSONL file."""
    import sys

    judge_json = jsonl_to_judge_json(jsonl_path)
    instance_alias = f"judge_{run_label}"
    temp_json = HERE / f"responses_{dataset}_{instance_alias}.json"
    temp_json.write_text(json.dumps(judge_json, indent=2))

    sys.argv = [
        "run_judge.py",
        "--dataset", dataset,
        "--instance", instance_alias,
    ]
    try:
        judge_main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"  Judge failed: {e}")

    out_dir = jsonl_path.resolve().parent / "judge"
    out_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in [
        (f"judge_results_{instance_alias}.json", "judge_results.json"),
        (f"judge_summary_{instance_alias}.txt", "judge_summary.txt"),
    ]:
        src = HERE / src_name
        if src.exists():
            dst = out_dir / dst_name
            shutil.move(str(src), str(dst))
            try:
                print(f"  → {dst.relative_to(HERE)}")
            except ValueError:
                print(f"  → {dst}")

    if temp_json.exists():
        temp_json.unlink()


def _detect_dataset(jsonl_path: Path) -> str:
    """Infer dataset name from JSONL filename or ancestor directory path.

    Handles both old layout (responses_pdf_withskills_local_run1.xlsx.jsonl) and
    new timestamped layout (results/pdf/withskills/20260720_124500/responses.xlsx.jsonl)
    where the filename is generic and the dataset name is in the directory hierarchy.
    """
    name = jsonl_path.stem.replace(".xlsx", "")
    for ds in ("combo", "pdf", "office", "image", "images", "routing", "split"):
        if f"_{ds}_" in name or name.startswith(f"responses_{ds}"):
            return ds
    # Fall back to ancestor directory names (new timestamped layout)
    for part in jsonl_path.resolve().parts:
        for ds in ("combo", "pdf", "office", "image", "images", "routing", "split"):
            if part == ds:
                return ds
    return "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", nargs="+", default=["combo", "pdf", "office"])
    parser.add_argument("--runs", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    parser.add_argument("--skills-label", default="noskills")
    parser.add_argument("--instance-label", default="local")
    parser.add_argument("--jsonl", default="",
                        help="path to a .jsonl file or directory of .jsonl files (bypasses --datasets/--runs)")
    args = parser.parse_args()

    import sys

    # --- Direct JSONL mode: judge whatever files are given ---
    if args.jsonl:
        jp = Path(args.jsonl)
        if not jp.exists():
            raise SystemExit(f"Path not found: {jp}")
        jsonl_files = sorted(jp.rglob("*.jsonl")) if jp.is_dir() else [jp]
        if not jsonl_files:
            raise SystemExit(f"No .jsonl files found in {jp}")
        for jf in jsonl_files:
            dataset = _detect_dataset(jf)
            run_label = jf.stem.replace(".xlsx", "")
            print(f"\n{'='*60}")
            print(f"JUDGING: {jf.name} (dataset={dataset})")
            print(f"{'='*60}")
            _judge_jsonl(jf, dataset, run_label)
        print("\nDone.")
        return

    # --- Legacy mode: construct filenames from --datasets/--runs ---
    DATASET_FOLDER = {"images": "image"}
    DATASET_FILE_PREFIX = {"images": "image"}

    for dataset in args.datasets:
        folder_name = DATASET_FOLDER.get(dataset, dataset)
        file_prefix = DATASET_FILE_PREFIX.get(dataset, dataset)
        for run_num in args.runs:
            jsonl_name = f"responses_{file_prefix}_{args.skills_label}_{args.instance_label}_run{run_num}.xlsx.jsonl"
            jsonl_path = RESULTS_DIR / folder_name / args.skills_label / jsonl_name

            if not jsonl_path.exists():
                print(f"SKIP {dataset} run{run_num}: {jsonl_path.name} not found")
                continue

            print(f"\n{'='*60}")
            print(f"JUDGING: {dataset} run{run_num}")
            print(f"{'='*60}")

            judge_json = jsonl_to_judge_json(jsonl_path)
            instance_alias = f"{args.skills_label}_{args.instance_label}_run{run_num}"
            temp_json = HERE / f"responses_{dataset}_{instance_alias}.json"
            temp_json.write_text(json.dumps(judge_json, indent=2))

            sys.argv = [
                "run_judge.py",
                "--dataset", dataset,
                "--instance", instance_alias,
            ]
            try:
                judge_main()
            except SystemExit:
                pass
            except Exception as e:
                print(f"  Judge failed: {e}")

            out_dir = jsonl_path.resolve().parent / "judge"
            out_dir.mkdir(parents=True, exist_ok=True)
            for src_name, dst_name in [
                (f"judge_results_{instance_alias}.json", "judge_results.json"),
                (f"judge_summary_{instance_alias}.txt", "judge_summary.txt"),
            ]:
                src = HERE / src_name
                if src.exists():
                    dst = out_dir / dst_name
                    shutil.move(str(src), str(dst))
                    print(f"  → {dst.relative_to(HERE)}")

            if temp_json.exists():
                temp_json.unlink()

    print("\nDone.")


if __name__ == "__main__":
    main()
