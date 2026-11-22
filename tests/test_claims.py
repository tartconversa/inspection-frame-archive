"""已签发引用与参考像素的跨批次核对测试。"""
import unittest
from pathlib import Path

from filmarchive.archive import Archive
from filmarchive.claims import (
    ClaimStatus, ReferenceVerdict, check_reference_pixels, verify_claim,
)
from filmarchive.model import PriorClaim

ROOT = Path(__file__).resolve().parents[1]


class ClaimVerificationTests(unittest.TestCase):
    def setUp(self):
        self.archive = Archive(ROOT / "sources", ROOT / "assets")

    def test_z01_pgm_replay_byte_identical_at_every_intake(self):
        digests = set()
        for intake in (1, 2, 3):
            snap = self.archive.snapshot(intake)
            claim = next(c for c in snap.prior_claims if c.id == "z01")
            report = verify_claim(snap, claim)
            self.assertEqual(report.status, ClaimStatus.REPLAYABLE)
            digests.add(report.recomputed_sha256)
        self.assertEqual(len(digests), 1)  # 三个批次重算字节完全一致

    def test_z02_superseded_but_replays_same_bytes_and_origin(self):
        snap1 = self.archive.snapshot(1)
        snap3 = self.archive.snapshot(3)
        claim1 = next(c for c in snap1.prior_claims if c.id == "z02")
        claim3 = next(c for c in snap3.prior_claims if c.id == "z02")
        r1 = verify_claim(snap1, claim1)
        r3 = verify_claim(snap3, claim3)
        self.assertEqual(r1.status, ClaimStatus.REPLAYABLE)
        self.assertEqual(r3.status, ClaimStatus.SUPERSEDED)
        # 原合同坐标出处不变、字节不变
        self.assertEqual(r3.read.geometry_id, "gb")
        self.assertEqual(
            r1.recomputed_sha256, r3.recomputed_sha256,
        )
        self.assertTrue(any("gb2" in note for note in r3.notes))

    def test_z03_void_geometry_with_scope_explanation(self):
        snap3 = self.archive.snapshot(3)
        claim = next(c for c in snap3.prior_claims if c.id == "z03")
        report = verify_claim(snap3, claim)
        self.assertEqual(report.status, ClaimStatus.VOID)
        effects = {q["effect"] for q in report.quality_reasons}
        self.assertIn("geometry", effects)
        joined = " ".join(report.notes)
        self.assertIn("坐标定位作废", joined)

    def test_z04_void_both_geometry_and_pixels_in_region(self):
        snap3 = self.archive.snapshot(3)
        claim = next(c for c in snap3.prior_claims if c.id == "z04")
        report = verify_claim(snap3, claim)
        self.assertEqual(report.status, ClaimStatus.VOID)
        scopes = {q["affected"] for q in report.quality_reasons}
        self.assertIn("pixels-in-region", scopes)
        self.assertIn("geometry", scopes)
        # 像素仍可重算（c3 字节未被替换），但证据状态是作废
        self.assertIsNotNone(report.recomputed_sha256)

    def test_z03_was_already_unpinnable_before_quality_notice(self):
        # intake1 没有任何作废：z03 的区域超出其固定小块 c0，必须拒绝无声拼接
        snap1 = self.archive.snapshot(1)
        claim = next(c for c in snap1.prior_claims if c.id == "z03")
        report = verify_claim(snap1, claim)
        self.assertEqual(report.status, ClaimStatus.UNRECONSTRUCTABLE)
        self.assertTrue(any("固定小块集合之外" in n for n in report.notes))

    def test_z04_replayable_before_void(self):
        snap1 = self.archive.snapshot(1)
        claim = next(c for c in snap1.prior_claims if c.id == "z04")
        report = verify_claim(snap1, claim)
        self.assertEqual(report.status, ClaimStatus.REPLAYABLE)
        self.assertIsNotNone(report.recomputed_sha256)

    def test_replaced_tile_never_silently_substitutes_pinned_claim(self):
        # 构造一条固定在 b1 上的引用：用 b1 盒内全部像素中心的严格外框
        gb = self.archive.snapshot(3).geometries["gb"]
        points = [gb.pixel_center(c, r) for c in range(5, 12) for r in range(0, 4)]
        xs = sorted({p[0] for p in points})
        ys = sorted({p[1] for p in points})
        claim = PriorClaim(
            id="probe-b1", capture="shot-b", geometry="gb",
            region=(int(xs[0]), int(ys[0]), int(xs[-1]) + 2, int(ys[-1]) + 2),
            tiles=("b1",), form="raw", recipient="probe", intake=1,
        )
        # intake1：b1 完好，可重放
        r1 = verify_claim(self.archive.snapshot(1), claim)
        self.assertEqual(r1.status, ClaimStatus.REPLAYABLE)
        # intake3：b1 被 b1-fix 明确替换，固定 b1 的引用不得拿新块冒充原字节
        r3 = verify_claim(self.archive.snapshot(3), claim)
        self.assertEqual(r3.status, ClaimStatus.UNRECONSTRUCTABLE)
        self.assertTrue(any("b1-fix" in note and "替换" in note for note in r3.notes))
        self.assertIsNone(r3.recomputed_sha256)


class ReferencePixelTests(unittest.TestCase):
    def setUp(self):
        self.archive = Archive(ROOT / "sources", ROOT / "assets")

    def test_all_reference_points_match_at_intake1(self):
        snap = self.archive.snapshot(1)
        checks = check_reference_pixels(snap)
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(c.verdict is ReferenceVerdict.MATCH for c in checks))
        by_id = {c.reference.id: c for c in checks}
        self.assertEqual(by_id["a-ref"].observed, 194)
        self.assertEqual(by_id["b-ref"].observed, 389)
        self.assertEqual(by_id["b-ref2"].observed, 623)

    def test_conflicted_reference_reports_conflict_at_intake2(self):
        # b-ref2 落在 b1/b1-copy 冲突盒内（像素 8,3）
        snap2 = self.archive.snapshot(2)
        checks = {c.reference.id: c for c in check_reference_pixels(snap2)}
        self.assertEqual(checks["b-ref2"].verdict, ReferenceVerdict.CONFLICT)
        self.assertIsNone(checks["b-ref2"].observed)
        # b-ref 位于 b0，不受冲突影响
        self.assertEqual(checks["b-ref"].verdict, ReferenceVerdict.MATCH)

    def test_reference_resolves_after_replacement(self):
        snap3 = self.archive.snapshot(3)
        checks = {c.reference.id: c for c in check_reference_pixels(snap3)}
        self.assertEqual(checks["b-ref2"].verdict, ReferenceVerdict.MATCH)
        self.assertEqual(checks["b-ref2"].observed, 623)


if __name__ == "__main__":
    unittest.main()
