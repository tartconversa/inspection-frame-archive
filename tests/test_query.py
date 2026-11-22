"""局部读片测试：窗口取值、掩码区分、跨块出处与兼容预览导出。"""
import unittest
from pathlib import Path

from filmarchive.archive import Archive
from filmarchive.coding import (
    build_p8, map_preview, pack_t12, sha256_bytes, unpack_t12,
)
from filmarchive.query import (
    CellStatus, export_preview_pgm, export_raw_bundle, read_window,
)

ROOT = Path(__file__).resolve().parents[1]


class QueryTestBase(unittest.TestCase):
    def setUp(self):
        self.archive = Archive(ROOT / "sources", ROOT / "assets")


class ShotAQueryTests(QueryTestBase):
    def test_qa_full_window_p8_identity_preview(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qa"]
        result = read_window(snap, request.capture, request.rectangle, "qa")
        self.assertEqual(result.geometry_id, "ga")
        values = result.values_grid()
        # 与直接从 PGM 切出的窗口逐点一致
        _, _, full = self._decode_a()
        expected = [row[2:6] for row in full[2:5]]
        self.assertEqual(values, expected)
        self.assertEqual(values[0][0], 194)  # 定点 a-ref 落在窗口左上角
        preview = export_preview_pgm(result)
        self.assertTrue(preview.available)
        # grayA 是恒等映射：预览像素等于原始灰度
        self.assertEqual(self._pgm_values(preview.pgm), expected)
        self.assertFalse(result.has_gap())

    @staticmethod
    def _decode_a():
        from filmarchive.coding import parse_p8
        return parse_p8((ROOT / "assets" / "a.pgm").read_bytes())

    @staticmethod
    def _pgm_values(pgm: bytes):
        from filmarchive.coding import parse_p8
        _, _, grid = parse_p8(pgm)
        return grid


class ShotBQueryTests(QueryTestBase):
    def test_qb_intake1_crosses_three_tiles_with_missing_corner(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qb"]
        result = read_window(snap, request.capture, request.rectangle, "qb")
        # 6x6 窗口，右上 4x3 区域因 b3 从未到达而缺片
        self.assertEqual((result.window.width, result.window.height), (6, 6))
        missing = result.missing_mask()
        self.assertTrue(all(missing[r][c] for r in range(2, 6) for c in range(2, 6)))
        self.assertFalse(any(missing[r][c] for r in range(2) for c in range(6)))
        # 缺片格没有值，也没有出处
        gap = result.gap_cells()
        self.assertTrue(all(cell.value is None and cell.byte_ranges == () for cell in gap))
        # 真实黑像素语义：本数据没有 0，但缺口绝不以 0 回填
        self.assertTrue(all(cell.value is None for cell in gap))
        # 出处：覆盖到的三个小块都在 manifest 中
        bundle = export_raw_bundle(result)
        self.assertEqual({t["tile"] for t in bundle.tiles}, {"b0", "b1", "b2"})

    def test_qb_byte_ranges_point_into_correct_tile_rows(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qb"]
        result = read_window(snap, request.capture, request.rectangle, "qb")
        cell = next(c for c in result.cells if c.col == 3 and c.row == 2)
        self.assertEqual(cell.status, CellStatus.OK)
        self.assertEqual(cell.value, 389)  # b-ref 定点
        self.assertEqual(cell.tile_id, "b0")
        rng = cell.byte_ranges[0]
        # b0 内局部行 row=2，t12 步长 8：字节 [16,24)
        self.assertEqual(rng.file, "b0.raw")
        self.assertEqual((rng.start, rng.end), (16, 24))
        self.assertEqual(rng.tile_sha256, snap.tile_states["b0"].record.sha256)

    def test_byte_ranges_slice_back_to_source_rows_for_both_encodings(self):
        from filmarchive.coding import unpack_t12
        from filmarchive.query import _tile_grid
        snap = self.archive.snapshot(1)

        # t12：按出处范围直切源文件，解包应等于小块整行（含行填充处理）
        b_result = read_window(snap, "shot-b", snap.requests["qb"].rectangle, "qb")
        cell = next(c for c in b_result.cells if c.tile_id == "b0")
        rng = cell.byte_ranges[0]
        raw = (ROOT / "assets" / rng.file).read_bytes()[rng.start:rng.end]
        local_row = cell.row - snap.tile_states["b0"].record.y
        self.assertEqual(
            unpack_t12(raw, snap.tile_states["b0"].record.width, 1)[0],
            _tile_grid(snap, snap.tile_states["b0"])[local_row],
        )

        # p8：直切字节应等于 PGM 小块整行
        a_result = read_window(snap, "shot-a", (104, 204, 112, 210), "qa")
        a_state = snap.tile_states["a-all"]
        pgm = (ROOT / "assets" / "a.pgm").read_bytes()
        for row_i in range(a_result.window.height):
            r = a_result.cells[row_i * a_result.window.width].byte_ranges[0]
            source_row = _tile_grid(snap, a_state)[a_result.window.row_min + row_i]
            self.assertEqual(list(pgm[r.start:r.end]), source_row)

    def test_raw_bundle_repacks_identically_and_masks_authoritative(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qb"]
        result = read_window(snap, request.capture, request.rectangle, "qb")
        bundle = export_raw_bundle(result)
        # 重新打包的窗口字节解回后，非缺口位置与读取值逐点相同
        decoded = unpack_t12(bundle.blob, bundle.width, bundle.height)
        for r, row in enumerate(bundle.values):
            for c, value in enumerate(row):
                if value is not None:
                    self.assertEqual(decoded[r][c], value)
                else:
                    # 缺口位置字节补零，但掩码标记为缺失——零不被当成样本
                    self.assertEqual(decoded[r][c], 0)
                    self.assertTrue(bundle.missing_mask[r][c])
        self.assertEqual(sha256_bytes(bundle.blob), bundle.sha256)

    def test_preview_refused_when_gap_crosses_selection(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qc"]
        result = read_window(snap, request.capture, request.rectangle, "qc")
        preview = export_preview_pgm(result)
        self.assertFalse(preview.available)
        self.assertIsNotNone(preview.reason)
        self.assertTrue(preview.gap_cells)  # 实际缺口随结论返回
        # 但能取到的格仍给出固定映射预览值
        grid = result.preview_grid()
        self.assertEqual(grid[0][0], map_preview(353, 128, 3800))

    def test_intake2_conflict_is_exposed_not_silently_chosen(self):
        snap = self.archive.snapshot(2)
        request = snap.requests["qb"]
        result = read_window(snap, request.capture, request.rectangle, "qb")
        conflict_cells = [c for c in result.cells if c.status is CellStatus.CONFLICT]
        self.assertEqual(len(conflict_cells), 8)  # b1 盒 7x4 与窗口交 4x2
        cell = next(c for c in conflict_cells if c.col == 8 and c.row == 3)
        sources = {rng.file for rng in cell.byte_ranges}
        self.assertEqual(sources, {"b1.raw", "b1-retransmit.raw"})
        self.assertIsNone(cell.value)
        preview = export_preview_pgm(result)
        self.assertFalse(preview.available)

    def test_intake3_fixed_tiles_restore_full_window_under_old_geometry(self):
        snap3 = self.archive.snapshot(3)
        # 用 intake1 的 gb 合同重放 qb 区域，b1-fix/b3-fix 后应整窗可交付
        gb = snap3.geometries["gb"]
        result = read_window(
            snap3, "shot-b", snap3.requests["qb"].rectangle, "qb", geometry=gb,
            pinned_geometry=True,
        )
        self.assertTrue(all(c.status is CellStatus.OK for c in result.cells))
        bundle = export_raw_bundle(result)
        self.assertEqual({t["tile"] for t in bundle.tiles},
                         {"b0", "b1-fix", "b2", "b3-fix"})
        preview = export_preview_pgm(result)
        self.assertTrue(preview.available)

    def test_new_request_uses_corrected_geometry_and_reports_outside(self):
        snap3 = self.archive.snapshot(3)
        request = snap3.requests["qb"]
        result = read_window(snap3, request.capture, request.rectangle, "qb")
        self.assertEqual(result.geometry_id, "gb2")
        outside = [c for c in result.cells if c.status is CellStatus.OUTSIDE]
        self.assertEqual(len(outside), 12)
        preview = export_preview_pgm(result)
        self.assertFalse(preview.available)

    def test_qe_legacy_gap_then_restored(self):
        snap2 = self.archive.snapshot(2)
        result2 = read_window(snap2, "shot-b", snap2.requests["qe"].rectangle, "qe")
        self.assertTrue(any(c.status is CellStatus.MISSING for c in result2.cells))
        self.assertFalse(export_preview_pgm(result2).available)
        # intake3 后在 gb2 下窗口越界（合同修正改变了定位），结论随之改变
        snap3 = self.archive.snapshot(3)
        result3 = read_window(snap3, "shot-b", snap3.requests["qe"].rectangle, "qe")
        self.assertEqual(result3.geometry_id, "gb2")
        self.assertTrue(any(c.status is CellStatus.OUTSIDE for c in result3.cells))


class ShotCVoidTests(QueryTestBase):
    def test_pixel_void_keeps_value_but_blocks_evidence(self):
        snap2 = self.archive.snapshot(2)
        request = snap2.requests["qd"]
        result = read_window(snap2, request.capture, request.rectangle, "qd")
        voids = [c for c in result.cells if c.status is CellStatus.VOID]
        self.assertEqual(len(voids), 28)
        # 作废格仍带原始值与出处，只是状态为 void；缺片掩码不包含它
        self.assertTrue(all(c.value is not None and c.tile_id for c in voids))
        self.assertFalse(any(any(row) for row in result.missing_mask()))
        self.assertTrue(any(any(row) for row in result.void_mask()))
        preview = export_preview_pgm(result)
        self.assertFalse(preview.available)
        self.assertIn("作废", preview.reason)

    def test_geometry_void_blocks_new_positioning(self):
        snap3 = self.archive.snapshot(3)
        request = snap3.requests["qd"]
        result = read_window(snap3, request.capture, request.rectangle, "qd")
        self.assertIsNone(result.window)
        self.assertEqual({n.id for n in result.positioning_blockers}, {"qc-geometry"})

    def test_localized_damage_elsewhere_leaves_small_window_deliverable(self):
        # 作废区是像素 (5..11, 4..7)；取一个完全在作废区外的小窗口
        snap2 = self.archive.snapshot(2)
        gc = snap2.geometry_for("shot-c")
        # 工件矩形 [160,212,166,220) 在 gc 下列 0..2、行 0..2，全部位于 c0
        result = read_window(snap2, "shot-c", (160, 212, 166, 220), "local")
        self.assertTrue(all(c.status is CellStatus.OK for c in result.cells))
        bundle = export_raw_bundle(result)
        self.assertEqual([t["tile"] for t in bundle.tiles], ["c0"])
        preview = export_preview_pgm(result)
        self.assertTrue(preview.available)
        # 小窗口自带小块哈希，足以独立校验
        self.assertEqual(bundle.tiles[0]["sha256"],
                         snap2.tile_states["c0"].record.sha256)


class CoherenceTests(QueryTestBase):
    def test_raw_preview_and_coordinates_share_one_snapshot(self):
        snap = self.archive.snapshot(1)
        request = snap.requests["qa"]
        result = read_window(snap, request.capture, request.rectangle, "qa")
        bundle = export_raw_bundle(result)
        preview = export_preview_pgm(result)
        self.assertEqual(bundle.geometry_id, result.geometry_id)
        self.assertEqual(preview.black, snap.previews["grayA"].black)
        # 预览由同一份原始值映射而来
        from filmarchive.coding import parse_p8
        _, _, preview_grid = parse_p8(preview.pgm)
        self.assertEqual(preview_grid, result.values_grid())
        # 模拟「之后收到补块」：已发出的结果对象不受影响
        self.archive.snapshot(3)
        self.assertEqual(result.snapshot_intake, 1)
        self.assertEqual(bundle.sha256, sha256_bytes(bundle.blob))


if __name__ == "__main__":
    unittest.main()
