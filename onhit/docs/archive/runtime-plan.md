# Isolated runtime acceptance ledger

Updated 2026-11-14. This ledger distinguishes verified GitHub execution from remaining research. Production remains strictly read-only. No paid runner, cloud resource, or artifact upload is authorized.

## Verified isolated execution

- Official Endstone v0.11.15 C++ SDK build and link: **PASS**, OnHit Actions [43486566882](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43486566882), source `7e8940dd99a7a209d3fec8a602a784ab67a4b6a2`. Two CTests and heap parser also passed.
- Target BDS 1.26.60.6 executable identity: **PASS** in Testbed [43487320217](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43487320217), experiment commit `1fd3f98c29f44716982f242adecbc396b44d0fb5`. SHA-256 `ea16b541a448e07c337848d2c5f37321b706554b5216dd942f3f11e7a8ed2bad`, Build ID `52576b75b6af5a09a6bde582be804e29657dbbf3`, 265500280 bytes.
- Passive native plugin load, identity check, disable and graceful shutdown: **PASS**, same run. Hooks compiled OFF.
- Two-start Beta API initialization, EndstoneBot Bridge protocol 3, standalone Survival SimulatedPlayer, Normal difficulty, loaded chunk and actual `shulker_bullet` selector observation: **PASS**, Testbed [43490505011](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43490505011), experiment commit `b4f42d3a3890972a67ef352d73b3a59d3babe725`, bot commit `4bd0691f1ef0f551e5ba258d4747b35a3b748177`. This does not establish projectile contact or target factory execution.
- Existing production heap profiles: offline weighted comparison documents +463.024 MiB OnHit-associated estimated `inuse_space`; **stack attribution only**, not proof of leaked child objects. See [heap observations](heap-profile-observations.md).

## Remaining gates, in dependency order

1. **ABI and unwind — BLOCKED:** independently inspect exact ELF entry bytes, complete callsites, return conventions, prologue relocation and funchook trampoline exception/unwind behavior. See [ABI gate](abi.md). Identity verification alone does not authorize enabling experimental detours.
2. **Actual OnHit contact and native Hook hit — NOT VERIFIED:** establish authoritative collision/contact oracle and prove that the observed projectile invokes the candidate OnHit path. Execute active instrumentation only after gate 1, in isolated Testbed.
3. **Lifetime accounting — NOT VERIFIED:** record child allocation and actual deallocation, reset/copy/alias transitions, address generations, dropped events, trace overhead, and repeat hit/no-hit controls. Deleting-wrapper entry and vector backing-buffer release do not establish child free.
4. **Leak hypothesis — UNRESOLVED:** use matched repeated weighted `inuse_space` cohorts and object-level accounting to confirm or exclude retention.
5. **Conditional fix — NOT STARTED:** only after ownership proof, make a separate reversible change and test UAF/double-free, stability, overhead and retained-byte improvement.

## Operational boundaries

Continue on `feat/identity-and-diagnostic-scaffold` and Testbed `experiment/onhit-shulker`; leave Testbed `main` untouched and PRs in Draft. Pin cross-repository commits exactly. Do not publish BDS binaries, raw production heap profiles, private code, tokens or worlds. Use standard public-repository Ubuntu GitHub-hosted runners only after verifying zero-extra-cost eligibility. Do not equate a passing CI job with runtime Hook or leak validation.

The previous version of this ledger described the SDK build and isolated runtime as unexecuted; those statements were superseded by the linked successful runs. For full chronological evidence see [HANDOFF.md](engineering-log.md).
