# Latency / Cache Optimization Review

## Scope

- Repository:
- Revision / branch:
- Unit of work:
- Event frequency:
- Instruments / accounts per event:
- Latency budget:
- Thread / dispatcher context:
- Backpressure behavior:

## Evidence collected

| Item | Value | Source |
|---|---|---|
| p50 | | |
| p99 | | |
| Throughput | | |
| CPU | | |
| Allocation/GC | | |
| Cache misses | | |
| LFB stalls | | |
| Context switches | | |
| Working set estimate | | |

## Findings

| # | Location | Pattern | Hypothesis | Upper-bound gain | Confidence | Verdict |
|---|---|---|---|---|---|---|
| 1 | | | | | | TEST NOW / BACKLOG / REJECT |
| 2 | | | | | | |

## Candidate detail

### Candidate 1 — <name>

- **Location**:
- **Current behavior**:
- **Suspected bottleneck**: `SERIAL_DEP` / `LOW_MLP` / `CACHE_FOOTPRINT` / `MATERIALIZE` / `ALLOC_GC` / `FALSE_SHARING` / `OWNERSHIP` / `CACHE_POLLUTION` / `BRANCH_CODE`
- **Mechanism**: why the proposed change could move the metric
- **Evidence from code**:
- **Estimated per-event cost / upper bound**:
- **Why it might not help**:
- **Experiment**:
  - change:
  - workload:
  - metrics:
  - success threshold:
  - abort threshold:
- **Correctness checks**:
- **Effort / risk**:
- **Verdict**:

### Candidate 2 — <name>

- **Location**:
- **Current behavior**:
- **Suspected bottleneck**:
- **Mechanism**:
- **Evidence from code**:
- **Estimated per-event cost / upper bound**:
- **Why it might not help**:
- **Experiment**:
- **Correctness checks**:
- **Effort / risk**:
- **Verdict**:

## Recommended order

1. Measure baseline.
2. Try the smallest isolated change.
3. Only proceed to larger redesigns if counters show the bottleneck is still present.

## Rejected candidates

| Candidate | Why rejected |
|---|---|
| | Cold path / no reuse / bottleneck elsewhere / unmeasurable |

## Open questions

- Is the workload bursty or steady-state?
- What is the actual LFB/cache capacity of the production CPU?
- Is the JVM moving objects (GC) or is the C++ allocator reusing memory?
- Does the consumer actually need full depth, or only BBO / top-N?
- Are there existing counters or traces for this path?

## Final recommendation

- **Short term**:
- **Medium term**:
- **Do not do yet**:
