"""位置合同测试：像素中心、左闭右开边界、翻转旋转取向与画面外判定。"""
import unittest
from fractions import Fraction

from filmarchive.geometry import Geometry, select_window

# ga：轴对齐，原点 (100,200)，每像素 2mm，像素 (c,r) 中心 (100+2c, 200+2r)
GA = Geometry("ga", "shot-a", 100, 200, 2, 0, 0, 2, None)
# gb：旋转 90°，X=160-2r, Y=220+2c
GB = Geometry("gb", "shot-b", 160, 220, 0, 2, -2, 0, None)
# gc：反向取向，X=160+2r, Y=220-2c
GC = Geometry("gc", "shot-c", 160, 220, 0, -2, 2, 0, None)


class PixelCenterTests(unittest.TestCase):
    def test_axis_aligned_centers(self):
        self.assertEqual(GA.pixel_center(0, 0), (Fraction(100), Fraction(200)))
        self.assertEqual(GA.pixel_center(3, 2), (Fraction(106), Fraction(204)))

    def test_rotated_centers(self):
        self.assertEqual(GB.pixel_center(3, 2), (Fraction(156), Fraction(226)))
        self.assertEqual(GC.pixel_center(5, 0), (Fraction(160), Fraction(210)))


class WindowSelectionTests(unittest.TestCase):
    def test_qa_window_axis_aligned(self):
        # 矩形 [104,204,112,210) -> 列 2..5、行 2..4
        window = select_window(GA, (104, 204, 112, 210), 8, 6)
        self.assertEqual((window.col_min, window.col_max), (2, 6))
        self.assertEqual((window.row_min, window.row_max), (2, 5))
        self.assertEqual(window.width * window.height, len(window.cells))
        self.assertTrue(all(cell.in_frame for cell in window.cells))
        # 行列顺序：行优先
        self.assertEqual((window.cells[0].col, window.cells[0].row), (2, 2))

    def test_half_open_boundary_excludes_edge_centered_pixels(self):
        # 上界 112 恰好是 col=6 的中心，左闭右开必须排除它
        window = select_window(GA, (104, 204, 112, 210), 8, 6)
        cols = {cell.col for cell in window.cells}
        self.assertNotIn(6, cols)
        self.assertIn(5, cols)

    def test_rotated_geometry_qb_window(self):
        # gb 下 [146,226,158,238) -> 列 3..8、行 2..7（6x6）
        window = select_window(GB, (146, 226, 158, 238), 12, 8)
        self.assertEqual((window.col_min, window.col_max), (3, 9))
        self.assertEqual((window.row_min, window.row_max), (2, 8))

    def test_flipped_geometry_qd_window(self):
        # gc 下 [160,198,176,218) -> 列 2..11、行 0..7（10x8）
        window = select_window(GC, (160, 198, 176, 218), 12, 8)
        self.assertEqual((window.col_min, window.col_max), (2, 12))
        self.assertEqual((window.row_min, window.row_max), (0, 8))

    def test_out_of_frame_cells_are_flagged_not_dropped(self):
        # gb2 修正原点后，旧 qb 区域有一部分落到画面之外
        gb2 = Geometry("gb2", "shot-b", 164, 216, 0, 2, -2, 0, "gb")
        window = select_window(gb2, (146, 226, 158, 238), 12, 8)
        inside = [c for c in window.cells if c.in_frame]
        outside = [c for c in window.cells if not c.in_frame]
        self.assertTrue(inside and outside)
        for cell in outside:
            self.assertFalse(0 <= cell.col < 12 and 0 <= cell.row < 8)

    def test_window_cells_carry_exact_mm_centers(self):
        window = select_window(GA, (104, 204, 112, 210), 8, 6)
        first = window.cells[0]
        self.assertEqual(first.x, Fraction(104))
        self.assertEqual(first.y, Fraction(204))

    def test_degenerate_geometry_rejected(self):
        bad = Geometry("bad", "x", 0, 0, 1, 1, 2, 2, None)
        with self.assertRaises(ValueError):
            select_window(bad, (0, 0, 10, 10), 4, 4)

    def test_non_positive_rectangle_rejected(self):
        with self.assertRaises(ValueError):
            select_window(GA, (104, 204, 104, 210), 8, 6)


if __name__ == "__main__":
    unittest.main()
