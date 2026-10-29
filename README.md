# Bedrock Testbed

Minimal real-server smoke tests on Ubuntu 24.04 x86-64. Server: **Endstone
0.11.14 / BDS 1.26.52 / protocol 2193** on Python 3.13. Client: EndPilot
`8f920627fbf8e2d34bfc2d16d56f8163c36db3ca` (default, overridable) on Python 3.12.

The client's `bedrock-protocol-native==0.5.0.dev0` is not on PyPI. Use the
CI-verified `python-pure` and `python-native-Linux` CPython 3.12 wheels from
[bedrock-protocol run 40091787005](https://github.com/ReallocAll/bedrock-protocol/actions/runs/40091787005).
The workflow downloads these pinned artifacts using read-only cross-repo access.
The token is passed only to the official GitHub download action.

## Run locally (Linux)

```bash
python3.13 -m venv .venv-server
.venv-server/bin/python -m pip install 'endstone==0.11.14'
python3.12 -m venv .venv-client
# Extract the pinned artifacts to dependencies/pure and dependencies/native first.
.venv-client/bin/python -m pip install dependencies/pure/*.whl dependencies/native/*.whl \
  'endpilot[raknet] @ git+https://github.com/ReallocAll/endpilot.git@8f920627fbf8e2d34bfc2d16d56f8163c36db3ca'
.venv-client/bin/python testbed.py --server-python .venv-server/bin/python \
  --endpilot-commit 8f920627fbf8e2d34bfc2d16d56f8163c36db3ca
```

Use Actions **Run workflow** with the optional `endpilot_commit` input to test an exact
40-character commit; pushes use the verified default above. The test reads
`endpilot`'s PEP 610 `direct_url.json` from the installed distribution and fails
before starting BDS if its actual VCS commit differs. `work/result.json` records
both expected `endpilot_commit` and `installed_endpilot_commit`, including on
failure. Input must be lowercase hex, not a branch name.

The test performs two BDS starts: the first lets official Endstone fetch and
verify BDS; after stopping it, the test configures offline mode, deletes the
bootstrap world, and starts a clean world. **Every invocation deletes `work/`**;
do not store personal data there.

It requires both EndPilot Ready and server-side `list`, received world data,
new server ChunkRadiusUpdated responses after public `send_packet()`, continued
correctness following rejected invalid packets, and clean disconnect and BDS
process shutdown. The event subscription scenario additionally confirms two independent
PacketDecodedEvent observers, observer-exception reporting without shutting down
the healthy session, and idempotent unsubscription. Passing queue admission
alone cannot pass the test.

GitHub Actions saves `work/result.json` and `work/bds.log`, even on failure.
No production servers/worlds, generic adapters, custom packet codecs or test
controllers are involved. The server world and process are never cached.
