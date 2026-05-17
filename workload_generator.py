"""Workload generator for the modular DBMS engine.

Produces a deterministic stream of valid commands (one per line on stdout)
according to a chosen mode. Used by the three experiments described in the
spec (section 12).

Usage
-----
    python3 workload_generator.py --mode MODE --records N --queries Q > w.txt

Modes (spec section 10)
-----------------------
    sequential : insert N records, then Q full-table scans
    random     : insert N records, then Q random equality searches (by PK)
    range      : insert N records, then Q random range queries on an int field
    mixed      : insert N records, then Q mixed ops (search / insert / delete)

The relation has 6 fields:
    pk      : str   -- primary key, distinct alphanumeric token per record
    region  : str   -- one of a small set of categories
    leader  : str   -- one of a small set of names
    score   : int   -- 0 .. 9999
    wealth  : int   -- 0 .. 99999
    spice   : int   -- 0 .. 999

(2+ int fields, 4+ fields total -- meets the spec's "at least 4 fields,
including 2+ int fields".)

Reproducibility: a fixed `--seed` (default 42) ensures every run produces the
same workload, so experiments are bit-exact reproducible.
"""

import argparse
import random
import sys

TYPE_NAME = "bench"

REGIONS = ["Caladan", "GiediPrime", "Kaitain", "Arrakis", "Salusa",
           "IxianCore", "Tleilax", "Tupile"]
LEADERS = ["Duke", "Baron", "Emperor", "Count", "Earl", "Lord", "Lady"]


def _pk_str(i: int) -> str:
    return f"k{i:05d}"


def _pk_int(i: int) -> int:
    return i


def emit_create_type(int_pk: bool = False) -> None:
    if int_pk:
        print(
            f"create type {TYPE_NAME} 6 1 "
            f"pk int region str leader str "
            f"score int wealth int spice int"
        )
    else:
        print(
            f"create type {TYPE_NAME} 6 1 "
            f"pk str region str leader str "
            f"score int wealth int spice int"
        )


def emit_insert(rng: random.Random, i: int, int_pk: bool = False) -> None:
    region = rng.choice(REGIONS)
    leader = rng.choice(LEADERS)
    score = rng.randint(0, 9999)
    wealth = rng.randint(0, 99999)
    spice = rng.randint(0, 999)
    pk = _pk_int(i) if int_pk else _pk_str(i)
    print(
        f"create record {TYPE_NAME} {pk} {region} {leader} "
        f"{score} {wealth} {spice}"
    )


def _pk(i: int, int_pk: bool = False):
    return _pk_int(i) if int_pk else _pk_str(i)


# ---- modes --------------------------------------------------------------

def mode_sequential(rng: random.Random, n: int, q: int,
                    int_pk: bool = False) -> None:
    emit_create_type(int_pk)
    for i in range(n):
        emit_insert(rng, i, int_pk)
    for _ in range(q):
        print(f"range_search {TYPE_NAME} score 0 9999")


def mode_random(rng: random.Random, n: int, q: int,
                int_pk: bool = False) -> None:
    emit_create_type(int_pk)
    for i in range(n):
        emit_insert(rng, i, int_pk)
    for _ in range(q):
        if rng.random() < 0.8:
            target = rng.randrange(n)
            print(f"search record {TYPE_NAME} {_pk(target, int_pk)}")
        else:
            miss = n + rng.randint(1, 999)
            print(f"search record {TYPE_NAME} {_pk(miss, int_pk)}")


def mode_range(rng: random.Random, n: int, q: int,
               int_pk: bool = False) -> None:
    """In int_pk mode the range goes over PK so B+ tree fast path applies."""
    emit_create_type(int_pk)
    for i in range(n):
        emit_insert(rng, i, int_pk)
    for _ in range(q):
        if int_pk:
            lo = rng.randint(0, max(0, n - 50))
            hi = lo + rng.randint(5, 50)
            print(f"range_search {TYPE_NAME} pk {lo} {hi}")
        else:
            lo = rng.randint(0, 9000)
            hi = lo + rng.randint(100, 999)
            print(f"range_search {TYPE_NAME} score {lo} {hi}")


def mode_mixed(rng: random.Random, n: int, q: int,
               int_pk: bool = False) -> None:
    emit_create_type(int_pk)
    next_id = n
    live = list(range(n))
    for i in range(n):
        emit_insert(rng, i, int_pk)
    for _ in range(q):
        r = rng.random()
        if r < 0.5 and live:
            target = rng.choice(live)
            print(f"search record {TYPE_NAME} {_pk(target, int_pk)}")
        elif r < 0.75:
            emit_insert(rng, next_id, int_pk)
            live.append(next_id)
            next_id += 1
        else:
            if live:
                victim_idx = rng.randrange(len(live))
                victim = live.pop(victim_idx)
                print(f"delete record {TYPE_NAME} {_pk(victim, int_pk)}")


MODES = {
    "sequential": mode_sequential,
    "random": mode_random,
    "range": mode_range,
    "mixed": mode_mixed,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", required=True, choices=sorted(MODES.keys()))
    ap.add_argument("--records", type=int, required=True,
                    help="number of records N to load")
    ap.add_argument("--queries", type=int, required=True,
                    help="number of queries Q after the load")
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed (default 42 for reproducibility)")
    ap.add_argument("--int-pk", action="store_true",
                    help="Use an integer primary key. This is required for "
                         "the B+ tree fast-path on range queries -- the range "
                         "field must be the PK.")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    MODES[args.mode](rng, args.records, args.queries, int_pk=args.int_pk)


if __name__ == "__main__":
    main()
