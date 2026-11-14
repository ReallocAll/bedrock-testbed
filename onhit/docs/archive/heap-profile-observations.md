# Independent production heap snapshot check (read-only)

**2026-11-14 Asia/Taipei.** Existing files were accessed strictly by read-only SSH/SFTP. No production profile was triggered, no process memory modified, no files written remotely, and no plugin/hook deployed. Raw `.heap` files and process maps are **not** committed.

## Matched observations

Two existing files from a single PID (467575): `bds.467575.20261113-090310.heap` and `bds.467575.20261113-193312.heap`. Both had the `heap_v2/1048576` sampling header and the same BDS executable offset-zero mapping. The archived suspect path's stack contained **raw return PC offset `0x71439E0`**; the raw return PC is *not* the same as a function entrypoint (symbolizers commonly subtract one before attribution).

| Per-stack `t*` samples, main thread aggregate excluded | 09:03 | 19:33 | Delta |
|---|---:|---:|---:|
| Total in-use sampled allocations | 1,606 | 2,125 | +519 |
| Total sampled payload bytes (unweighted) | 132,999,156 | 130,368,272 | −2,630,884 |
| OnHit-related raw return PC `0x71439E0` samples | 719 | 1,182 | +463 |
| OnHit-related sampled payload bytes (unweighted) | 75,600 | 126,256 | +50,656 |
| **Estimated weighted total `inuse_space` (MiB)** | **1,702.573** | **2,220.286** | **+517.713** |
| **Estimated weighted OnHit-stack `inuse_space` (MiB)** | **719.036** | **1,182.060** | **+463.024** |
| Estimated weighted remaining stacks (MiB) | 983.537 | 1,038.225 | +54.688 |

OnHit contributes approximately **89.4%** of the weighted-in-use *difference*, using these two point-in-time samples. This is an **allocation stack association**, not proof the retained allocations are actual live `OnHitSubcomponent` objects or that `ProjectileComponent` lost ownership. Sampling variance, other owners, unobserved frees, and different workload volumes remain unresolved. Process-wide `stats.allocated` is a different measurement; do not equate it to sampled bytes or these weighted estimates.

## Weighting method and reproducibility

The standard jemalloc `jeprof` `heap_v2` sampling adjustment is Poisson: for each individual stack's `t*` sampled in-use count `n` and byte sum `s`, with sample interval `S=1,048,576`, use

`weighted_inuse_space = s / (1 - exp(-(s/n)/S))` for `n>0`.

Sum weighted bytes **per stack**, not by adjusting the already-aggregated totals; do not add `t0`, `t1`, etc. on top of `t*` because they partition the same samples. Read the `MAPPED_LIBRARIES` section and subtract the **main executable's** offset-zero mapping to resolve candidate return PCs. We applied this formula per stack, not a `n × S` shortcut. These results were computed from the existing snapshots with an in-memory read-only parser; `tools/heap_v2_summary.py` is a reproducible offline implementation for future copies of those exact files.

```sh
python3 tools/heap_v2_summary.py /path/to/bds.467575.20261113-090310.heap /path/to/bds.467575.20261113-193312.heap --target-return-ea 0x71439e0
python3 tests/test_heap_v2.py
```

The utility rejects mismatch of observed snapshot PID (when filenames expose it), sample rate or load bias. It never connects to a running process. Its numerical algorithm follows jemalloc `bin/jeprof.in` `AdjustSamples` for `heap_v2`; native `jeprof` was unavailable in the local execution container and was **not run**. Official sources: [jemalloc heap profile format](https://jemalloc.net/jemalloc.3.html), [jeprof source](https://github.com/jemalloc/jemalloc/blob/dev/bin/jeprof.in).

## Still required

- A matched *isolated* BDS process, verified identity, safe ABI and exception-unwind evidence.
- An actual pointer and allocation-generation trace of creation/reset/alias/destruction/**true free**, not only stack samples.
- Matched hit and no-hit controlled cohorts with quiescent weighted `inuse_space`, observer-cost and loss accounting.
- A reversible fix only when an exclusive-owner violation is confirmed. No fix is justified by these observations alone.
