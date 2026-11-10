# Optimization Patterns

Use this catalog to generate candidates. Every candidate still needs a hot-path rationale and a measurement plan.

## 1. Two-pass / batched load

### Pattern

Split a loop into:

```text
pass 1: issue loads / touch cache lines for a bounded batch
pass 2: compute over the batch, mostly hitting cache
```

### Mechanism

- Creates independent memory requests; increases MLP.
- Hides memory latency behind other misses.
- Bounded batch avoids overwhelming LFB/MSHR and cache.

### Good conditions

- Per event, several independent objects/rows must be touched.
- The objects are reused by the subsequent compute.
- The current loop serializes load and compute.
- Batch fits in LFB and cache capacity.

### Bad conditions

- Data is already in cache.
- The first pass is optimized away (dead load).
- Batch is too large (LFB saturation, eviction before use).
- Data is one-shot and huge.
- Compute is already independent and the hardware prefetcher already overlaps it.

### Verification

- Cycles per event; IPC.
- L1/L2/L3 miss counts.
- `l1d_pend_miss.fb_full` or vendor equivalent.
- Offcore pending misses / MSHR pressure.
- p50 and p99 end-to-end.

## 2. Software data prefetch

### Pattern

```cpp
_mm_prefetch(reinterpret_cast<const char*>(&obj), _MM_HINT_T0);
```

### Mechanism

- Issues a data cache-line request without a destination register.
- Does not occupy ROB waiting for a result.
- Can be issued earlier than a regular load.

### Good conditions

- Address is predictable several instructions/iterations ahead.
- Data will be reused soon.
- Working set does not exceed cache lifetime.
- Prefetch distance is tuned.

### Bad conditions

- Data is used once; `NTA` or no prefetch may be better.
- Data is already in cache.
- Prefetch too early → evicted before use.
- Prefetch too late → no overlap.
- Too many prefetches → LFB pressure.
- JVM: no portable intrinsic; GC may move objects.

### Important

`_mm_prefetch` prefetches **data**, not instructions. Instruction fetch is a separate front-end concern.

## 3. Pipeline / staged processing

### Pattern

```text
load N+1 → compute N → store N-1
```

### Mechanism

- Keeps multiple independent operations in flight.
- Hides memory latency behind compute.
- Avoids requiring the entire batch in cache at once.

### Good conditions

- Work has distinct load / compute / store stages.
- Per-stage dependency is low.
- Ring buffers or queues already exist.

### Bad conditions

- Unbalanced stage latencies.
- Extra synchronization/queue overhead.
- Bounded queue adds backpressure and jitter.

## 4. SoA / primitive arrays

### Pattern

Replace:

```text
List<Book> where Book = {price, size}
```

with:

```text
DoubleArray prices
DoubleArray sizes
```

### Mechanism

- Removes object headers and references.
- Fewer cache lines per logical element.
- Better spatial locality and vectorization.
- Less GC pressure in JVM.

### Good conditions

- Sequential scans or full-depth materialization dominate.
- Object-per-entry is large relative to useful fields.
- Ordering can be represented by parallel arrays or index arrays.

### Bad conditions

- Random insert/delete with ordering (TreeMap semantics).
- Stable object identity/API compatibility is required.
- Many nullable fields / variant types.
- Update cost becomes O(n) copying.

### JVM note

Use `DoubleArray`, `LongArray`, `IntArray`; avoid `Array<Double>` and boxed collections.

## 5. Lazy materialization

### Pattern

Keep a compact internal state and expose:

```text
version / sequence / timestamp
```

Build the expensive consumer view only when requested, or at a lower cadence.

### Mechanism

- Avoids per-frame list/snapshot allocation.
- Avoids copying full depth when the consumer only needs BBO or a few levels.
- Avoids materializing updates that lose a race/sequence check.

### Good conditions

- Many updates are not consumed.
- Downstream only occasionally needs full snapshot.
- A sequence/version check can reject stale work first.

### Bad conditions

- Consumer needs the complete snapshot on every event.
- Lazy construction moves work into an already hotter path.
- Materialization must be consistent with a mutable internal book (needs locking/versioning).

## 6. Reduce copies / wrappers

### Smells

- `.take(n).map { ... }.toList()`
- full snapshot copy per update;
- wrapping primitives in multiple data classes;
- repeated `toBigDecimal()` / `toDouble()` conversions;
- string formatting in constructors.

### Mechanism

- Fewer allocations and memory traffic.
- Lower GC and cache pollution.
- Shorter dependency chains.

### Caution

- Copy-on-write may be required for thread safety.
- Immutability can be worth some copying; measure the actual hot path.

## 7. Non-temporal / streaming hints

### Pattern

```cpp
_mm_prefetch(addr, _MM_HINT_NTA);
```

or streaming stores where supported.

### Mechanism

- Hints that data is one-shot and should not pollute L2/L3.
- Useful for large sequential streams with no reuse.

### Bad conditions

- Data is reused by the next event. This is the classic failure mode: NTA evicts data that will be needed again.
- Small working set that fits in cache.
- Prefetch consumes LFB slots.

### Verification

- L1/L2/L3 hit rate;
- memory bandwidth;
- p99 latency and throughput;
- compare NTA vs T0 on the real event sequence.

## 8. Dead-data eviction / cache management

### C++ intrinsics

- `_mm_clflush`
- `_mm_clflushopt`
- `_mm_clwb`

### Mechanism

- `clflush` invalidates a line.
- `clflushopt` is a weaker-ordered flush.
- `clwb` writes back dirty data while retaining a clean local copy.

### Good conditions

- Event buffers are written/read once and then dead.
- Dead data occupies cache needed by persistent hot state.
- Ownership transfer is the actual bottleneck.

### Bad conditions

- Data is reused.
- LFB is already saturated; flush operations compete for memory resources.
- JVM: not available through standard Java/Kotlin; GC manages object movement.

## 9. Cross-core decoupling / MESI ownership

### Pattern

Avoid sending large result arrays back to the producer core. Send a small completion signal and let the producer read only what it needs, or write results into a shared buffer with appropriate write-back.

### Mechanism

- Avoids reading dirty lines from another core's private cache.
- Reduces cache-line ownership transfer and coherence traffic.

### Good conditions

- Producer-consumer split across cores.
- Consumer results are large but producer needs only a compact summary.
- Data can be partitioned by key.

### Bad conditions

- Producer genuinely needs all intermediate results.
- Shared-memory protocol becomes complex.
- False sharing remains in the handoff metadata.

### Verification

- cache-to-cache transfers;
- remote HITM / snoop counts;
- LLC misses;
- context switches and core migrations.

## 10. False-sharing removal

### Pattern

Pad hot per-thread fields to a cache line, or shard state by thread/core.

### Mechanism

- Prevents independent writers from invalidating each other's cache lines.
- Reduces coherence traffic.

### Good conditions

- Multiple threads frequently write adjacent counters/pointers.
- Profiler shows HITM / cache-line ping-pong.

### Bad conditions

- Memory overhead matters more than coherence.
- The fields are actually shared/read together.
- Threads rarely write concurrently.

## 11. Code / branch footprint

### Pattern

Reduce megamorphic dispatch, giant switches, cold code in hot functions, excessive inlining.

### Mechanism

- Reduces I-cache pressure and branch misprediction.
- Helps front-end throughput.

### Good conditions

- Hot loop is instruction-fetch bound.
- Large virtual dispatch / many branches.
- PGO shows hot/cold code mixing.

### Bad conditions

- Bottleneck is data memory, not instruction fetch.
- Refactor increases indirect dispatch.

## 12. Caching and precomputation

### Pattern

Cache small, frequently reused results; precompute invariants outside the loop.

### Mechanism

- Removes repeated work and memory traffic.
- Shrinks hot working set.

### Bad conditions

- Cache becomes large and reduces locality.
- Invalidation costs exceed savings.
- Cache misses in the cache itself.

## 13. Fixed point / integer tick representation

### Pattern

Use scaled `Long` or per-instrument tick counts for order prices, quantities, PnL, or accounting boundaries.

### Mechanism

- Avoids BigDecimal object allocation and arbitrary-precision overhead.
- Makes exact decimal/tick comparisons cheap.
- Avoids Double↔BigDecimal round trips.

### Good conditions

- Exact decimal/tick semantics are required.
- Values can be represented in a fixed scale/tick.
- Operations are mostly add/compare/round.
- Multiplication/division has a safe 128-bit/intermediate strategy.

### Bad conditions

- Transcendental math, optimization, normal CDF, SABR/Black-Scholes internals.
- Dynamic scales and wide value ranges.
- Frequent conversion to/from Double.
- Need to keep BigDecimal API/serialization compatibility.

### Verification

- Same trade/order decisions on golden data.
- Round-trip and boundary tests around ticks.
- Allocation/GC profiles.
- p50/p99 of the order/tick path.

## 14. Thread sharding by instrument

### Pattern

Route each instrument to a stable worker thread.

### Mechanism

- Keeps state hot in one core's cache.
- Avoids synchronization and dirty-line ownership transfer.
- Improves locality of order book and strategy state.

### Good conditions

- Instrument state is independent.
- Load can be balanced.
- Cross-instrument aggregation is not on the hot path.

### Bad conditions

- Heavy cross-instrument dependencies.
- Hot instruments create imbalance.
- Handoff/aggregation dominates.

## 15. Anti-patterns

- Prefetching without reuse.
- Batch size chosen by intuition rather than LFB/cache limits.
- Optimizing a cold path because the code looks inefficient.
- Adding threads to hide memory latency without measuring coherence cost.
- Replacing all floating-point math with fixed point.
- Microbenchmarking with tiny data that fits in L1.
- Measuring p50 only.
- Ignoring GC/allocation because "the algorithm is O(1)".
- Copying full snapshots because "it is simpler", then optimizing arithmetic.
