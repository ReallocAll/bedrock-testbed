# Bedrock Testbed

Minimal real-server smoke tests on Ubuntu 24.04 x86-64. Server: **Endstone
0.11.14 / BDS 1.26.52 / protocol 2193** on Python 3.13. Client: EndPilot
`73b26f632d4dc6f04f9448bacc1928fedc7b7ff2` (default, overridable) on Python 3.12.

The client's `bedrock-protocol-native==0.5.0.dev0` is not on PyPI. Use the
CI-verified `python-pure` and `python-native-Linux` CPython 3.12 wheels from
[bedrock-protocol run 40091787005](https://github.com/ReallocAll/bedrock-protocol/actions/runs/40091787005).
The workflow downloads these pinned artifacts using read-only cross-repo access.
The token is passed only to the official GitHub download action.
These exact upstream Actions artifacts expire **2027-01-22 11:54 UTC**.
The upstream `release.yml` already packages the producer's wheels into a draft
tagged Release, but no published release assets are currently available; move
to a published producer Release or PyPI before the Artifact expires rather
than copying its native build infrastructure into this testbed.


## Run locally (Linux)

```bash
python3.13 -m venv .venv-server
.venv-server/bin/python -m pip install 'endstone==0.11.14'
python3.12 -m venv .venv-client
# Extract the pinned artifacts to dependencies/pure and dependencies/native first.
.venv-client/bin/python -m pip install dependencies/pure/*.whl dependencies/native/*.whl \
  'endpilot[raknet] @ git+https://github.com/ReallocAll/endpilot.git@73b26f632d4dc6f04f9448bacc1928fedc7b7ff2'
.venv-client/bin/python testbed.py --server-python .venv-server/bin/python \
  --endpilot-commit 73b26f632d4dc6f04f9448bacc1928fedc7b7ff2
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
The input extension scenario teleports into the fresh test world, submits
34 frames through a standalone event subscriber, and compares BDS `querytarget`
coordinates before and after movement. This is a server-authoritative oracle,
not a count of submitted input frames or the client's predicted position.
The same Runtime also connects two named Bots to BDS, verifies server-side
player counts (2 → 1 → 2 → 0), checks that the surviving Bot still receives a
packet response after its peer disconnects, and reconnects the original Bot
after input execution. After reconnect, old trade/entity/offer references
must fail while a new Session can reopen the same server villager. The high-altitude movement test confirms active
server gravity before asserting horizontal displacement. A stable
server-built platform enables the authoritative jump-height check.

The inventory scenario confirms BDS acknowledges public
`InteractPacket(Action.OPEN_INVENTORY)` with `ContainerOpenPacket`.
BDS grants four emeralds; the Bot observes their identity/count through
the Item Registry, moves one from hotbar to main inventory, and awaits
an authoritative ItemStackResponse. Item totals must remain four.
Final `list` verifies **zero players online**, rather than only the
absence of the original Bot. A real BDS villager is summoned within interaction range, and EndPilot
uses only its public entity reference, trade-window and item-request APIs.
Since BDS chooses the villager profession and quotes, the smoke selects a
single-ingredient, currently available trade that sells an emerald, grants
the quoted input through BDS, stages one surplus item, executes the Trade2
request, returns the residual and verifies item conservation. Each request
must receive the successful server response, and BDS must acknowledge the
container close. After the completed exchange the Bot reopens the villager window and
checks that a fresh BDS quote advances the recipe use counter exactly once,
without changing the maximum. The same server recipe is then exercised repeatedly until BDS reports
`uses == max_uses`, proving an actual exhaust-and-refuse boundary:
each exchange is acknowledged by BDS, inputs/outputs are conserved,
and a subsequent public Trade2 request must be rejected locally without
writing another ItemStackRequest packet. The current smoke includes **24 checks**.
No additional server boot or Bot manager is required.

No production servers/worlds, generic adapters, custom packet codecs or test
controllers are involved. The server world and process are never cached.
