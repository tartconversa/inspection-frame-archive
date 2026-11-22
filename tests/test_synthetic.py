"""合成夹具测试：不依赖仓库资产，钉死黑像素语义、独立校验与拍次隔离。

夹具在临时目录中现场生成清单与 t12 字节，用 Archive 的注入能力核验，
保证这些保证来自归档逻辑本身，而不是来自某组固定样本。
"""
import json
import tempfile
import unittest
from pathlib import Path

from filmarchive.archive import Archive, TILE_MISSING, TILE_OK
from filmarchive.coding import pack_t12, sha256_bytes
from filmarchive.query import CellStatus, export_raw_bundle, read_window


def _tile_record(tid, capture, x, y, w, h, data, replaces=None):
    return {
        "id": tid, "capture": capture, "x": x, "y": y, "width": w, "height": h,
        "file": f"{tid}.raw", "replaces": replaces,
        "size": len(data), "sha256": sha256_bytes(data),
    }


class SyntheticFixtureTests(unittest.TestCase):
    def _build_archive(self, tiles_blob: dict[str, bytes], present_files: set[str]):
        # 两个拍次：same-item 的两台相机，网格规格不同，绝不允许互相拼补
        records = [_tile_record(tid, *meta, tiles_blob[tid])
                   for tid, meta in self.tile_meta.items()]
        sources = {
            "captures": [
                {"id": "s1", "item": "item-x", "face": "top", "width": 4,
                 "height": 4, "format": "t12", "preview": "g"},
                {"id": "s2", "item": "item-x", "face": "top", "width": 6,
                 "height": 2, "format": "t12", "preview": "g"},
            ],
            "geometry": [
                {"id": "g1", "capture": "s1", "ox": 0, "oy": 0, "ux": 1,
                 "uy": 0, "vx": 0, "vy": 1, "replaces": None},
            ],
            "tiles": records,
            "preview": [{"id": "g", "black": 0, "white": 4095}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src, assets = tmp / "sources", tmp / "assets"
            src.mkdir()
            (src / "01-captures.json").write_text(json.dumps(
                {"intake": 1, "table": "captures", "records": sources["captures"]}))
            (src / "01-geometry.json").write_text(json.dumps(
                {"intake": 1, "table": "geometry", "records": sources["geometry"]}))
            (src / "01-tiles.json").write_text(json.dumps(
                {"intake": 1, "table": "tiles", "records": records}))
            (src / "01-preview.json").write_text(json.dumps(
                {"intake": 1, "table": "preview", "records": sources["preview"]}))
            injected = {
                f"{tid}.raw": blob for tid, blob in tiles_blob.items()
                if tid in present_files
            }
            return Archive(src, assets, injected_files=injected)

    def setUp(self):
        # s1：2x2 两块，左块含真实黑像素 0；右块整体缺失
        left = [[0, 100], [200, 0]]
        right = [[300, 400], [500, 600]]
        # s2：不同相机、不同网格，几何盒与 s1 像素坐标重叠但属不同拍次
        other = [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12]]
        self.tile_meta = {
            "L": ("s1", 0, 0, 2, 2),
            "R": ("s1", 2, 0, 2, 2),
            "O": ("s2", 0, 0, 6, 2),
        }
        blobs = {
            "L": pack_t12(left), "R": pack_t12(right), "O": pack_t12(other),
        }
        self.archive = self._build_archive(blobs, {"L", "O"})

    def test_real_black_zero_distinct_from_missing(self):
        snap = self.archive.snapshot(1)
        self.assertEqual(snap.tile_states["L"].status, TILE_OK)
        self.assertEqual(snap.tile_states["R"].status, TILE_MISSING)
        result = read_window(snap, "s1", (0, 0, 4, 2), "full")
        values = result.values_grid()
        # 左块真实黑像素以 0 交付、状态 OK
        zeros = [c for c in result.cells if c.value == 0]
        self.assertEqual(len(zeros), 2)
        self.assertTrue(all(c.status is CellStatus.OK for c in zeros))
        # 右块缺口是 None 而不是 0，掩码区分
        self.assertEqual([row[2:] for row in values], [[None, None], [None, None]])
        missing = result.missing_mask()
        self.assertEqual([row[2:] for row in missing], [[True, True], [True, True]])
        self.assertEqual([row[:2] for row in missing], [[False, False], [False, False]])

    def test_raw_bundle_zero_padding_never_pretends_to_be_pixel(self):
        snap = self.archive.snapshot(1)
        result = read_window(snap, "s1", (0, 0, 4, 2), "full")
        bundle = export_raw_bundle(result)
        # 掩码是权威：补零字节位置全部标记缺失
        decoded = self._decode(bundle.blob, 4, 2)
        for r in range(2):
            for c in range(4):
                if bundle.missing_mask[r][c]:
                    self.assertEqual(decoded[r][c], 0)  # 打包补零
                else:
                    self.assertFalse(bundle.missing_mask[r][c])
                    self.assertEqual(decoded[r][c], bundle.values[r][c])

    def test_different_camera_capture_never_fills_sibling_gap(self):
        snap = self.archive.snapshot(1)
        result = read_window(snap, "s1", (0, 0, 4, 2), "full")
        # s2 的 O 块几何盒覆盖 (0,0)..(5,1)，但绝不能用来补 s1 的缺口
        for cell in result.cells:
            if cell.status is CellStatus.MISSING:
                self.assertIsNone(cell.tile_id)
        used = {cell.tile_id for cell in result.cells if cell.tile_id}
        self.assertEqual(used, {"L"})

    @staticmethod
    def _decode(blob, w, h):
        from filmarchive.coding import unpack_t12
        return unpack_t12(blob, w, h)


if __name__ == "__main__":
    unittest.main()
