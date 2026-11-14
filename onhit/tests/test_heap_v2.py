from pathlib import Path
from tempfile import TemporaryDirectory
import math
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from heap_v2_summary import snapshot


def test_profile() -> None:
    profile = """heap_v2/1048576
  t*: 99: 99 [0: 0]
@ 0x1000100 0x1100030
  t*: 2: 256 [0: 0]
  t1: 2: 256 [0: 0]
@ 0x2000001
  t*: 1: 4096 [0: 0]
MAPPED_LIBRARIES:
1000000-1100000 r--p 00000000 00:01 1 /fake/bedrock_server
"""
    with TemporaryDirectory() as tmp:
        p = Path(tmp) / "bds.42.test.heap"
        p.write_text(profile)
        got = snapshot(p, 0x100)
    assert got["target_sampled_count"] == 2
    assert got["sampled_count"] == 3
    assert got["target_sampled_payload_bytes"] == 256
    assert got["sampled_payload_bytes"] == 4352
    assert math.isclose(got["target_weighted_inuse_space_bytes"],
                        256 / -math.expm1(-128 / 1048576), rel_tol=1e-7)


if __name__ == "__main__":
    test_profile()
    print("synthetic heap_v2 Poisson weighting checks passed")
