# Case Study: The Latency Slippage

> 参考案例，不是生产优化配方。  
> 来源：CppCon 2026，Sanchit Gupta（Graviton Research Capital），演讲 “The Latency Slippage”。
>
> 视频：https://youtu.be/FDjXUP6Am1M  
> 幻灯片：https://github.com/CppCon/CppCon2026/blob/main/Presentations/The_Latency_Slippage.pdf

## 1. 为什么这个案例值得放进 skill

这个案例和期权做市/低延迟交易很接近：

- 每 **5µs** 收到一条标的行情；
- 标的价格变化时，要重算关联的 **64 个期权**；
- alpha 信号越过阈值时触发交易；
- 最终在**不改数学公式、不改算法复杂度**的前提下，把 p50 从 **510ns 降到 329ns**，p90 从 **1012ns 降到 555ns**。

它说明：在纳秒/微秒级系统里，真正的瓶颈经常不是公式，而是：

- cache miss；
- memory-level parallelism（MLP）；
- LFB / MSHR 容量；
- 数据复用和 cache pollution；
- 跨核 dirty-line ownership；
- 代码/数据布局。

## 2. 测试环境

- CPU：Intel Core i9-14900KS（Raptor Lake）
- 核心频率：锁定 3.19 GHz
- 关键层次：L1 → L2 → L3 → DRAM
- 文章强调：L1 到 DRAM 延迟相差 100 倍以上

### 内存足迹估算

单次事件处理大约需要：

| 数据 | 缓存行数（约） |
|---|---:|
| 1 条行情事件 | 3 |
| 股票元数据 | 9 |
| 64 个期权对象 × 2 行 | 128 |
| 单次事件合计 | 约 140 行 |

全局：

- 查找表 + 环形缓冲区：约 **50.7 MB**
- L3：约 **36 MB**

结论：工作集已经超过 L3，cache miss 不可避免。优化目标不是消灭 miss，而是：

> 降低每次 miss 的代价，并让多个 miss 重叠。

## 3. 基线

- p50：**510ns**
- p90：**1012ns**

在普通 Web 系统里这几乎等于零；在做市系统中，这 510ns 中很大一部分是 CPU 被流水线挂起、等待内存。

## 4. 四步优化

> 文章正文和汇总表在个别数字上有 469/474ns 的小差异；下表按文章汇总表记录，并用“约”标注正文里的差异。

| 回合 | 技术 | 回应的硬件问题 | p50 | p90 |
|---|---|---|---:|---:|
| 基线 | 串行逐个重算 | — | 510ns | 1012ns |
| 第一步 | 批处理 `D=4` | Prediction / MLP | 约 469–474ns | 776ns |
| 第二步 | 软件预取 `_MM_HINT_T0` | Prediction | 414ns | 710ns |
| 第三步 | `clflushopt` 清理事件缓存行 | Retention | 409ns | — |
| 第四步初版 | 双核 40 + 24 | Ownership | 364ns | — |
| 第四步终版 | 解耦回读 + `_mm_clwb` | Ownership | **329ns** | **555ns** |

### 4.1 第一步：批处理，释放 MLP

问题：原始循环是串行的：

```text
load option 0 → 等 → compute 0
load option 1 → 等 → compute 1
...
```

每次 compute 都依赖自己的 load，内存延迟无法重叠。

方案：

1. 第一趟只对一批期权发起 load；
2. 第二趟集中计算，此时数据大多已进入缓存。

关键限制：**LFB**。

- i9-14900KS 每核约 **16 个 LFB 槽位**；
- 每个未完成 cache miss 占一个槽位；
- 每个期权对象约占 2 个 cache line；
- `D=4` 约 8 个 line 在途，能放下；
- 文章也试了 `D=64`，结果大量请求把 LFB 打满，性能计数器记录到约 **1001 个周期**完全挂起在 Fill Buffer 上。

教训：

> batch size 不是越大越好，要匹配 LFB 和 cache 容量。

### 4.2 第二步：软件数据预取

问题：第一趟仍使用普通 load。普通 load 有目标寄存器，需要等数据返回，会占 ROB / 指令窗口。

`_mm_prefetch` 没有目标寄存器，不产生数据依赖，发出后可以很快退出。

```cpp
_mm_prefetch(reinterpret_cast<const char*>(&option), _MM_HINT_T0);
```

每个期权对象占 2 个 cache line，所以需要预取两条：

```cpp
_mm_prefetch(reinterpret_cast<const char*>(&option), _MM_HINT_T0);
_mm_prefetch(reinterpret_cast<const char*>(&option) + 64, _MM_HINT_T0);
```

结果：

- p50：约 469ns → 414ns
- p90：776ns → 710ns

#### 反例 1：`_MM_HINT_NTA`

`NTA` 暗示“只用一次，不要污染 L2/L3”。但期权数据在后续行情事件中还会继续使用。

结果：延迟反而升到 **850ns**。

教训：

> `T0` vs `NTA` 取决于数据复用。不要为了“看起来更快”而把后续还要用的数据赶出缓存。

#### 反例 2：用假 load 模拟预取

假 load 同样占用 ROB、无法提前退出，结果多出约 **292 个停顿周期**。

教训：

> prefetch 的收益来自“轻量、可提前退出”，不是来自“提前摸一下”。

### 4.3 第三步：主动清理死数据

问题：事件环形缓冲区中的行情事件通常只读一次，处理完就死了。但硬件不知道，伪 LRU 仍可能把它留在 L1/L2/L3，挤掉长期复用的期权元数据。

方案：用 `clflushopt` 主动驱逐事件对应的 3 个缓存行。

结果：

- p50：414ns → 409ns
- 表面收益只有 5ns；
- 但 L1 命中率提升，平均等待时间下降 **21.3%**。

#### 陷阱

`clflushopt` 自己也要占用资源和 LFB 槽位。  
高内存压力下乱刷，可能抢 LFB、造成反向停顿。

教训：

> 不要对没有价值的优化抱有执念，必须用硬件性能计数器验证。

### 4.4 第四步：双核并行与 MESI

单核优化到 409ns 后，开始双核：

- 主核算前 40 个 option；
- 辅核算后 24 个 option；
- 通过共享内存 + 原子标志同步。

第一次双核只到 **364ns**，远没有接近减半。

原因：**MESI dirty-line ownership transfer**。

流程：

1. 辅核把结果写入自己的 L1/L2，缓存行变成 Modified；
2. 主核去读结果，L1 miss；
3. 请求穿透到 L3，触发跨核嗅探；
4. 辅核被迫刷脏行；
5. 数据经互连网络传给主核。

代价：

- 主核读脏行：约 **27.7ns**
- 如果主核还要写：约 **53.8ns**
- 相比之下，L1 命中只有约 0.8ns

#### 药方 1：先问“真的需要读回来吗”

主核只需要辅核最终的命中列表，不需要完整中间状态。  
于是解耦结果，只传极小的确认信标，避免读回脏缓存行。

#### 药方 2：必须传的数据用 `_mm_clwb`

`clwb`：

- 把脏数据同步回 L3；
- 但保留本地副本；
- 不会像 `clflush` 那样把本地缓存行彻底失效。

这样主核之后读的时候，数据已经在 L3，避免昂贵的跨核嗅探。

最终：

- p50：510ns → **329ns**
- p90：1012ns → **555ns**

## 5. 失败案例同样是路标

| 尝试 | 结果 | 原因 |
|---|---|---|
| `D=64` | 性能大幅倒退，约 1001 周期卡在 LFB | 超出 16 个 LFB 槽位 |
| `_MM_HINT_NTA` | 延迟升到 850ns | 把后续还要用的数据逐出缓存 |
| 假 load 模拟预取 | 多 292 个停顿周期 | 占 ROB，无法提前退出 |
| 无限刷新/不验证 | 可能抢 LFB | 优化本身也要消耗资源 |

## 6. 这个案例对应到 skill 的检查点

### Step 0：定义 workload

必须记录：

- 每个事件处理多少对象？
- 事件频率是多少？
- 热数据工作集多大？
- p50/p99 预算多少？
- 当前在 I/O 线程、Disruptor 线程还是 worker 线程？

本案例：

```text
64 options / underlying tick, every 5µs, p99 budget << 1µs per option
```

### Step 3：估计 cache footprint

用：

```text
lines(event) ≈ 3 + 9 + 64 × 2 = 140 cache lines
```

再算：

```text
working_set = tables + ring_buffers + in_flight_events
```

如果工作集超过 L3，就不能假设“顺序访问会自动命中”。

### Step 4：生成候选

本案例对应：

- two-pass batching；
- software prefetch；
- T0 vs NTA；
- dead-data eviction；
- cross-core decoupling；
- clwb / ownership；
- avoid unbounded batch.

### Step 5：证伪

必须问：

- 数据会被复用吗？
- batch 会不会超过 LFB？
- prefetch 会不会太早/太晚？
- 多核是否引入 ownership transfer？
- p50 改善是否以 p99 为代价？
- 是否真的在 hot path？

## 7. C++ 实现注意事项

- `_mm_prefetch` 预取的是**数据**，不是指令，也不是 L1I。
- `_mm_prefetch` 的地址无效时通常不会触发缺页，但不要依赖未定义行为。
- LFB 大小和可用 PMU 事件依 CPU 型号/虚拟化环境而定。
- 用 `perf` 看：
  - `l1d_pend_miss.fb_full`
  - `cycle_activity.stalls_l1d_miss`
  - `mem_load_retired.l1_miss` / `l2_miss` / `l3_miss`
  - `perf c2c` 看 HITM / 跨核脏行。
- JVM/Kotlin 不能直接照搬这些 intrinsics；应优先做 primitive array、SoA、批量处理、lazy materialization、减少分配。

## 8. 一句话总结

> **不改数学、不改 O(N)，只改变 CPU 等待内存的方式：让 miss 重叠、让预取提前、让死数据离开缓存、让核间只传必要结果。**

## 9. 引用

- CppCon 2026 talk: “The Latency Slippage”, Sanchit Gupta, Graviton Research Capital
- Video: https://youtu.be/FDjXUP6Am1M
- Slides: https://github.com/CppCon/CppCon2026/blob/main/Presentations/The_Latency_Slippage.pdf
