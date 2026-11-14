# OnHitFix historical validation and investigation material

This directory is the home for OnHitFix's **historical verification, Testbed investigation notes, and offline profiling tools**. They were relocated from [ReallocAll/bds-onhit-lifecycle](https://github.com/ReallocAll/bds-onhit-lifecycle) to keep that repository focused on the native plugin.

- [Verification and release-candidate provenance](docs/verification.md)
- [Historical engineering handoff](HANDOFF.md)
- [Historical investigation archive](docs/archive/README.md)
- [Offline heap profile comparison](tools/heap_v2_summary.py) and [test](tests/test_heap_v2.py)
- [Read-only ELF ABI evidence helper](tools/export_abi_evidence.py)

The **active** isolated BDS acceptance harness remains in Testbed's `experiment/onhit-shulker` branch: `.github/workflows/onhit-bds.yml`, `onhit_probe.py` and `onhit_shulker_probe.py`.

Plugin sources and current [IDA object-size evidence](https://github.com/ReallocAll/bds-onhit-lifecycle/tree/main/evidence/object-size) remain in [OnHitFix](https://github.com/ReallocAll/bds-onhit-lifecycle). Historical documents were preserved as written; old relative source-tree links may require following the original OnHitFix repository.

No tool in this archival directory is installed into BDS by the active OnHitFix workflow.
