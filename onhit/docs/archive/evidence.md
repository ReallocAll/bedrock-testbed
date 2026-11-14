# Evidence and interpretation

## Input integrity

- Handoff ZIP SHA-256: `9d7ab8b6f23948089ae05f8754d622f40c9dcfaead886281ef864d7b4612360f`; 29 entries (manifest and 28 evidence/source files).
- Target BDS ELF SHA-256: `ea16b541a448e07c337848d2c5f37321b706554b5216dd942f3f11e7a8ed2bad`; ELF size per IDA identity file: 265,500,280 bytes; GNU Build ID: `52576b75b6af5a09a6bde582be804e29657dbbf3`.
- Target IDB SHA-256: `3eaf57495876b78b0997d353f236e0f5444189ed551ef150b6406a4252150470` (not distributed here).
- IDA base `0x0`; record stride `0x2F8`; vector raw-pointer triple at `+0x120/+0x128/+0x130`.

## Confirmed only by archived static observations

- `sub_7143660` `0x7143660`: resets `end=begin`, transfers a factory-created child's `unique_ptr` into a raw pointer appended to the vector. Its known direct callsite is `0xB38FE01`.
- `sub_7143370` `0x7143370`: initializes/zeros the vector triple; known callsite `0x7757F05`.
- `sub_7757840` `0x7757840`: in the examined body, rewinds the vector and frees **the backing allocation**, not observed iterating to delete children.
- `sub_7757F60` can shallow-copy child pointers. The complete ownership graph is unknown.
- The valid-generation branch of `sub_B38F1A0` can call `sub_7143660` without prior clear. That is a **conditional** loss of references, not evidence that live children existed at that moment.
- Six `+8` vtable deleting-wrapper candidates exist; their presence is not proof any recorded child passed through them.

## New production observations (user-reported, not independently inspected here)

2026-11-13, jemalloc 5.4.0 Heap Profiling had been enabled on the production BDS; 69 same-process `.heap` captures existed. From 09:03 to 19:33, `stats.allocated` increased roughly 588 MiB; in-use allocation sample counts increased 1,606 → 2,125, with OnHit factory-path-related retained sample counts 719 → 1,182 (463 of net 519, ~89.2%). The 89.2% is **a fraction of the *sample-count increment***, not a percentage of leaked bytes. The older handoff's `config.prof=0` statement is superseded by this later observation. The `.heap` files have not been accessed or parsed during the current project.

No weighted `inuse_space` comparison, pointer-generation correlation, controlled no-hit/hit cohorts or exact observed deallocations is present. The original hypothesis therefore remains **unconfirmed**.

## What would change the conclusion

A matched binary and safe instrumented runtime must show actual live child pointer identities before a destructive/reset boundary, actual references lost afterward, no alias owner, and real deallocation/retention evidence. After warm-up and quiescence, weighted `inuse_space` growth should scale across comparable hit cohorts relative to controls. Repeated snapshots alone cannot exclude normal allocations or allocator retention.

Source: original `evid-runtime/IMPLEMENTATION_HANDOFF.md`, `evid-supplement/identity.json`, `evid-supplement/evidence/key-disassembly.md`, `valid-handle-reparse-path.md`, and `on-hit-wrapper-releases.md` in the ZIP.
