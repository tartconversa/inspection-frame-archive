"""两种源编码的译码、打包与固定预览映射。

约定（见 README 来源约定）：

* p8 是原始 PGM P5，头部固定三行（魔数、宽高、最大值 255），随后逐行
  灰度像素，无行填充。
* t12 按行表示 12 位无符号值，从每个字节的最高位依次向低位排列；
  每行打包成字节后补零到四字节的整数倍。
* 小块完整性以提供方清单给出的字节长度和 SHA-256 校验，范围是整块数据。
"""

from __future__ import annotations

import hashlib
from fractions import Fraction


class IntegrityError(ValueError):
    """字节长度或 SHA-256 与提供方清单不一致。"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def t12_row_stride(width: int) -> int:
    """一行 ``width`` 个 12 位样本打包后所占字节数（含行末补零）。"""
    packed_bits = width * 12
    packed_bytes = (packed_bits + 7) // 8
    return ((packed_bytes + 3) // 4) * 4


def t12_blob_size(width: int, height: int) -> int:
    return t12_row_stride(width) * height


def unpack_t12(data: bytes, width: int, height: int) -> list[list[int]]:
    """把 t12 小块解成 ``height`` 行、每行 ``width`` 个整数的灰度网格。

    不静默忽略长度异常：过短会在读取时触发 :class:`IntegrityError`，
    过长同样报错，保证「校验范围是整块数据」。
    """
    expected = t12_blob_size(width, height)
    if len(data) != expected:
        raise IntegrityError(
            f"t12 小块长度 {len(data)} 与按规格计算的 {expected} 不一致"
        )
    stride = t12_row_stride(width)
    grid: list[list[int]] = []
    for y in range(height):
        chunk = data[y * stride : (y + 1) * stride]
        acc = 0
        row: list[int] = []
        bits_held = 0
        for byte in chunk:
            acc = (acc << 8) | byte
            bits_held += 8
            while bits_held >= 12 and len(row) < width:
                bits_held -= 12
                row.append((acc >> bits_held) & 0xFFF)
        if len(row) != width:
            raise IntegrityError("t12 行内解出的样本数不足")
        grid.append(row)
    return grid


def pack_t12(grid: list[list[int]]) -> bytes:
    """按 t12 约定重新打包灰度网格，是 :func:`unpack_t12` 的逆操作。"""
    height = len(grid)
    width = len(grid[0]) if height else 0
    if any(len(row) != width for row in grid):
        raise ValueError("pack_t12 要求每行等宽")
    if any(not 0 <= value <= 0xFFF for row in grid for value in row):
        raise ValueError("t12 只接受 0..4095 的无符号值")
    out = bytearray()
    for row in grid:
        acc = 0
        bits_held = 0
        line = bytearray()
        for value in row:
            acc = (acc << 12) | value
            bits_held += 12
            while bits_held >= 8:
                bits_held -= 8
                line.append((acc >> bits_held) & 0xFF)
            acc &= (1 << bits_held) - 1
        if bits_held:  # 行末不足一字节的残余高位，低位补零
            line.append((acc << (8 - bits_held)) & 0xFF)
        line.extend(b"\x00" * (t12_row_stride(width) - len(line)))
        out.extend(line)
    return bytes(out)


def parse_p8(data: bytes) -> tuple[int, int, list[list[int]]]:
    """解析固定三行头部的 PGM P5，返回宽、高和灰度网格。"""
    if not data.startswith(b"P5\n"):
        raise IntegrityError("p8 小块必须是 P5 魔数开头的 PGM")
    first_nl = data.index(b"\n")
    second_nl = data.index(b"\n", first_nl + 1)
    third_nl = data.index(b"\n", second_nl + 1)
    header = data[: third_nl + 1]
    dims = data[first_nl + 1 : second_nl].split()
    max_line = data[second_nl + 1 : third_nl]
    if len(dims) != 2 or max_line != b"255":
        raise IntegrityError("p8 头部必须是固定三行且最大值为 255")
    width, height = int(dims[0]), int(dims[1])
    pixels = data[third_nl + 1 :]
    if len(pixels) != width * height:
        raise IntegrityError(
            f"p8 像素字节 {len(pixels)} 与规格 {width}x{height} 不一致"
        )
    grid = [
        list(pixels[y * width : (y + 1) * width]) for y in range(height)
    ]
    return width, height, grid


def build_p8(grid: list[list[int]]) -> bytes:
    """把八位灰度网格重新编码成固定头部的 PGM P5 字节。"""
    height = len(grid)
    width = len(grid[0]) if height else 0
    if any(len(row) != width for row in grid):
        raise ValueError("build_p8 要求每行等宽")
    if any(not 0 <= value <= 255 for row in grid for value in row):
        raise ValueError("p8 只接受 0..255 的值")
    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(value for row in grid for value in row)


def map_preview(value: int, black: int, white: int) -> int:
    """按拍次固定映射生成八位预览值。

    线性映射、截断到 0..255、四舍五入到最近整数，正好一半时向上取整。
    预览只能由原始灰度生成，绝不从预览反推原始值。
    """
    if white <= black:
        raise ValueError("预览映射要求 white > black")
    if value <= black:
        return 0
    if value >= white:
        return 255
    scaled = Fraction(255 * (value - black), white - black)
    # scaled 恒为正，int() 即向下取整；round-half-up = floor(x + 1/2)
    return int(scaled + Fraction(1, 2))
