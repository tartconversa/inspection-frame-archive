"""编码层测试：两种格式的精确对应、行填充、往返与固定预览映射。"""
import unittest
from pathlib import Path

from filmarchive.coding import (
    IntegrityError,
    build_p8,
    map_preview,
    pack_t12,
    parse_p8,
    sha256_bytes,
    t12_blob_size,
    t12_row_stride,
    unpack_t12,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


class T12PackingTests(unittest.TestCase):
    def test_row_stride_pads_to_four_bytes(self):
        # 5 个 12 位 = 60 位 = 8 字节（行内补 4 位），已是 4 的倍数
        self.assertEqual(t12_row_stride(5), 8)
        # 7 个 12 位 = 84 位 = 11 字节，补到 12
        self.assertEqual(t12_row_stride(7), 12)
        self.assertEqual(t12_blob_size(7, 4), 48)
        self.assertEqual(t12_blob_size(5, 4), 32)

    def test_msb_first_layout_matches_known_blob(self):
        # b0 第一行从 300 起步、步长 17；逐字节核对 MSB 优先的位排列
        grid = unpack_t12((ASSETS / "b0.raw").read_bytes(), 5, 4)
        self.assertEqual(grid[0], [300, 317, 334, 351, 368])
        # 手工拼第一行前两个样本：300=0x12C, 317=0x13D
        bits = f"{300:012b}{317:012b}"
        first_two = bytes(int(bits[i:i + 8], 2) for i in (0, 8, 16))
        self.assertEqual((ASSETS / "b0.raw").read_bytes()[:3], first_two)

    def test_roundtrip_preserves_every_value(self):
        grid = [
            [0, 1, 4095, 2048, 7],
            [389, 623, 255, 256, 4094],
        ]
        blob = pack_t12(grid)
        self.assertEqual(len(blob), t12_blob_size(5, 2))
        self.assertEqual(unpack_t12(blob, 5, 2), grid)

    def test_real_black_zero_is_a_value_not_padding(self):
        # 值 0 与行末补零必须能区分：解出来是样本 0，补零位不产生样本
        grid = [[0, 0, 0, 0, 0]]
        blob = pack_t12(grid)
        self.assertEqual(unpack_t12(blob, 5, 1), [[0, 0, 0, 0, 0]])
        self.assertEqual(len(blob), t12_row_stride(5))  # 仍是 8 字节而非 60 位

    def test_truncated_blob_is_rejected(self):
        # b3.raw 实际 46 字节，规格要求 48：截断必须报错而不是补零读
        with self.assertRaises(IntegrityError):
            unpack_t12((ASSETS / "b3.raw").read_bytes(), 7, 4)

    def test_all_asset_t12_tiles_decode_when_complete(self):
        specs = {
            "b0.raw": (5, 4), "b1.raw": (7, 4), "b2.raw": (5, 4),
            "b1-retransmit.raw": (7, 4), "b1-fixed.raw": (7, 4),
            "b3-fixed.raw": (7, 4), "c0.raw": (5, 4), "c1.raw": (7, 4),
            "c2.raw": (5, 4), "c3.raw": (7, 4),
        }
        for name, (w, h) in specs.items():
            with self.subTest(file=name):
                grid = unpack_t12((ASSETS / name).read_bytes(), w, h)
                self.assertEqual(len(grid), h)
                self.assertTrue(all(len(row) == w for row in grid))
                self.assertTrue(all(0 <= v <= 4095 for row in grid for v in row))


class P8Tests(unittest.TestCase):
    def test_fixed_three_line_header_and_pixels(self):
        data = (ASSETS / "a.pgm").read_bytes()
        self.assertTrue(data.startswith(b"P5\n8 6\n255\n"))
        width, height, grid = parse_p8(data)
        self.assertEqual((width, height), (8, 6))
        self.assertEqual(grid[2][2], 194)  # 供应方定点 a-ref

    def test_roundtrip_is_byte_identical(self):
        data = (ASSETS / "a.pgm").read_bytes()
        _, _, grid = parse_p8(data)
        self.assertEqual(build_p8(grid), data)
        self.assertEqual(sha256_bytes(build_p8(grid)), sha256_bytes(data))

    def test_wrong_length_rejected(self):
        with self.assertRaises(IntegrityError):
            parse_p8(b"P5\n2 2\n255\n\x00\x00\x00")


class PreviewMappingTests(unittest.TestCase):
    def test_identity_table_for_p8_capture(self):
        # grayA: black=0 white=255，预览值必须与原始八位灰度逐点相同
        for value in (0, 1, 127, 128, 200, 255):
            self.assertEqual(map_preview(value, 0, 255), value)

    def test_grayB_endpoints_clamp(self):
        black, white = 128, 3800
        self.assertEqual(map_preview(0, black, white), 0)
        self.assertEqual(map_preview(128, black, white), 0)
        self.assertEqual(map_preview(3800, black, white), 255)
        self.assertEqual(map_preview(4095, black, white), 255)

    def test_grayB_known_reference_points(self):
        # 389 -> round-half-up(255*261/3672)=18；623 -> 34
        self.assertEqual(map_preview(389, 128, 3800), 18)
        self.assertEqual(map_preview(623, 128, 3800), 34)

    def test_round_half_up_never_banker(self):
        # 构造精确半点：取 black=0、white=510，则缩放式为 255*v/510 = v/2，
        # 奇数 v 恰好落在半点上，必须向上取整（银行家舍入会偶化到偶数）
        self.assertEqual(map_preview(1, 0, 510), 1)   # 0.5 -> 1
        self.assertEqual(map_preview(3, 0, 510), 2)   # 1.5 -> 2
        self.assertEqual(map_preview(5, 0, 510), 3)   # 2.5 -> 3
        self.assertEqual(map_preview(2, 0, 510), 1)   # 整数点不受影响
        # 真实 grayB 上再验证一个普通点没有回归
        self.assertEqual(map_preview(389, 128, 3800), 18)


if __name__ == "__main__":
    unittest.main()
