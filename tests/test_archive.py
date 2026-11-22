"""归档快照测试：核验、替换链、冲突保留与补块中断的版本连续性。"""
import unittest
from pathlib import Path

from filmarchive.archive import Archive, TILE_CORRUPT, TILE_MISSING, TILE_OK

ROOT = Path(__file__).resolve().parents[1]


class ArchiveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.archive = Archive(ROOT / "sources", ROOT / "assets")

    def test_manifest_verification_detects_truncation(self):
        snap2 = self.archive.snapshot(2)
        state = snap2.tile_states["b3"]
        self.assertEqual(state.status, TILE_CORRUPT)
        self.assertEqual(state.observed_size, 46)
        self.assertNotIn("b3", snap2.active_tiles)
        # 损坏不等于缺片：两种状态分开记录
        self.assertNotEqual(state.status, TILE_MISSING)

    def test_conflicting_retransmits_are_both_kept(self):
        snap2 = self.archive.snapshot(2)
        self.assertIn("b1", snap2.active_tiles)
        self.assertIn("b1-copy", snap2.active_tiles)
        conflict = snap2.conflict_for_pixel("shot-b", 8, 3)
        self.assertIsNotNone(conflict)
        self.assertEqual({s.record.id for s in conflict}, {"b1", "b1-copy"})
        # 冲突来源内容确实不同（623 vs 627）
        from filmarchive.query import _tile_grid
        grids = {s.record.id: _tile_grid(snap2, s) for s in conflict}
        self.assertEqual(grids["b1"][3][3], 623)
        self.assertEqual(grids["b1-copy"][3][3], 627)

    def test_explicit_replacement_resolves_conflict(self):
        snap3 = self.archive.snapshot(3)
        self.assertEqual(snap3.conflicts, {})
        self.assertNotIn("b1", snap3.active_tiles)
        self.assertNotIn("b1-copy", snap3.active_tiles)
        self.assertIn("b1-fix", snap3.active_tiles)
        # b1-fix 与 b1 字节相同，sha256 一致，替换后来源唯一
        self.assertEqual(
            snap3.tile_states["b1-fix"].record.sha256,
            snap3.tile_states["b1"].record.sha256,
        )

    def test_failed_replacement_does_not_revoke_published_version(self):
        # b3 在 intake2 损坏：登记在册但不产生替换效力，旧覆盖继续按缺片暴露
        snap2 = self.archive.snapshot(2)
        self.assertFalse(snap2.tile_states["b3"].verified)
        # intake3 的 b3-fix 核验通过才替换 b3
        snap3 = self.archive.snapshot(3)
        self.assertIn("b3-fix", snap3.active_tiles)
        self.assertNotIn("b3", snap3.active_tiles)

    def test_snapshots_are_immutable_across_later_intakes(self):
        snap1 = self.archive.snapshot(1)
        snap3 = self.archive.snapshot(3)
        # 旧快照里没有后来才登记的小块与合同
        self.assertNotIn("b3", snap1.tile_states)
        self.assertNotIn("gb2", snap1.geometries)
        self.assertIn("b3-fix", snap3.tile_states)
        self.assertEqual(snap1.effective_geometry["shot-b"].id, "gb")
        self.assertEqual(snap3.effective_geometry["shot-b"].id, "gb2")

    def test_effective_geometry_follows_replaces_chain(self):
        snap3 = self.archive.snapshot(3)
        gb2 = snap3.geometries["gb2"]
        self.assertEqual(gb2.replaces, "gb")
        self.assertIs(snap3.effective_geometry["shot-b"], gb2)
        # 历史合同仍可用于复核
        self.assertIn("gb", snap3.geometries)

    def test_quality_notices_partition_by_scope(self):
        snap3 = self.archive.snapshot(3)
        positioning = {n.id for n in snap3.positioning_notices("shot-c")}
        self.assertEqual(positioning, {"qc-geometry"})
        notice = snap3.is_pixel_void("shot-c", 6, 5)
        self.assertEqual(notice.id, "qc-lower")
        self.assertIsNone(snap3.is_pixel_void("shot-c", 0, 0))

    def test_missing_asset_file_is_distinct_status(self):
        # 空资源目录模拟「清单登记但文件未随附」：missing 与 corrupt 分开
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            missing_archive = Archive(ROOT / "sources", empty)
            snap = missing_archive.snapshot(1)
            self.assertEqual(snap.tile_states["b0"].status, TILE_MISSING)
            self.assertNotIn("b0", snap.active_tiles)


if __name__ == "__main__":
    unittest.main()
