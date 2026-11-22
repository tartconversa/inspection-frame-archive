"""whole 级质量作废的合成测试：夹具只含 geometry/pixels，这里补全覆盖。"""
import json
import tempfile
import unittest
from pathlib import Path

from filmarchive.archive import Archive
from filmarchive.claims import ClaimStatus, verify_claim
from filmarchive.model import PriorClaim
from filmarchive.query import read_window

ROOT = Path(__file__).resolve().parents[1]


class WholeEffectTests(unittest.TestCase):
    def _archive_with_whole_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src, assets = tmp / "sources", tmp / "assets"
            src.mkdir()
            # 复用真实 sources 的前两批，再追加一条 whole 级作废
            for name in ("01-captures.json", "01-geometry.json", "01-tiles.json",
                         "01-preview.json", "01-prior_claims.json",
                         "01-reference_pixels.json", "01-requests.json"):
                (src / name).write_text((ROOT / "sources" / name).read_text())
            (src / "02-quality-whole.json").write_text(json.dumps({
                "intake": 2, "table": "quality",
                "records": [{
                    "id": "q-whole", "issued_for": "shot-a", "effect": "whole",
                    "region": [], "cause": "camera-recall",
                }],
            }))
            return Archive(src, ROOT / "assets")

    def test_whole_notice_blocks_new_requests_and_voids_claim(self):
        archive = self._archive_with_whole_notice()
        snap = archive.snapshot(2)

        # 新请求：整幅作废，定位被拦截
        result = read_window(snap, "shot-a", (104, 204, 112, 210), "qa")
        self.assertIsNone(result.window)
        self.assertEqual({n.id for n in result.positioning_blockers}, {"q-whole"})

        # 已签发引用 z01：终局 void，缘由说明是整幅图
        claim = next(c for c in snap.prior_claims if c.id == "z01")
        report = verify_claim(snap, claim)
        self.assertEqual(report.status, ClaimStatus.VOID)
        self.assertEqual(report.quality_reasons[0]["effect"], "whole")
        self.assertTrue(any("整幅图作废" in n for n in report.notes))


if __name__ == "__main__":
    unittest.main()
