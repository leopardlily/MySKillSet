---
name: latency-cache-optimizer
description: Review hot-path code for cache, memory-level parallelism, allocation, false-sharing, and data-layout optimization opportunities, and decide whether each candidate is worth benchmarking. Use when asked to inspect code for low-latency optimization, cache misses, prefetching, batching, object layout, FullBook / order book hot paths, JVM allocation/GC, MESI ownership, or "is this optimization worth testing".
---

# Latency Cache Optimizer

Use this skill to review code the way a low-latency systems engineer would:

> Do not change the math first. Find where the CPU is waiting on memory, allocation, serialization, or cache-line ownership, then decide whether the candidate is hot enough and measurable enough to justify an experiment.

This skill is a **review and decision workflow**, not an auto-refactor recipe. Its output must separate:

1. **Observed facts** from the code;
2. **Hypotheses** about the bottleneck;
3. **Evidence** needed to accept/reject the hypothesis;
4. **Verdict**: `TEST NOW`, `BACKLOG`, or `REJECT`;
5. **Experiment plan** if testing is justified.

## 1. First principles

For one event on a latency-critical path:

```text
latency ≈ compute
        + memory_latency / memory_level_parallelism
        + serialization
        + allocation/GC
        + cache_coherence/ownership
        + scheduling/jitter
```

A code change is interesting only if it reduces one of these on the critical path, or increases overlap without increasing a worse term.

Key rules:

- **Working set matters.** If the hot data exceeds the relevant cache level, misses are unavoidable; optimize miss cost and overlap, not only instruction count.
- **MLP matters.** Independent memory requests can overlap; dependent loads cannot.
- **OoO is not unlimited.** ROB, load/store queues, LFB/MSHR slots, registers, and memory dependencies bound overlap.
- **LFB is finite.** On many Intel cores only ~16 line-fill buffers are available. Do not batch arbitrarily large numbers of misses.
- **Prefetch must be early enough, but not so early that data is evicted before use.**
- **`_mm_prefetch` is a data-prefetch instruction.** It does not prefetch machine instructions or L1I.
- **Data reuse determines cache hint.** Use `T0` for data reused in the next event(s); `NTA` is for genuinely streaming one-shot data and can hurt reused working sets.
- **Cross-core reads of dirty lines are expensive.** MESI ownership transfer can dominate a "simple read".
- **Measure with counters and end-to-end percentiles.** A microbenchmark alone is not enough.

## 2. Apply this skill when

Use it when one or more of these is true:

- The code is on a documented hot path (per market-data event, per order, per quote, per frame).
- The user asks whether an optimization is worth testing.
- There are per-event allocations, list materialization, object graphs, copies, or BigDecimal conversions.
- There is batching, prefetching, cache hints, thread handoff, or false-sharing risk.
- There is a large working set: full order books, option chains, factor history, feature tables.
- A proposed change (fixed point, SoA, prefetch, multi-threading, caching) needs a go/no-go decision.

Do **not** use it to justify broad rewrites without a hot-path measurement.

## 3. Review workflow

### Step 0 — Define the unit of work

Record:

- What is the event? `WS frame`, `BBO update`, `public trade`, `timer tick`, `order ack`, `strategy term`.
- Event frequency: per second, per instrument, per account.
- Number of instruments/accounts processed per event.
- Latency sensitivity: p50 / p90 / p99 budget.
- Is the code executed inline on an I/O thread, a dedicated Disruptor thread, a coroutine dispatcher, a worker pool, or a timer?
- Is backpressure possible? What happens when the consumer is slow?

Output a one-line workload statement:

```text
Hot path: 64 option reprices per underlying tick, ~200k ticks/s, p99 budget < 50µs.
```

### Step 1 — Trace the critical path

Follow the actual call chain, not just filenames:

```text
event source
  → decode / validation
  → data snapshot
  → calculation
  → decision
  → order operation
```

For each step record:

- bytes touched;
- objects allocated;
- collections copied;
- locks / atomics / queues;
- cross-thread handoff;
- blocking calls;
- branchy paths.

Stop when the path reaches the order operation or leaves the latency-critical section.

### Step 2 — Classify the likely bottleneck

Label each candidate path with one or more:

| Label | Signals |
|---|---|
| `SERIAL_DEP` | load → compute → load → compute; pointer chasing; dependent chains |
| `LOW_MLP` | independent work exists but is issued one-at-a-time |
| `CACHE_FOOTPRINT` | large working set; many objects; poor locality |
| `MATERIALIZE` | rebuilds lists/snapshots every event; copies full depth |
| `ALLOC_GC` | per-event boxed values, temporary lists, strings, BigDecimal allocations |
| `FALSE_SHARING` | multiple threads write adjacent fields/objects |
| `OWNERSHIP` | producer writes, consumer reads the same cache lines; dirty-line transfer |
| `CACHE_POLLUTION` | one-shot/dead data retained in cache, evicting reusable state |
| `BRANCH_CODE` | huge code footprint, branch misses, megamorphic dispatch |
| `SCHED_JITTER` | wakeups, context switches, unpinned work, safepoints |
| `LOCK_CONTENTION` | blocking locks or retry loops on hot data |

### Step 3 — Estimate cache-line footprint

Use rough calculations to expose obviously bad layouts:

```text
cache_line = 64 bytes (typical x86)
lines(object) = ceil(object_size / 64)
lines(event) = event_lines + Σ(objects_touched × lines_per_object)
working_set = lines(event) × events_in_flight + persistent_tables
```

Also include:

- array/list backing storage;
- object headers and references (JVM: 12–16-byte headers, compressed oops may apply);
- boxed primitives;
- hash-map nodes;
- duplicate snapshot copies;
- per-thread queues/buffers.

Do not pretend the estimate is exact. Its purpose is to reveal order-of-magnitude problems.

### Step 4 — Generate candidates

Use the pattern catalog in `references/optimization-patterns.md`. Typical candidates:

1. **Batch independent work** to increase MLP, but bound the batch.
2. **Software prefetch** data for reused working sets, with enough lead time.
3. **Pipeline** load / compute / write stages to overlap latency.
4. **SoA or primitive arrays** instead of object-per-level/object-per-tick.
5. **Lazy materialization** instead of rebuilding full snapshots per event.
6. **Reduce copies** and duplicate immutable wrappers.
7. **Streaming / non-temporal hints** only for truly one-shot data.
8. **Thread ownership / shard by key** to avoid cross-core dirty-line transfer.
9. **False-sharing padding** for producer/consumer metadata.
10. **Reduce code/branch footprint** on the hot path.
11. **Cache lookup tables / precompute constants** if reused and small enough.
12. **Fixed-point or integer tick representation** only for exact decimal/tick boundaries, not as a reflex for all math.

For each candidate record the exact file, function, line range, and the mechanism by which it could help.

### Step 5 — Try to falsify the candidate

Before recommending a test, ask:

- Is this code actually on the hot path?
- Is the data reused? If not, prefetch/caching may hurt.
- Is the working set too large for the proposed cache level?
- Is the bottleneck compute, branch, lock, allocation, or memory? Would this change the actual bottleneck?
- Does batching exceed LFB/cache capacity?
- Does prefetch arrive too early (evicted) or too late (no overlap)?
- Does lazy materialization merely move work into a more frequent path?
- Will JVM GC or JIT eliminate/reshape the supposed cost?
- Does multi-threading introduce ownership transfer that negates the gain?
- Does the change increase p99 while improving microbenchmark p50?

If the candidate cannot plausibly move the critical-path metric, mark `REJECT` or `BACKLOG`.

### Step 6 — Decide whether testing is worth it

Compute a rough upper bound:

```text
saved_ns_per_event × events_per_second × affected_instances
```

Then compare with the end-to-end budget:

```text
fraction_of_total_latency = saved_ns_per_event / current_p50_or_p99
```

Decision guide:

| Condition | Verdict |
|---|---|
| Hot path + plausible memory bottleneck + measurable + meaningful upper bound | `TEST NOW` |
| Hot path but evidence weak / gain likely <5% / change risky | `BACKLOG` |
| Cold path, no reuse, or bottleneck elsewhere | `REJECT` |
| Uncertain but cheap to measure with existing counters | `TEST NOW` (measurement only) |
| Requires broad redesign and no baseline | `BACKLOG` after instrumentation |

### Step 7 — Design the experiment

Every `TEST NOW` must include:

- **Baseline**: current p50/p90/p99, throughput, CPU, cache/TLB/GC counters.
- **Variant**: minimal isolated change, ideally behind a flag.
- **Workload**: realistic instruments, event rate, data volume, distribution.
- **Warmup**: JIT/CPU frequency/cache warmup as applicable.
- **Metrics**: end-to-end latency plus hardware counters, not just microbenchmark time.
- **Success threshold**: e.g. "p99 -10% with no p50 regression and no CPU increase >3%".
- **Abort threshold**: e.g. "revert if p99 +2% or CPU +5%".
- **Correctness**: bit-identical behavior or golden-value equivalence.

For low-level measurement checklists, see `references/measurement-checklist.md`.

## 4. Language-specific constraints

### C / C++

Available:

- `__builtin_prefetch(addr, rw, locality)`;
- `_mm_prefetch(addr, _MM_HINT_T0/T1/T2/NTA)`;
- `_mm_clflush`, `_mm_clflushopt`, `_mm_clwb` (target-dependent);
- explicit alignment, SoA, cache-line padding, compiler intrinsics.

Rules:

- Prefetch **data**, not instructions.
- Use `T0` for reused data; `NTA` only for streaming/one-shot data.
- Do not issue an unbounded number of misses; respect LFB capacity.
- `clflushopt` and `clwb` also consume memory-pipeline resources; measure.
- Prevent dead-code elimination in benchmarks; consume results.

### JVM / Kotlin / Java

Direct equivalents are generally unavailable:

- no standard `_mm_prefetch`, `clflushopt`, or `clwb`;
- GC may move objects;
- hardware prefetchers and JIT optimize away naive experiments;
- object headers/references and boxed values dominate footprint.

Prefer:

- primitive arrays and Struct-of-Arrays;
- value classes / avoiding boxing;
- fewer temporary lists and snapshot copies;
- lazy materialization;
- batching and object reuse;
- thread-local ownership / sharding;
- measuring with JFR, async-profiler, and `perf` counters where available.

Do not claim a JVM optimization will reproduce a C++ prefetch result without measurement.

### Rust / Go / other native

Use the same principles. Check whether prefetch/non-temporal intrinsics are stable and portable before recommending them.

## 5. Mandatory guardrails

- Never recommend `_mm_prefetch` for code that is not on a measured hot path.
- Never recommend `NTA` for data reused in subsequent events.
- Never recommend batch size 64 just because the loop has 64 items; bound by LFB/cache and measure.
- Never treat a microbenchmark improvement as production improvement without end-to-end validation.
- Never optimize a cold path because it is aesthetically ugly.
- Never replace all `Double`/`BigDecimal` with fixed point without profiling the specific path.
- Always distinguish **per-frame**, **per-event**, **timer**, and **background** paths.
- Always include p50 and p99; a p50 win with a p99 regression is usually not acceptable.
- Always state what would falsify the hypothesis.

## 6. Required output format

Use `templates/review-report.md` as the structure. At minimum:

```markdown
# Latency / Cache Optimization Review

## Scope
- Repo:
- Hot path:
- Workload assumptions:
- Current evidence:

## Findings
| # | Location | Pattern | Hypothesis | Upper-bound gain | Confidence | Verdict |
|---|---|---|---|---|---|---|

## Detail per candidate
### Candidate N
- Location:
- Current behavior:
- Suspected bottleneck:
- Proposed experiment:
- Why it might help:
- Why it might not help:
- Measurement plan:
- Correctness checks:
- Estimated effort/risk:
- Verdict: TEST NOW / BACKLOG / REJECT

## Recommended order
1. ...
2. ...

## Baselines to collect before changing code
- ...
```

## 7. Quality bar

A good review:

- starts from measured or strongly inferred hot-path facts;
- separates cache, MLP, allocation, coherence, and compute costs;
- gives a concrete mechanism for each proposed win;
- rejects most speculative optimizations;
- gives a cheap first experiment before a rewrite;
- names the exact counters and acceptance thresholds;
- is honest about uncertainty.

A bad review:

- says "use prefetch" without a hot path or reuse pattern;
- says "use SoA" without estimating object footprint;
- says "multi-thread it" without analyzing ownership transfer;
- says "fixed point will be faster" without profiling BigDecimal/Double usage;
- benchmarks with unrealistically small data;
- optimizes p50 while ignoring p99;
- recommends a large rewrite before a baseline exists.

## 8. Reference files

- `references/optimization-patterns.md` — candidate patterns and anti-patterns.
- `references/measurement-checklist.md` — counters, tools, benchmark design.
- `references/case-study-latency-slippage.md` — worked 64-option case study, four optimization rounds, failure cases, and mapping back to this workflow.
- `templates/review-report.md` — final report template.
- `scripts/scan_candidates.py` — rough **C++-only** static scan for common hot-path patterns; inventory only, never proof.

```bash
python3 scripts/scan_candidates.py /path/to/cpp/repo --top 50 --out /tmp/cpp-candidates.md
```

Use `--include-tests` only when test/benchmark code is part of the review scope.

## 9. Theoretical basis

This skill is based on the cache/latency reasoning popularized by Sanchit Gupta (Graviton Research Capital) in the CppCon talk “The Latency Slippage”:

- batch independent loads to expose MLP;
- respect LFB limits;
- software-prefetch reused data;
- avoid polluting caches with dead/one-shot data;
- account for MESI dirty-line ownership transfer;
- decouple cross-core results and use write-back where appropriate;
- do not change the math; change how the CPU waits for memory.

The concrete C++ intrinsics are platform-specific. The review method is not.
