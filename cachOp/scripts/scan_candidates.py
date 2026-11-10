#!/usr/bin/env python3
"""Rough C++ hot-path/cache candidate inventory.

C++ only. This is an inventory tool, not a profiler, and not proof of a
bottleneck. Every hit needs human review and a measurement plan.

Usage:
  python3 scan_candidates.py /path/to/cpp/repo
  python3 scan_candidates.py /path/to/cpp/repo --top 40
  python3 scan_candidates.py /path/to/cpp/repo --out /tmp/candidates.md
  python3 scan_candidates.py /path/to/cpp/repo --include-tests
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


CXX_EXTS = {
    ".cpp", ".cc", ".cxx", ".c++",
    ".c",
    ".h", ".hh", ".hpp", ".hxx", ".h++",
    ".ipp", ".inl",
}

SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "build", "build-*", "cmake-build-*", "out", "dist", "target",
    "third_party", "third-party", "3rdparty", "external", "extern", "vendor",
    "node_modules", "__pycache__", ".cache", ".idea", ".vscode",
    "venv", ".venv", ".gradle",
}

TEST_DIRS = {"test", "tests", "unittest", "unittests", "benchmark", "benchmarks", "bench"}

# Deliberately broad. The scanner finds review leads, not confirmed hotspots.
PATTERNS: dict[str, re.Pattern[str]] = {
    "vector_churn": re.compile(
        r"\b(?:std\s*::\s*)?vector\s*<|"
        r"\.\s*(?:reserve|resize|push_back|emplace_back|insert|erase|clear|"
        r"shrink_to_fit|assign)\s*\(|"
        r"\b(?:reserve|resize|push_back|emplace_back)\s*\("
    ),
    "copy_mem": re.compile(
        r"\b(?:memcpy|memmove|memset|std\s*::\s*copy|std\s*::\s*copy_n|"
        r"std\s*::\s*transform|std\s*::\s*fill|std\s*::\s*assign)\s*\(|"
        r"\.\s*assign\s*\("
    ),
    "allocation": re.compile(
        r"\b(?:new|delete|malloc|calloc|realloc|free|alloca)\b|"
        r"\b(?:make_unique|make_shared)\s*<|"
        r"\b(?:std\s*::\s*)?(?:string|vector|map|unordered_map|set|"
        r"unordered_set|list|deque|shared_ptr|unique_ptr)\s*<"
    ),
    "sorting": re.compile(
        r"\bstd\s*::\s*(?:sort|stable_sort|partial_sort|nth_element)\s*\("
    ),
    "cache_hint": re.compile(
        r"\b(?:_mm_prefetch|__builtin_prefetch|_mm_clflushopt|_mm_clflush|"
        r"_mm_clwb|prefetchnta|prefetcht0|prefetcht1|prefetcht2|"
        r"hardware_destructive_interference_size)\b|"
        r"\b_MM_HINT_(?:T0|T1|T2|NTA)\b|"
        r"\balignas\s*\(|\bCACHELINE\b"
    ),
    "sync": re.compile(
        r"\b(?:std\s*::\s*)?(?:atomic|mutex|shared_mutex|lock_guard|"
        r"unique_lock|scoped_lock|condition_variable|spinlock|"
        r"compare_exchange_weak|compare_exchange_strong|memory_order_\w+)\b"
    ),
    "ownership": re.compile(
        r"\b(?:shared_ptr|unique_ptr|weak_ptr|atomic_ref)\s*<|"
        r"\b(?:fetch_add|fetch_sub|load|store)\s*\(\s*memory_order_\w+\s*\)"
    ),
    "timer_io": re.compile(
        r"\b(?:steady_clock|high_resolution_clock|sleep_for|sleep_until|"
        r"nanosleep|epoll_wait|io_uring|poll|select)\s*\("
    ),
    "loop": re.compile(r"\b(?:for|while)\s*\("),
    "branch": re.compile(r"\b(?:switch|if|else if)\s*\("),
    "hot_name": re.compile(
        r"(?i)(hot|fast|latency|low_latency|low-latency|market_data|"
        r"marketdata|orderbook|order_book|fullbook|full_book|quote|pricing|"
        r"frame|disruptor|risk|greek|safeguard|strategy)"
    ),
}

# These are not proof of an issue; they help humans spot suspicious shapes.
COMPOUND_HINTS = {
    "likely_materialize_loop": re.compile(
        r"\b(?:for|while)\s*\([^)]*\)[^{;]*\{[^}]{0,2000}?"
        r"(?:\.push_back|\.emplace_back|\.insert|std\s*::\s*copy|memcpy)",
        re.S,
    ),
    "likely_object_chase": re.compile(
        r"\b(?:for|while)\s*\([^)]*\)[^{;]*\{[^}]{0,2000}?"
        r"(?:->|\.)\s*(?:next|data|value|price|size|qty|quantity|book|level)",
        re.S,
    ),
    "likely_cross_core_shared": re.compile(
        r"\b(?:atomic|mutex|shared_ptr)\b[^;]{0,300}"
        r"(?:memory_order_|compare_exchange|lock_guard|unique_lock)",
        re.S,
    ),
}


@dataclass
class FileHit:
    path: Path
    counts: dict[str, int] = field(default_factory=dict)
    compound: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values()) + sum(self.compound.values())

    @property
    def hot_name(self) -> int:
        return 1 if self.counts.get("hot_name", 0) else 0


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def is_skipped(path: Path, include_tests: bool) -> bool:
    for part in path.parts:
        if any(part == pat or (pat.endswith("*") and part.startswith(pat[:-1])) for pat in SKIP_DIRS):
            return True
        if not include_tests and part.lower() in TEST_DIRS:
            return True
    return False


def iter_files(root: Path, include_tests: bool):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in CXX_EXTS:
            continue
        if is_skipped(path, include_tests):
            continue
        yield path


def scan(root: Path, include_tests: bool) -> list[FileHit]:
    hits: list[FileHit] = []
    for path in iter_files(root, include_tests):
        text = read_text(path)
        if not text:
            continue

        counts: dict[str, int] = {}
        for name, pattern in PATTERNS.items():
            if name == "hot_name":
                count = 1 if pattern.search(str(path)) else 0
            else:
                count = len(pattern.findall(text))
            if count:
                counts[name] = count

        compound: dict[str, int] = {}
        for name, pattern in COMPOUND_HINTS.items():
            count = len(pattern.findall(text))
            if count:
                compound[name] = count

        if counts or compound:
            hits.append(FileHit(path=path, counts=counts, compound=compound))

    hits.sort(key=lambda h: (-h.hot_name, -h.total, str(h.path)))
    return hits


def render_markdown(root: Path, hits: list[FileHit], top: int) -> str:
    pattern_cols = list(PATTERNS.keys())
    compound_cols = list(COMPOUND_HINTS.keys())
    lines = [
        "# C++ Hot-Path / Cache Candidate Inventory",
        "",
        f"Root: `{root}`",
        "",
        "> C++ only. This is a static keyword inventory, not a profiler and not proof of a bottleneck.",
        "> Confirm hot-path frequency, inspect the surrounding code, then use perf/PMU counters.",
        "> Tests/benchmarks are excluded unless `--include-tests` is passed.",
        "",
        "| File | Total | " + " | ".join(pattern_cols) + " | " + " | ".join(compound_cols) + " |",
        "|---|---:|" + "|".join("---:" for _ in pattern_cols) + "|" + "|".join("---:" for _ in compound_cols) + "|",
    ]

    for hit in hits[:top]:
        try:
            rel = hit.path.relative_to(root)
        except ValueError:
            rel = hit.path
        cells = [str(hit.counts.get(name, 0)) for name in pattern_cols]
        cells += [str(hit.compound.get(name, 0)) for name in compound_cols]
        lines.append(f"| `{rel}` | {hit.total} | " + " | ".join(cells) + " |")

    if len(hits) > top:
        lines += ["", f"Showing top {top} of {len(hits)} matching files. Use --top to increase."]

    lines += [
        "",
        "## Column guide",
        "",
        "- `vector_churn`: vector/string mutation and capacity operations; possible reallocation/copy.",
        "- `copy_mem`: memcpy/std::copy/std::transform; possible duplicate data movement.",
        "- `allocation`: new/delete/malloc/containers/smart pointers; possible allocator pressure.",
        "- `sorting`: sort/partial sort; possible O(n log n) or cache-unfriendly reorder.",
        "- `cache_hint`: prefetch/flush/alignment intrinsics; inspect distance, hint, and LFB budget.",
        "- `sync`: atomics/locks/orderings; inspect contention and ownership transfer.",
        "- `ownership`: shared_ptr/atomic_ref/orderings; possible MESI ownership or refcount traffic.",
        "- `timer_io`: timers/event loops; separate hot event handlers from idle polling.",
        "- `loop` / `branch`: code-shape hints only.",
        "- `likely_materialize_loop`: loop body contains push_back/insert/copy; review for per-event snapshots.",
        "- `likely_object_chase`: loop body dereferences pointer-like fields; review locality.",
        "- `likely_cross_core_shared`: atomic/shared ownership near synchronization; review false sharing.",
        "",
        "## Suggested next step",
        "",
        "1. Keep only paths demonstrably on the hot path.",
        "2. Record p50/p99, CPU, allocation, and cache counters before editing.",
        "3. Read the function; a regex hit is only a lead.",
        "4. Use `templates/review-report.md` and return TEST NOW / BACKLOG / REJECT.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Rough C++ hot-path/cache candidate scanner")
    parser.add_argument("root", nargs="?", default=".", help="C++ repository or source root")
    parser.add_argument("--top", type=int, default=50, help="number of files to show")
    parser.add_argument("--out", help="write Markdown report to this path")
    parser.add_argument("--include-tests", action="store_true", help="include test/benchmark directories")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    hits = scan(root, args.include_tests)
    report = render_markdown(root, hits, max(1, args.top))
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        print(f"wrote {out} ({len(hits)} matching C++ files)")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
