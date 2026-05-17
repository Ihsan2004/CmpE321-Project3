"""Layer 4 - Query Processor.

The top layer. Parses input commands, dispatches to FileIndexManager,
collects per-command statistics, writes results to output.txt, and logs
every operation to log.csv.

Output files (all next to archive.py, spec section 13):
  - output.txt        : query results and explain output (one per run)
  - log.csv           : persistent, append-only operation log
  - stats_output.txt  : statistics snapshot (overwritten on every `stats`)

Behaviour
---------
- Never crashes on bad input. Bad commands are logged as 'failure' and
  produce no output line.
- Every operation that the FIM accepts is one log line. `stats` and
  `stats reset` are also logged.
- `explain` prints a plan, runs the wrapped command, prints results, then
  prints actual I/O / buffer / pages-scanned stats for *that command only*.
"""

import os
import time
from typing import List, Optional

from results import OpResult, RecordResult, SUCCESS, FAILURE


# command names we recognise (first 1 or 2 tokens)
_TWO_WORD_VERBS = {
    ("create", "type"),
    ("create", "record"),
    ("delete", "record"),
    ("search", "record"),
}
_ONE_WORD_VERBS = {"range_search", "explain", "stats"}


class QueryProcessor:
    """Layer 4."""

    def __init__(self, config: dict, file_idx, buffer, disk):
        self.config = config
        self.file_idx = file_idx
        self.buffer = buffer
        self.disk = disk

        # Resolve file paths once. Everything lives next to archive.py.
        root = os.path.dirname(os.path.abspath(__file__))
        self._root = os.path.dirname(root)
        self.output_path = os.path.join(self._root, "output.txt")
        self.log_path = os.path.join(self._root, "log.csv")
        self.stats_output_path = os.path.join(self._root, "stats_output.txt")

        # spec sample shows a clean file per run; truncate once at startup
        # so an empty-output run still leaves a fresh output.txt.
        open(self.output_path, "w", encoding="utf-8").close()

        # Cumulative counters that only the QP keeps (data-record level).
        # Disk / Buffer / Index counters live on their respective layers.
        self.records_scanned = 0
        self.records_returned = 0

    # ==================================================================
    # Public entry point (called from archive.py)
    # ==================================================================
    def process(self, line: str) -> None:
        """Parse and execute one input line. Never raises -- bad lines are
        logged as 'failure' and silently consumed."""
        try:
            self._dispatch(line)
        except Exception as e:
            # Last-ditch safety net. Anything that escapes the dispatcher
            # is a bug, but we still must not crash the engine.
            self._log(line, "failure")
            # Optional debug trace -- keep quiet on stdout to match the spec.
            _ = e

    # ==================================================================
    # Dispatcher
    # ==================================================================
    def _dispatch(self, line: str) -> None:
        tokens = line.split()
        if not tokens:
            return

        # ----- 'explain ...' wraps a DML command --------------------
        if tokens[0] == "explain":
            self._handle_explain(line, tokens[1:])
            return

        # ----- 'stats' / 'stats reset' ------------------------------
        if tokens[0] == "stats":
            if len(tokens) == 1:
                self._handle_stats(line)
            elif len(tokens) == 2 and tokens[1] == "reset":
                self._handle_stats_reset(line)
            else:
                self._log(line, "failure")
            return

        # ----- two-word verbs ----------------------------------------
        if len(tokens) >= 2 and (tokens[0], tokens[1]) in _TWO_WORD_VERBS:
            verb = (tokens[0], tokens[1])
            rest = tokens[2:]
            if verb == ("create", "type"):
                self._handle_create_type(line, rest)
            elif verb == ("create", "record"):
                self._handle_create_record(line, rest)
            elif verb == ("delete", "record"):
                self._handle_delete_record(line, rest)
            elif verb == ("search", "record"):
                self._handle_search_record(line, rest)
            return

        # ----- one-word verbs ---------------------------------------
        if tokens[0] == "range_search":
            self._handle_range_search(line, tokens[1:])
            return

        # ----- unrecognised ------------------------------------------
        self._log(line, "failure")

    # ==================================================================
    # Handlers
    # ==================================================================
    def _handle_create_type(self, line: str, rest: List[str]) -> None:
        r: OpResult = self.file_idx.create_type(rest)
        self._log(line, "success" if r.success else "failure")

    def _handle_create_record(self, line: str, rest: List[str]) -> None:
        if not rest:
            self._log(line, "failure")
            return
        type_name, values = rest[0], rest[1:]
        r: OpResult = self.file_idx.insert_record(type_name, values)
        self._log(line, "success" if r.success else "failure")

    def _handle_delete_record(self, line: str, rest: List[str]) -> None:
        if len(rest) < 2:
            self._log(line, "failure")
            return
        type_name, pk = rest[0], rest[1]
        r: OpResult = self.file_idx.delete_record(type_name, pk)
        self._log(line, "success" if r.success else "failure")

    def _handle_search_record(self, line: str, rest: List[str]) -> None:
        # spec: 'search record <type> <pk> <v1> ...'.
        # The extra values after the PK are not strictly needed for lookup;
        # we ignore them, matching the spec sample.
        if len(rest) < 2:
            self._log(line, "failure")
            return
        type_name, pk = rest[0], rest[1]
        r: RecordResult = self.file_idx.search_by_pk(type_name, pk)
        self.records_scanned += r.records_scanned
        if r.status == SUCCESS and r.records:
            self.records_returned += len(r.records)
            self._write_records(r.records)
            self._log(line, "success")
        else:
            self._log(line, "failure")

    def _handle_range_search(self, line: str, rest: List[str]) -> None:
        if len(rest) < 4:
            self._log(line, "failure")
            return
        type_name, field, lo, hi = rest[0], rest[1], rest[2], rest[3]
        r: RecordResult = self.file_idx.range_search(type_name, field, lo, hi)
        self.records_scanned += r.records_scanned
        if r.status == SUCCESS:
            # A valid range_search succeeds even when the result set is empty
            # (spec 7.4 lists only non-int field / missing type as failures).
            if r.records:
                self.records_returned += len(r.records)
                self._write_records(r.records)
            self._log(line, "success")
        else:
            self._log(line, "failure")

    def _handle_explain(self, line: str, inner_tokens: List[str]) -> None:
        """Print plan + result + actual stats for the wrapped command."""
        if not inner_tokens:
            self._log(line, "failure")
            return

        # Spec 7.3: explain wraps a DML command. DDL (create type) and system
        # commands (stats, stats reset) are not valid inner commands.
        first = inner_tokens[0]
        is_dml = (
            first == "range_search"
            or (first in ("search", "create", "delete")
                and len(inner_tokens) >= 2
                and inner_tokens[1] == "record")
        )
        if not is_dml:
            self._log(line, "failure")
            return

        # Reject malformed inner commands BEFORE writing the PLAN block, so
        # explain output stays consistent with the regular dispatcher.
        _min_inner_tokens = {
            "range_search": 5,   # range_search TYPE FIELD LO HI
            "search": 4,         # search record TYPE PK
            "create": 4,         # create record TYPE at-least-one-value
            "delete": 4,         # delete record TYPE PK
        }
        if len(inner_tokens) < _min_inner_tokens[first]:
            self._log(line, "failure")
            return

        inner_line = " ".join(inner_tokens)

        # Snapshot the engine state BEFORE running the inner command.
        snap = self._snapshot()

        # Pick a strategy + I/O estimate for the plan block.
        strategy, estimate = self._plan_for(inner_tokens)

        # Write the plan block (always, even if execution fails).
        # Width 16 matches the spec's sample formatting (section 9.2):
        # "Estimated I/O:" is 14 chars, so 16 gives 2-space gap before value.
        self._write_lines([
            "--- PLAN ---",
            f"{'Query:':<16}{inner_line}",
            f"{'Strategy:':<16}{strategy}",
            f"{'Estimated I/O:':<16}{estimate}",
        ])

        # --- Run the inner command. We collect its result lines into a
        # buffer (rather than going through _write_records) so we can place
        # them under '--- RESULT ---' even when the wrapped command would
        # have produced nothing on its own.
        result_lines: List[str] = []
        success_flag = False
        rec_pages = 0

        first = inner_tokens[0]
        if first == "search" and len(inner_tokens) >= 2 and \
                inner_tokens[1] == "record":
            rest = inner_tokens[2:]
            if len(rest) >= 2:
                type_name, pk = rest[0], rest[1]
                r = self.file_idx.search_by_pk(type_name, pk)
                rec_pages = r.pages_accessed
                self.records_scanned += r.records_scanned
                if r.status == SUCCESS and r.records:
                    result_lines = [self._format_record(rec) for rec in r.records]
                    success_flag = True
                    self.records_returned += len(r.records)
        elif first == "range_search":
            rest = inner_tokens[1:]
            if len(rest) >= 4:
                r = self.file_idx.range_search(rest[0], rest[1], rest[2], rest[3])
                rec_pages = r.pages_accessed
                self.records_scanned += r.records_scanned
                if r.status == SUCCESS:
                    success_flag = True
                    if r.records:
                        result_lines = [self._format_record(rec) for rec in r.records]
                        self.records_returned += len(r.records)
        elif first == "create" and len(inner_tokens) >= 2 and \
                inner_tokens[1] == "record":
            rest = inner_tokens[2:]
            if rest:
                r = self.file_idx.insert_record(rest[0], rest[1:])
                success_flag = bool(r.success)
        elif first == "delete" and len(inner_tokens) >= 2 and \
                inner_tokens[1] == "record":
            rest = inner_tokens[2:]
            if len(rest) >= 2:
                r = self.file_idx.delete_record(rest[0], rest[1])
                success_flag = bool(r.success)
        # If first is none of the above, success_flag stays False.

        # --- Write the RESULT block.
        self._write_lines(["--- RESULT ---"])
        if result_lines:
            self._write_lines(result_lines)

        # --- Write the STATS block, computed as (after - before).
        delta = self._delta(snap)
        self._write_lines([
            "--- STATS ---",
            f"{'Actual I/O:':<16}{delta['reads']} reads, {delta['writes']} writes",
            f"{'Buffer Hits:':<16}{delta['hits']}",
            f"{'Buffer Misses:':<16}{delta['misses']}",
            f"{'Pages Scanned:':<16}{rec_pages}",
        ])

        # Log the OUTER `explain` line; spec sample treats explain itself
        # as a successful operation if its inner command succeeded.
        self._log(line, "success" if success_flag else "failure")

    def _handle_stats(self, line: str) -> None:
        """Overwrite stats_output.txt with a current snapshot (spec 9.3)."""
        hit_rate = self.buffer.hit_rate() * 100.0 if hasattr(self.buffer, "hit_rate") \
            else (self.buffer.hits / self.buffer.requests * 100.0 if self.buffer.requests else 0.0)
        nodes = getattr(self.file_idx, "index_nodes_visited", 0)
        # Width 15 matches the spec's sample formatting (section 9.3):
        # "Buffer Pool:" is 12 chars, so 15 gives 3-space gap before value.
        text = (
            "=== STATISTICS ===\n"
            f"{'Disk I/O:':<15}{self.disk.read_count} reads, "
            f"{self.disk.write_count} writes\n"
            f"{'Buffer Pool:':<15}{self.buffer.requests} requests, "
            f"{self.buffer.hits} hits, {self.buffer.misses} misses "
            f"({hit_rate:.1f}% hit rate)\n"
            f"{'Evictions:':<15}{self.buffer.evictions} "
            f"({self.buffer.dirty_writebacks} dirty writebacks)\n"
            f"{'Index:':<15}{self.file_idx.index_strategy}, "
            f"{nodes} nodes visited\n"
            f"{'Records:':<15}{self.records_scanned} scanned, "
            f"{self.records_returned} returned\n"
        )
        with open(self.stats_output_path, "w", encoding="utf-8") as f:
            f.write(text)
        self._log(line, "success")

    def _handle_stats_reset(self, line: str) -> None:
        """Zero every counter exposed by every layer (spec 7.3)."""
        self.disk.stats_reset()
        # The buffer manager and FIM have their own resets.
        if hasattr(self.buffer, "stats_reset"):
            self.buffer.stats_reset()
        if hasattr(self.file_idx, "stats_reset"):
            self.file_idx.stats_reset()
        self.records_scanned = 0
        self.records_returned = 0
        self._log(line, "success")

    # ==================================================================
    # Plan / estimate
    # ==================================================================
    def _plan_for(self, inner_tokens: List[str]):
        """Pick a strategy label + estimated I/O for an explain block."""
        strategy = self.config["index_strategy"]

        # Identify the query shape.
        if inner_tokens[:1] == ["search"] and len(inner_tokens) >= 3 \
                and inner_tokens[1] == "record":
            type_name = inner_tokens[2]
            est = self._estimate_lookup(strategy, type_name)
        elif inner_tokens[:1] == ["range_search"] and len(inner_tokens) >= 5:
            type_name = inner_tokens[1]
            # hash_index falls back to heap_scan on range queries (spec 7.2).
            if strategy == "hash_index":
                strategy = "heap_scan (fallback)"
            est = self._estimate_range(strategy, type_name)
        else:
            # Inserts/deletes: estimate is approximately one page + one write.
            est = 1
        return strategy, est

    def _estimate_lookup(self, strategy: str, type_name: str) -> int:
        # Try to read the page count for a meaningful estimate.
        try:
            n_pages = self.disk.num_pages(type_name)
        except Exception:
            n_pages = 1
        if strategy == "heap_scan":
            return max(1, n_pages)
        if strategy == "hash_index":
            return 2     # 1 hash bucket page + 1 data page
        # bplus_tree: tree height + 1 leaf + 1 data; small trees -> ~3
        return 3

    def _estimate_range(self, strategy: str, type_name: str) -> int:
        try:
            n_pages = self.disk.num_pages(type_name)
        except Exception:
            n_pages = 1
        if strategy.startswith("heap_scan"):
            return max(1, n_pages)
        # bplus_tree range: tree walk + leaf chain + matched data pages
        return max(2, n_pages // 2 + 2)

    # ==================================================================
    # Snapshot / delta for explain
    # ==================================================================
    def _snapshot(self) -> dict:
        return {
            "reads": self.disk.read_count,
            "writes": self.disk.write_count,
            "hits": self.buffer.hits,
            "misses": self.buffer.misses,
            "nodes": getattr(self.file_idx, "index_nodes_visited", 0),
        }

    def _delta(self, snap: dict) -> dict:
        return {
            "reads": self.disk.read_count - snap["reads"],
            "writes": self.disk.write_count - snap["writes"],
            "hits": self.buffer.hits - snap["hits"],
            "misses": self.buffer.misses - snap["misses"],
            "nodes": getattr(self.file_idx, "index_nodes_visited", 0)
                     - snap["nodes"],
        }

    # ==================================================================
    # Output / log file helpers
    # ==================================================================
    def _write_lines(self, lines: List[str]) -> None:
        with open(self.output_path, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    def _write_records(self, records: List[List]) -> None:
        self._write_lines([self._format_record(r) for r in records])

    @staticmethod
    def _format_record(rec: List) -> str:
        # Each record on one line, fields space-separated -- matches spec
        # sample output exactly.
        return " ".join(str(v) for v in rec)

    def _log(self, command: str, status: str) -> None:
        # Persistent, append-only -- previous runs' lines survive.
        ts = int(time.time())
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{ts},{command},{status}\n")
