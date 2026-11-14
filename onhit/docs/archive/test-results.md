# Executed verification ledger (2026-11-14)

## Local execution only

Environment: Linux x86-64 container, GCC 14.2.0, CMake, OpenSSL 3; **not** a BDS host. Source tree: `feat/identity-and-diagnostic-scaffold`.

| Check | Invocation | Result | What it proves |
|---|---|---|---|
| Identity negative gate | `cmake -S . -B build -DONHIT_BUILD_PLUGIN=OFF -DONHIT_BUILD_TESTS=ON && cmake --build build -j4 && ctest --test-dir build --output-on-failure` | PASS, 2/2 with bounded trace | Refuses unrelated executable; parses its local GNU Build ID; recognizes mapped executable segment |
| Concurrent bounded trace | Same | PASS, 2/2 | 4 producers × 40 events, 128 slots, 32 overflows, 256 omitted child pointers; unique ordered reservations |
| Sanitizer rerun | `-DCMAKE_CXX_FLAGS='-fsanitize=address,undefined -fno-omit-frame-pointer' -DCMAKE_EXE_LINKER_FLAGS='-fsanitize=address,undefined'` + CTest | PASS, 2/2 | No ASan/UBSan finding in narrow standalone checks |
| Optional and passive plugin syntax | GCC C++20 `-fsyntax-only` against *temporary handwritten stub* for Endstone/funchook | PASS | Syntax only; **not** Endstone binary compatibility |

**Not executed:** Endstone official SDK build, full plugin link, target ELF ELF-ID confirmation on a BDS host, live Hook install/uninstall, exception unwind, per-child generation and actual free tracing, jemalloc weighted profiles, controlled BDS cohorts, performance. No production writes; no GitHub Actions runs.

No result in this file constitutes real BDS lifecycle acceptance. The static trace/identity tests are intentionally small. Synthetic simulation cannot establish actual owner graphs.

## GitHub Actions spending boundary

The `2026-11` usage summary was retrieved via GitHub's current user billing API with a private credential reference. GitHub reported zero net billed Actions usage in the returned summary at the moment of retrieval. This does **not** prove that a new private Actions job cannot exceed the included allowance. An account-level stop-at-budget guard could not be verified; no Actions job was dispatched. Do not commit tokens, API responses or other private billing records.
