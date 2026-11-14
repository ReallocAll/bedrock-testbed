# Verification and release-candidate provenance

## Top 10 display and full dimension names — 2026-11-15

- [Fix commit `352ca1e`](https://github.com/ReallocAll/bds-onhit-lifecycle/commit/352ca1e41cee4bb7cab4e3bb6fcd16de5c511430) ([PR #8](https://github.com/ReallocAll/bds-onhit-lifecycle/pull/8)): `/ohf hit` now emits up to **10 rows in one message**, omits pagination/header, and uses `Dimension::getName()` instead of `OW/NE/END`. The `projectile` source-type event filter is **unchanged**; these are not shulker-bullet-only counts.
- [Native main CI 43626772742](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43626772742): **3/3 CTest PASS** (rank 1–10, full names, and one-message line count). [Artifact 14126451032](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43626772742/artifacts/14126451032) contains `endstone_onhitfix.so` SHA-256 `5f42577be9e5a25f2efb57cb64a8b8aaf2b91d99da1fa34a0b964d28a7f4d9a9`.
- [Isolated BDS 43626478198](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43626478198): **PASS** on exact BDS 1.26.60.6 / Endstone 0.11.15, using source commit `14af2c3` (same plugin content as squash-merged main). The live command returned `#1 Overworld X:0 Z:0 Hits:12`, `Obj del: 216` and `Obj mem: 8.43 KiB`; successful Shulker workload, simulated-player recreation and clean shutdown. The preceding [Testbed run 43626211343](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43626211343) failed solely because the harness waited for a populated row when the command initially returned an empty result; the harness was corrected.
- Native 10-record formatting is tested by CTest; this isolated workload observed one populated chunk, so actual 10-row client chat rendering is not independently claimed.

## Chat display regression — 2026-11-15

The original `/ohf hit` packed 20 rows into one multiline message and relied on space-padded, fixed-width columns. The Bedrock in-game chat display can clip long output, and its font is not reliably monospaced.

- Fix commit: [`826c0c4`](https://github.com/ReallocAll/bds-onhit-lifecycle/commit/826c0c428275b7a1cc1e7170859a128577585d49). Each page shows five explicitly labeled `X/Z/Hits` rows, with pages 1–4 covering rank 1–20; the `/ohf status` and Cleanup repair are unchanged.
- [Native main CI 43624800324](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43624800324): **PASS**, three CTests including 20-row/page-boundary assertions. The [artifact 14125936745](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43624800324/artifacts/14125936745) contains `endstone_onhitfix.so`, SHA-256 `5f8a4d8f98f66465a29927cab3f14541cad237138826894bd600bbc0c29e33be`.
- [Isolated BDS Testbed 43624883762](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43624883762), Testbed `fc3b8bf` pinned to `826c0c4`: **PASS**. BDS 1.26.60.6 / Endstone 0.11.15 registered and executed explicit `/ohf hit 1`, returned `Top 20 Chunks (1/1)` and `#1 OW X:0 Z:0 Hits:24`; `Obj del: 272`, `Obj mem: 10.62 KiB`; normal shutdown. The isolated workload occupied only one chunk, so its real BDS execution validates command registration and row rendering, while the 20-record, four-page behavior is covered by the standalone test.

This document records **observed** results for the OnHitFix 0.1.0 preview; it does not certify memory ownership or long-term production safety.

## Reviewed implementation

- Code commit: [`d6b8dd02f1f91383f9f4331ff2ca2dd9fb8e8e28`](https://github.com/ReallocAll/bds-onhit-lifecycle/commit/d6b8dd02f1f91383f9f4331ff2ca2dd9fb8e8e28).
- Native CI: [run 43617107044](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43617107044), **3/3 CTest passed** (identity gate, hit chunks, object-memory accounting).
- Downloadable Linux x86-64 [artifact 14122273834](https://github.com/ReallocAll/bds-onhit-lifecycle/actions/runs/43617107044/artifacts/14122273834): extracted `endstone_onhitfix.so` SHA-256 `9e9e731123bb3ea3406dbcd0af3e70cc924239a41d97110c7735b9bbc08576bd`.
- Real isolated BDS: [Testbed run 43617190702](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43617190702), Testbed commit `0035e25` pinned to code `d6b8dd0`. Exact BDS SHA-256 / Build ID, Endstone 0.11.15, automatic hook, real Shulker projectile, six sustained rounds, simulated-player recreation and clean shutdown **passed**.
- Testbed console: `Obj del: 236`, `Obj mem: 9.21 KiB` (no lower-bound marker); `/ohf hit` included OW (0, 0), 19 hits.
- The Testbed **rebuilt** the plugin from the pinned commit: binary SHA-256 `8777654c7d81fd0dc6a2cecf18cef76f2cbbeb53f3b082f3e5a2b6cc6925a5fb`. This differs from the downloadable CI artifact, so byte-identical runtime acceptance of that exact artifact is **not claimed**.

## User-controlled production preview (2026-11-15)

A user-authorized deployment to a matching BDS 1.26.60.6 / Endstone 0.11.15 server used the **downloadable CI artifact** with the recorded SHA-256. The server subsequently logged `OnHitFix Cleanup hook installed` and `Server started.`. A live command snapshot at 01:27:56 (UTC+8) reported `Obj del: 80932` and `Obj mem: 3.09 MiB`. The server remained running in the observed window.

These are short-lived operational observations, **not** a controlled post-deployment memory A/B experiment or a guarantee against crashes.

## Prior allocator A/B evidence

Under a comparable 24-round isolated Shulker workload, [OFF run 43573545076](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43573545076) retained target-path sampled objects (0 → 27 → 53), whereas [ON run 43574144248](https://github.com/ReallocAll/bedrock-testbed/actions/runs/43574144248) observed 0 target-path retained samples and 708 deleting-destructor calls. Both exited cleanly. These are **sampled, workload-specific** observations, not proof of every allocator free or general memory safety.

## Known limitation

BDS `0x7757F60` may shallow-copy child pointers across Cleanup records. Whether the same allocation generation can be deleted by multiple records is **inconclusive**, leaving potential double-free/UAF. `Obj mem` accounts for completed destructor calls, not necessarily unique allocations or RSS reclaimed. The plugin is **preview only**.

The original reverse-engineering chronology remains in [the archive](archive/README.md). The [machine-readable IDA observations](../evidence/object-size/ida-direct-observations.json) document all 19 registry keys and 18 concrete outer-object sizes.
