"""Run the three experiments described in the spec (section 12).

For each experiment, generate the workload(s), run archive.py with the right
config(s), parse stats_output.txt, and print a Markdown table that can be
pasted directly into record.txt / the report.

Run from the project root:
    python3 run_experiments.py

This script writes the same files archive.py writes (output.txt, log.csv,
stats_output.txt, *.dat, *.hidx, *.bidx) and cleans them up between runs.
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def clean_db_files() -> None:
    """Remove every transient artifact so each run starts from a blank DB."""
    for fn in os.listdir(ROOT):
        if (fn.endswith(".dat") or fn.endswith(".hidx")
                or fn.endswith(".bidx") or fn.endswith(".idx")
                or fn in ("output.txt", "log.csv", "stats_output.txt")):
            os.remove(os.path.join(ROOT, fn))


def write_config(cfg: dict, name: str = "config.exp.json") -> str:
    path = os.path.join(ROOT, name)
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def generate_workload(mode: str, records: int, queries: int,
                      seed: int = 42, name: str = "workload.exp.txt",
                      int_pk: bool = False) -> str:
    path = os.path.join(ROOT, name)
    args = ["python3", "workload_generator.py",
            "--mode", mode,
            "--records", str(records),
            "--queries", str(queries),
            "--seed", str(seed)]
    if int_pk:
        args.append("--int-pk")
    with open(path, "w") as f:
        subprocess.run(args, cwd=ROOT, stdout=f, check=True)
    return path


def run_one(cfg: dict, workload_path: str) -> dict:
    """Run archive.py once and return parsed stats from stats_output.txt."""
    clean_db_files()
    cfg_path = write_config(cfg)
    # Append a final `stats` command so we always have a fresh snapshot.
    augmented = workload_path + ".aug"
    with open(workload_path) as f:
        body = f.read().rstrip()
    body += "\nstats\n"
    with open(augmented, "w") as f:
        f.write(body)
    subprocess.run(
        ["python3", "archive.py", cfg_path, augmented],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return parse_stats()


def parse_stats() -> dict:
    """Parse stats_output.txt into a flat dict."""
    text = open(os.path.join(ROOT, "stats_output.txt")).read()
    out = {}
    for ln in text.splitlines():
        m = re.match(r"Disk I/O:\s+(\d+)\s+reads,\s+(\d+)\s+writes", ln)
        if m:
            out["disk_reads"] = int(m.group(1))
            out["disk_writes"] = int(m.group(2))
            continue
        m = re.match(
            r"Buffer Pool:\s+(\d+)\s+requests,\s+(\d+)\s+hits,\s+(\d+)\s+misses\s+\(([\d\.]+)%", ln)
        if m:
            out["requests"] = int(m.group(1))
            out["hits"] = int(m.group(2))
            out["misses"] = int(m.group(3))
            out["hit_rate"] = float(m.group(4))
            continue
        m = re.match(r"Evictions:\s+(\d+)\s+\((\d+)\s+dirty", ln)
        if m:
            out["evictions"] = int(m.group(1))
            out["dirty_writebacks"] = int(m.group(2))
            continue
        m = re.match(r"Index:\s+(\S+),\s+(\d+)\s+nodes", ln)
        if m:
            out["index_strategy"] = m.group(1).rstrip(",")
            out["nodes_visited"] = int(m.group(2))
            continue
        m = re.match(r"Records:\s+(\d+)\s+scanned,\s+(\d+)\s+returned", ln)
        if m:
            out["scanned"] = int(m.group(1))
            out["returned"] = int(m.group(2))
    return out


def total_io(s: dict) -> int:
    return s["disk_reads"] + s["disk_writes"]


# --------------------------------------------------------------------------
# Experiment 1 -- LRU vs MRU on sequential & random workloads
# --------------------------------------------------------------------------
def experiment_1() -> str:
    """Report I/O count and hit rate for LRU vs MRU on two workloads."""
    out_lines = ["## Experiment 1 — LRU vs MRU\n"]
    out_lines.append(
        "Buffer pool size: 8 frames. Workloads: 200 records, 50 queries.\n"
    )
    out_lines.append(
        "| Workload    | LRU I/Os | LRU Hit Rate | MRU I/Os | MRU Hit Rate |"
    )
    out_lines.append(
        "|-------------|----------|--------------|----------|--------------|"
    )
    base_cfg = {
        "page_size": 4096, "max_records_per_page": 10,
        "buffer_pool_size": 8, "index_strategy": "heap_scan",
    }
    for mode in ("sequential", "random"):
        wl = generate_workload(mode, records=200, queries=50,
                               name=f"workload.exp1_{mode}.txt")
        row = [f"{mode:11s}"]
        for policy in ("LRU", "MRU"):
            cfg = {**base_cfg, "replacement_policy": policy}
            s = run_one(cfg, wl)
            row.append(f"{total_io(s):>8d}")
            row.append(f"{s['hit_rate']:>11.1f}%")
        out_lines.append("| " + " | ".join(row) + " |")
    return "\n".join(out_lines) + "\n"


# --------------------------------------------------------------------------
# Experiment 2 -- index strategy comparison
# --------------------------------------------------------------------------
def experiment_2() -> str:
    out_lines = ["## Experiment 2 — Index Strategy Comparison\n"]
    out_lines.append(
        "Workload: 500 records loaded, then 100 queries of one type.\n"
        "Buffer pool: 16 frames, LRU policy.\n"
    )
    out_lines.append(
        "| Query Type | heap_scan | hash_index           | bplus_tree |"
    )
    out_lines.append(
        "|------------|-----------|----------------------|------------|"
    )
    base_cfg = {
        "page_size": 4096, "max_records_per_page": 10,
        "buffer_pool_size": 16, "replacement_policy": "LRU",
    }

    # 2a -- equality queries (random mode)
    wl_eq = generate_workload("random", records=500, queries=100,
                              name="workload.exp2_eq.txt")
    eq_row = ["Equality  "]
    for strat in ("heap_scan", "hash_index", "bplus_tree"):
        cfg = {**base_cfg, "index_strategy": strat}
        s = run_one(cfg, wl_eq)
        eq_row.append(f"{total_io(s):>9d}")
    # neaten the hash column header width
    out_lines.append(
        f"| {eq_row[0]} | {eq_row[1]} | {eq_row[2]}             | {eq_row[3]}  |"
    )

    # 2b -- range queries (range mode). Use int PK so the B+ tree fast path
    # actually applies (the range field IS the PK).
    wl_rg = generate_workload("range", records=500, queries=100,
                              name="workload.exp2_rg.txt", int_pk=True)
    rg_row = ["Range     "]
    for strat in ("heap_scan", "hash_index", "bplus_tree"):
        cfg = {**base_cfg, "index_strategy": strat}
        s = run_one(cfg, wl_rg)
        if strat == "hash_index":
            rg_row.append(f"{total_io(s):>9d} (fallback)")
        else:
            rg_row.append(f"{total_io(s):>9d}")
    out_lines.append(
        f"| {rg_row[0]} | {rg_row[1]} | {rg_row[2]} | {rg_row[3]}  |"
    )
    return "\n".join(out_lines) + "\n"


# --------------------------------------------------------------------------
# Experiment 3 -- buffer pool size sensitivity
# --------------------------------------------------------------------------
def experiment_3() -> str:
    out_lines = ["## Experiment 3 — Buffer Pool Size Sensitivity\n"]
    out_lines.append(
        "Workload: random, 500 records, 200 queries. Index: bplus_tree, LRU.\n"
    )
    out_lines.append("| Buffer Size | I/Os  | Hit Rate |")
    out_lines.append("|-------------|-------|----------|")
    base_cfg = {
        "page_size": 4096, "max_records_per_page": 10,
        "replacement_policy": "LRU", "index_strategy": "bplus_tree",
    }
    wl = generate_workload("random", records=500, queries=200,
                           name="workload.exp3.txt")
    for size in (4, 8, 16, 32, 64):
        cfg = {**base_cfg, "buffer_pool_size": size}
        s = run_one(cfg, wl)
        out_lines.append(
            f"| {size:>11d} | {total_io(s):>5d} | {s['hit_rate']:>7.1f}% |"
        )
    return "\n".join(out_lines) + "\n"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def main() -> None:
    sections = []
    sections.append("# Experiment Results\n")
    sections.append(
        "Each row below was produced by running\n"
        "`python3 archive.py <config.json> <workload.txt>` and reading the "
        "resulting `stats_output.txt`.\n"
    )

    print("Running Experiment 1 (LRU vs MRU)...")
    sections.append(experiment_1())

    print("Running Experiment 2 (index comparison)...")
    sections.append(experiment_2())

    print("Running Experiment 3 (buffer size sensitivity)...")
    sections.append(experiment_3())

    # Clean up after ourselves
    clean_db_files()
    for fn in os.listdir(ROOT):
        if (fn.startswith("workload.exp") or fn == "config.exp.json"
                or fn.endswith(".aug")):
            os.remove(os.path.join(ROOT, fn))

    report = "\n".join(sections)
    print()
    print("=" * 60)
    print(report)
    # Also save it for record.txt
    out_path = os.path.join(ROOT, "experiment_results.md")
    with open(out_path, "w") as f:
        f.write(report)
    print(f"\nResults also written to {out_path}")


if __name__ == "__main__":
    main()
