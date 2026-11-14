# ABI and Hook safety gate — not cleared

## Static hypotheses for exact BDS ELF

| Candidate | IDA EA | Raw input registers | Reported return | Critical uncertainty |
|---|---:|---|---|---|
| record initializer | `0x7143370` | RDI | RAX/int64 | callsite and exception behavior |
| on-hit parse/factory | `0x7143660` | RDI, RSI | RAX/uint64 | RSI semantic role unknown |
| cleanup/move | `0x7757840` | RDI, RSI, RDX, RCX | RAX/int64 | opaque extra arguments and tail jumps |

These shapes are reconstructed from IDA, not C++ source-level ABI contracts. The exact target ELF was disassembled in the isolated Testbed CI run described below; this is not an ABI or trampoline safety approval.

Relevant callsites: `0xB38FE01` (parse), `0x7757F05` (reset), `0x7756A0E`, `0x7756A24`, `0x7756AD7`, `0x7756B98`, `0x7758522`, and tail-jump `0x77569D0` (cleanup). Before installing a detour, inspect each prologue and complete callsite, hidden/aggregate/FPU arguments and returns, stack alignment, volatile register effects and exception propagation. Remember that a tail jump can leave a different unwind chain.

## Non-negotiable gate

1. Verify `/proc/self/exe` SHA-256 and **loaded main ELF** GNU Build ID. Both must match exactly. Verify EA+load-bias is within an executable mapped segment.
2. Verify target entry bytes and all caller conventions against the actual same ELF. The current identity gate implements only step 1; **the stronger ABI gate is not implemented**.
3. Determine how funchook 1.1.3 relocates prologues and whether a C++ exception can unwind through its trampoline. Do not enable the three C++ detours unless this is established (or replace them with a proven passive/assembly probe). `noexcept` event writers may not suppress/convert target exceptions.
4. Run an isolated no-trigger startup/shutdown, Hook install/uninstall and real hit/no-hit experiment. Validate event capture and BDS stability before interpreting any pointer.
5. Match deletion by pointer **and allocation generation**; track copies/aliases and concrete free calls. A `+8` wrapper *entry* alone is not a confirmed object deallocation.

## Current code limitation

`src/plugin.cpp` contains compile-time disabled experimental detours to support later experiments without modifying the Endstone core. They are **not enabled by default**, **not ABI-verified**, and **not tested against the target**. They collect pointer snapshots but do not observe object allocations or actual frees. They must not be used as-is to justify a deletion fix.

## Design evidence

Evario Endstone #420 case (API/ABI/runtime proof separation); MT01 ownership test scope; PT01 observer cost/loss accounting. Bounded source: archived `key-disassembly.md` and `IMPLEMENTATION_HANDOFF.md`.

## Exact-ELF disassembly execution — verified 2026-11-14

The isolated public standard-runner [Testbed run 43529829494](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43529829494) completed successfully on Testbed commit `9f123e6bf086d2ab260625708a8074cac6cf50d7`, pinning OnHit `d3c5ad210cf4fdffbcfc526ba3348647288cde28`. Its `Export exact ELF ABI evidence without enabling hooks` step **actually executed** `tools/export_abi_evidence.py` after the target identity check. The output is retained in the workflow job log, not a committed binary or uploaded artifact.

Observed target instructions (ELF-relative virtual addresses; excerpt, not a complete ABI proof):

| Target | Exact observed entry instruction |
|---|---|
| reset `0x7143370` | `66 c7 07 00 00` — `mov WORD PTR [rdi],0x0` |
| parse `0x7143660` | `55` — `push rbp` |
| cleanup `0x7757840` | `53` — `push rbx` |

Observed direct calls in the same ELF:

| Callsite | Encoded instruction | Target |
|---|---|---|
| `0xB38FE01` | `e8 5a 38 db fb` | `0x7143660` |
| `0x7757F05` | `e8 66 b4 9e ff` | `0x7143370` |
| `0x7756A0E` | `e8 2d 0e 00 00` | `0x7757840` |
| `0x7756A24` | `e8 17 0e 00 00` | `0x7757840` |
| `0x7756AD7` | `e8 64 0d 00 00` | `0x7757840` |
| `0x7756B98` | `e8 a3 0c 00 00` | `0x7757840` |
| `0x7758522` | `e8 19 f3 ff ff` | `0x7757840` |
| `0x77569D0` | `e9 6b 0e 00 00` (tail jump) | `0x7757840` |

**What this closes:** static target-address and call-edge existence on the exact executable used by the isolated test. The native SDK build also passed at OnHit [run 43529819025](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43529819025). The unattended bullet scenario still passed in the same Testbed run, but hooks were compile-time OFF.

**What remains open:** argument and return ABI (including hidden parameters), minimum safely relocatable instruction span and RIP-relative fixups, funchook trampoline exception/unwind metadata, concurrent uninstallation/quiescence, projectile contact, Hook entry hits, and object allocation/alias/free evidence. A passing `objdump` export cannot establish these. Do not turn on detours or claim the leak is confirmed.

## Exact-ELF register and entry-span review — 2026-11-14

Source: [isolated Testbed run 43529829494](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43529829494), job `132244476886`, step `Export exact ELF ABI evidence without enabling hooks`. This is **manual static interpretation of already-executed evidence**, not a new BDS run.

- **Parse**: at `0x7143671` the callee saves `rsi` in `r12`, at `0x7143674` saves `rdi` in `rbx`, then dereferences `[rsi+0x98]` at `0x7143677` and writes `[rdi+0x100]` at `0x714368b`. The direct caller at `0xB38FDFE` explicitly sets `rsi=rbx`; `rdi` was computed using stride `0x2f8`. This corroborates two pointer-like input registers on that edge, **not** the C++ prototype, object lifetime, or return ABI.
- **Reset**: `0x7143370` starts with `66 c7 07 00 00` (5 bytes), then an 8-byte `mov [rdi+8],-1`. Caller `0x7757F02` sets `rdi=rax` before calling it, and `0x7757F0A` overwrites `rax` with `r14` immediately after. That edge does **not** consume reset's `rax` as a return value; the current `int64_t` declaration is unsupported. An incorrect return prototype can be especially misleading for instrumentation even when a caller discards the result.
- **Cleanup**: `0x7757840` starts `push rbx` (1 byte), `mov rbx,rdi` (3 bytes), then `mov rdi,[rdi+0x2a8]` (7 bytes). At `0x7756A0B`, the direct caller sets `rdi=rsp` and calls cleanup; at `0x77569C3`, another caller sets `rdi=rbx`, restores its frame, and **tail-jumps** into cleanup. The first instructions establish `rdi` use, but do not establish that `rsi/rdx/rcx` are meaningful arguments. The current four-argument prototype is an unverified hypothesis.
- **Entry relocation boundaries**: the first *whole* instruction spans at least 5 bytes for reset, 5 bytes for parse (`push rbp; push r15; push r14`), and 11 bytes for cleanup (`push rbx; mov rbx,rdi; mov rdi,[rdi+0x2a8]`). These are instruction-boundary observations **only**; they do not establish which patch length funchook will choose, whether a nearby branch is safe, or whether relocation and unwind work. For parse, the next instructions include further pushes and stack adjustment. No active patch is authorized.
- **Exporter caveat**: the fixed `--stop-address` windows cut through instructions at their far edge (e.g. reset `0x71433ef` and cleanup `0x77578ba`). Such terminal `.byte` fragments are an artifact of the window, **not evidence of malformed BDS code**. Interpret only complete instructions at known entry/callsite boundaries.

**Decision**: retain compile-time-disabled hooks. Next obtain a validated signature and relocation/unwind behavior (including tail-jump and concurrent uninstall), then execute a minimal isolated install/uninstall and actual projectile-contact probe. Do not infer frees from cleanup entry. This review follows Evario's separation of static hook inspection from runtime proof (Endstone commit `ddd2699` case) and minimal evidence-gated implementation.

## Isolated active funchook result — 2026-11-14

The prior **never-enable-until-perfect-ABI** static warning was superseded **for the specifically authorized isolated experiment only**. It was not an ABI correctness proof. Active C++ detours have now been installed and executed in an ephemeral exact-ELF BDS test using opt-in `BDS_ONHIT_ISOLATED_TEST` and build flag `ONHIT_ENABLE_EXPERIMENTAL_HOOKS=ON`.

- Single Parse entry, [CI 43565136065](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43565136065): 9 Enter / 9 Exit, 0 lost; real Shulker bullet damage; successful native `funchook_install`, trampoline call, `funchook_uninstall`, trace write and BDS shutdown.
- Parse+Reset+Cleanup, [CI 43565510800](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43565510800): Parse 3/3, Reset 3/3, Cleanup 1/1 Enter/Exit, 14 trace rows, 0 drops; real simulated-player Shulker bullet damage and clean shutdown.
- Funchook source pinned to upstream v1.1.3 commit `88388db3c69e16c1560fee65c6857d75f5ce6fd5`, static PIC build. For the target entry lengths and concrete runtime paths these runs exercised, relocation and ordinary return paths functioned. **This does not establish all ABI or C++ exception unwind paths, asynchronous thread quiescence during uninstall, or ownership/free semantics.**
- Existing guessed Reset `int64_t` return and Cleanup four-argument declaration remain hypotheses even though normal original-call paths happened to complete. In particular, Reset's caller ignores `RAX`; a returned scalar in the TSV is not a semantic return value.
- A cleanup-entry event is **not** a child-object destructor or allocator deallocation. No reason to introduce a leak fix before following allocation generations and real frees.

The compiler option remains OFF by default. All full-scope experiments are isolated; production must remain passive/read-only.
