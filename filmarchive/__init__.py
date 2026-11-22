"""工业相机局部读片归档。

仅使用 Python 标准库。两种源编码：

* ``p8``：PGM P5 八位单通道灰度，固定三行头部，逐行灰度无填充。
* ``t12``：12 位无符号值按行打包，每个值从字节最高位向低位排列，
  每行打包后补零到四字节的整数倍。
"""

from .coding import (
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
from .geometry import Geometry, WindowMap
from .archive import Archive, Snapshot, TileState
from .query import (
    CellStatus,
    ReadResult,
    export_preview_pgm,
    export_raw_bundle,
    read_window,
)
from .claims import ClaimStatus, check_reference_pixels, verify_claim

__all__ = [
    "IntegrityError",
    "build_p8",
    "map_preview",
    "pack_t12",
    "parse_p8",
    "sha256_bytes",
    "t12_blob_size",
    "t12_row_stride",
    "unpack_t12",
    "Geometry",
    "WindowMap",
    "Archive",
    "Snapshot",
    "TileState",
    "CellStatus",
    "ReadResult",
    "export_preview_pgm",
    "export_raw_bundle",
    "read_window",
    "ClaimStatus",
    "check_reference_pixels",
    "verify_claim",
]
