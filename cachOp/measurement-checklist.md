# Measurement Checklist

The goal is to distinguish a real end-to-end win from a microbenchmark illusion.

## 1. Baseline before touching code

Collect and record:

- p50 / p90 / p99 / p99.9 latency of the actual operation.
- Throughput: events/s, orders/s, quotes/s, frames/s.
- CPU utilization per thread/core.
- Allocations and GC pauses (JVM).
- Context switches and CPU migrations.
- Cache miss / LLC miss / TLB miss counters if available.
- Queue depth / ring-buffer backlog.
- Number of instruments/accounts and hot working-set size.
- Hardware/VM type, CPU model, frequency policy, hyperthreading state, NUMA placement.

Use absolute numbers and the exact workload:

```text
64 options × 5µs event stream, 200k events/s, p99 budget 50µs
```

## 2. `perf stat` (Linux native / JVM process)

Basic counter set:

```bash
perf stat -d -d -d \
  -e cycles,instructions,branches,branch-misses,\
cache-references,cache-misses,\
L1-dcache-loads,L1-dcache-load-misses,\
LLC-loads,LLC-load-misses,\
context-switches,cpu-migrations,page-faults \
  -- <command>
```

Normalize:

- misses per event;
- cycles per event;
- instructions per cycle (IPC);
- branch misses per event;
- LLC misses per second and per event.

Pitfalls:

- PMU may be unavailable in containers/VMs.
- Multiplexing can make counts approximate.
- `perf stat` overhead matters for very short runs.
- Do not compare runs on different CPU/frequency states.

## 3. Intel-specific stall/cache counters

Candidate events (availability depends on CPU generation):

```text
l1d_pend_miss.fb_full
l1d_pend_miss.pending
l1d_pend_miss.pending_cycles
cycle_activity.stalls_l1d_miss
cycle_activity.stalls_mem_any
mem_load_retired.l1_miss
mem_load_retired.l2_miss
mem_load_retired.l3_miss
mem_inst_retired.all_loads
mem_inst_retired.all_stores
machine_clears.memory_ordering
```

Look for:

- `fb_full`: LFB saturation. Classic symptom of batching too much / too many outstanding misses.
- stalls on L1D misses: CPU is waiting for memory.
- high LLC misses: working set exceeds cache.
- `machine_clears.memory_ordering`: memory ordering / coherence pressure.

Use `perf list` to confirm event names on the actual host.

## 4. False sharing and ownership

```bash
perf c2c record -- <command>
perf c2c report
```

Investigate:

- HITM (cache line modified in another core);
- remote loads/stores;
- cache-line ping-pong;
- same cache line written by multiple threads.

For JVM processes, attach to PID:

```bash
perf c2c record -p <pid> -- sleep 10
perf c2c report
```

## 5. CPU profile

Native:

```bash
perf record -F 999 -g -- <command>
perf report
```

JVM (async-profiler):

```bash
asprof -e cpu -d 30 -f /tmp/cpu.html <pid>
asprof -e alloc -d 30 -f /tmp/alloc.html <pid>
asprof -e lock -d 30 -f /tmp/lock.html <pid>
asprof -e wall -d 30 -f /tmp/wall.html <pid>
```

Look for:

- time in `memcpy`, `memset`, allocation, GC, safepoints;
- hot allocation stacks;
- lock contention;
- blocked/parked threads;
- cache-miss-heavy functions.

JFR:

```bash
jcmd <pid> JFR.start name=latency settings=profile duration=60s filename=/tmp/run.jfr
jcmd <pid> JFR.stop name=latency
```

Inspect allocation, GC, monitor, thread park, file/socket I/O events.

## 6. JVM-specific baseline

Record:

- heap used / committed;
- allocation rate (MB/s);
- GC count and pause distribution;
- TLAB/PLAB behavior if available;
- safepoint pauses;
- JIT compilation/inlining changes after warmup;
- deoptimization events;
- thread count and CPU affinity.

Watch for:

- boxed primitives and temporary lists;
- `BigDecimal` allocation and formatting;
- `String` formatting in constructors;
- `HashMap`/`TreeMap` node allocation;
- object headers dominating small objects;
- GC moving objects and invalidating assumptions.

## 7. Microbenchmark rules

### JMH for JVM

- Use JMH, not a hand-rolled `System.nanoTime()` loop.
- Warm up enough iterations.
- Use blackhole/consume results.
- Use realistic object sizes and data distributions.
- Test cold and hot states separately.
- Measure allocation via JMH profilers or async-profiler.

### Native

- Prevent dead-code elimination by consuming results.
- Use realistic memory footprint; do not let data fit in L1 by accident.
- Pin CPU and avoid noisy neighbors where possible.
- Separate cache-warm and cache-cold phases.
- Measure both median and tail.

## 8. End-to-end test rules

A microbenchmark win is not enough. Also test:

- realistic batch sizes;
- concurrent connections/threads;
- market-data burst and idle-recovery patterns;
- order/trade arrival interleaving;
- GC/JIT under sustained load;
- p99.9 and max;
- CPU and memory overhead;
- failure/backpressure behavior;
- rollback path.

## 9. Experiment template

```markdown
### Hypothesis
The current per-order logic rebuilds X lists and touches Y cache lines per event.
A bounded two-pass load should overlap misses and reduce p99.

### Baseline
- p50: ...
- p99: ...
- events/s: ...
- cache misses/event: ...
- LFB full stalls/event: ...
- alloc/event: ...

### Variant
- File/function:
- Change:
- Batch size:
- Guardrails:

### Acceptance
- p99 reduced by >= 10%
- p50 not worse by > 2%
- CPU not worse by > 3%
- cache misses/event reduced
- no correctness deviation

### Abort
- p99 +2%
- CPU +5%
- any correctness mismatch
```

## 10. Common measurement mistakes

- Comparing different CPU frequencies or turbo states.
- Letting the test data fit in L1.
- Ignoring warmup/JIT.
- Measuring only average.
- Using too few events; tail needs enough samples.
- Optimizing a copy that the compiler removed.
- Trusting `perf` counters without checking multiplexing/VM availability.
- Testing prefetch on sequential data the hardware prefetcher already handles.
- Attributing a win to layout when the real change was fewer allocations.
- Not keeping the old code path available for A/B comparison.

## 11. Minimal first experiment

When time is limited, do this first:

1. Capture p50/p99 + CPU + allocation for the current workload.
2. Count allocations and list copies per event from a profile.
3. Try one isolated change behind a flag.
4. Re-run the same workload for at least 3 runs.
5. Compare distributions, not just means.
6. Revert if the result is not clearly outside noise.
