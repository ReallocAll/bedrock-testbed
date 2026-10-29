# Bedrock Testbed

Minimal, real-server smoke tests for Bedrock clients. The baseline uses official
**Endstone 0.11.14 / BDS 1.26.52 (protocol 2193)** on Ubuntu 24.04 x86-64
with Python 3.13. EndPilot is fixed to
`b5a74d12497ddcc44eddf61ee6813138889ff40f`.

## Run locally (Linux)

```bash
python3.13 -m venv .venv-server
.venv-server/bin/python -m pip install 'endstone==0.11.14'
python3.13 -m venv .venv-client
.venv-client/bin/python -m pip install \
  'endpilot[raknet] @ git+https://github.com/ReallocAll/endpilot.git@b5a74d12497ddcc44eddf61ee6813138889ff40f'
.venv-client/bin/python testbed.py --server-python .venv-server/bin/python
```

The test runs two BDS starts: first, Endstone performs the official SHA256-checked
BDS installation; after a clean stop it configures offline mode, removes the
bootstrap world, and starts a fresh server. Every invocation removes `work/`
(including the world), so do not put any personal data there.

The single test checks Ready and server-side `list`, StartGame observations,
fresh ChunkRadiusUpdated responses to **public** `Bot.send_packet()`, invalid
packet isolation followed by another successful request, disconnect and process
cleanup. A successful queue admission is not considered a successful test.

`work/result.json` and `work/bds.log` retain the observable results; the CI job
uploads them even on failure. GitHub Actions runs on allowed branch pushes and
manual dispatch after the workflow exists on the default branch. It uses no PAT,
production server, or production world. First runs download BDS via Endstone;
the test never caches persistent server state.
