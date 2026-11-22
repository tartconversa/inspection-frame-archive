"""局部读片：窗口定位、掩码、出处、原始包与兼容预览导出。

同一次读片的原图、预览、坐标全部来自同一个 :class:`~filmarchive.archive.Snapshot`，
切换读法或收到补块都不会让一条复判记录混入不同交付的内容。

单元格状态区分四种不可互相替代的情形：

* ``OK``：原始灰度真实可取（真实黑像素 0 也是 OK，不是缺口）；
* ``VOID``：源像素被厂方质量作废，值不得再作为证据；
* ``MISSING``：小块未到达或核验失败，没有任何源值；
* ``CONFLICT``：两份互相矛盾且无 replacement 合同的重传并存；
* ``OUTSIDE``：矩形在有效合同下落到画面之外。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .archive import Snapshot, TileState
from .coding import (
    build_p8,
    map_preview,
    pack_t12,
    parse_p8,
    sha256_bytes,
    t12_blob_size,
    t12_row_stride,
    unpack_t12,
)
from .geometry import Geometry, WindowMap, select_window


class CellStatus(str, Enum):
    OK = "ok"
    VOID = "void"
    MISSING = "missing"
    CONFLICT = "conflict"
    OUTSIDE = "outside"


@dataclass(frozen=True)
class ByteRange:
    """一处样本在源文件字节流中的可独立核验范围。"""

    file: str
    start: int
    end: int  # 左闭右开
    tile_sha256: str

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "byte_start": self.start,
            "byte_end": self.end,
            "tile_sha256": self.tile_sha256,
        }


@dataclass(frozen=True)
class Cell:
    col: int
    row: int
    x_mm: int
    y_mm: int
    in_frame: bool
    status: CellStatus
    value: int | None
    tile_id: str | None
    note: str | None
    byte_ranges: tuple[ByteRange, ...]

    def as_dict(self) -> dict:
        return {
            "col": self.col,
            "row": self.row,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "in_frame": self.in_frame,
            "status": self.status.value,
            "value": self.value,
            "tile": self.tile_id,
            "note": self.note,
            "byte_ranges": [rng.as_dict() for rng in self.byte_ranges],
        }


@dataclass
class ReadResult:
    request_id: str | None
    capture_id: str
    item: str
    face: str
    geometry_id: str | None
    image_format: str
    snapshot_intake: int
    window: WindowMap | None
    cells: list[Cell] = field(default_factory=list)
    positioning_blockers: tuple = field(default_factory=tuple)  # QualityNotice
    tile_notes: tuple[str, ...] = ()

    # ------------------------------------------------------------- 掩码视图
    def mask(self, status: CellStatus) -> list[list[bool]]:
        """按所取画面行列顺序返回状态掩码（True 表示处于该状态）。"""
        assert self.window is not None
        grid = [[False] * self.window.width for _ in range(self.window.height)]
        for cell in self.cells:
            if cell.status is status:
                grid[cell.row - self.window.row_min][cell.col - self.window.col_min] = True
        return grid

    def missing_mask(self) -> list[list[bool]]:
        """复判用缺失掩码：MISSING/CONFLICT/OUTSIDE 都没有源值，保留为缺口；
        VOID 是另一种状态，由 :meth:`void_mask` 单独表达。"""
        blocked = {CellStatus.MISSING, CellStatus.CONFLICT, CellStatus.OUTSIDE}
        assert self.window is not None
        grid = [[False] * self.window.width for _ in range(self.window.height)]
        for cell in self.cells:
            if cell.status in blocked:
                grid[cell.row - self.window.row_min][cell.col - self.window.col_min] = True
        return grid

    def void_mask(self) -> list[list[bool]]:
        return self.mask(CellStatus.VOID)

    def present_mask(self) -> list[list[bool]]:
        assert self.window is not None
        grid = [[False] * self.window.width for _ in range(self.window.height)]
        for cell in self.cells:
            if cell.status is CellStatus.OK or cell.status is CellStatus.VOID:
                grid[cell.row - self.window.row_min][cell.col - self.window.col_min] = True
        return grid

    def values_grid(self) -> list[list[int | None]]:
        """原始灰度（未拉伸）；缺口与作废格为 None，绝不以预览值回填。"""
        assert self.window is not None
        grid: list[list[int | None]] = [
            [None] * self.window.width for _ in range(self.window.height)
        ]
        for cell in self.cells:
            grid[cell.row - self.window.row_min][cell.col - self.window.col_min] = cell.value
        return grid

    def preview_grid(self) -> list[list[int | None]]:
        """仅对真实可取的源值做固定映射；其他状态保持 None。"""
        preview = self._preview_table()
        grid = self.values_grid()
        return [
            [None if value is None else map_preview(value, preview.black, preview.white)
             for value in row]
            for row in grid
        ]

    def has_gap(self) -> bool:
        """画面内是否存在无法提供完整预览的格（缺片/冲突/越界）。"""
        return any(
            cell.status in {
                CellStatus.MISSING, CellStatus.CONFLICT, CellStatus.OUTSIDE,
            }
            for cell in self.cells
        )

    def has_void(self) -> bool:
        return any(cell.status is CellStatus.VOID for cell in self.cells)

    def gap_cells(self) -> list[Cell]:
        return [
            cell for cell in self.cells
            if cell.status in {
                CellStatus.MISSING, CellStatus.CONFLICT, CellStatus.OUTSIDE,
            }
        ]

    def _preview_table(self):
        capture = self._capture
        table_id = capture.preview
        return self._snapshot.previews[table_id]

    # 由 read_window 注入的内部引用
    _capture: object = None
    _snapshot: object = None


# ---------------------------------------------------------------- 网格缓存
def _tile_grid(snapshot: Snapshot, state: TileState) -> list[list[int]]:
    record = state.record
    data = state.bytes
    assert data is not None
    capture = snapshot.captures[record.capture]
    if capture.format == "p8":
        width, height, grid = parse_p8(data)
        if (width, height) != (record.width, record.height):
            raise ValueError(f"小块 {record.id} 的 PGM 尺寸与清单不符")
        return grid
    if capture.format == "t12":
        if len(data) != t12_blob_size(record.width, record.height):
            raise ValueError(f"小块 {record.id} 字节长度与 t12 规格不符")
        return unpack_t12(data, record.width, record.height)
    raise ValueError(f"不支持的编码 {capture.format}")


def _row_byte_ranges(snapshot: Snapshot, state: TileState, local_row: int) -> tuple[ByteRange, ...]:
    record = state.record
    capture = snapshot.captures[record.capture]
    if capture.format == "p8":
        header = len(b"P5\n") + len(f"{record.width} {record.height}\n255\n".encode("ascii"))
        start = header + local_row * record.width
        return (ByteRange(record.file, start, start + record.width, record.sha256),)
    stride = t12_row_stride(record.width)
    start = local_row * stride
    return (ByteRange(record.file, start, start + stride, record.sha256),)


def read_window(
    snapshot: Snapshot,
    capture_id: str,
    rectangle: tuple[int, int, int, int],
    request_id: str | None = None,
    geometry: Geometry | None = None,
    pinned_geometry: bool = False,
) -> ReadResult:
    """按工件坐标矩形取回真实样本、掩码和出处。

    ``geometry`` 用于已取片记录的复核：传入签发时固定的合同，即使它后来
    被新合同替换，也仍按原坐标出处重建；新请求默认走当前有效合同。
    ``pinned_geometry`` 为真时表示显式复核历史引用，geometry 级作废只
    记录在 ``positioning_blockers`` 中、不拦截重建，由调用方按作废处理。
    """
    capture = snapshot.captures.get(capture_id)
    if capture is None:
        raise KeyError(f"快照中不存在拍次 {capture_id}")
    geometry = geometry or snapshot.geometry_for(capture_id)

    result = ReadResult(
        request_id=request_id,
        capture_id=capture_id,
        item=capture.item,
        face=capture.face,
        geometry_id=geometry.id if geometry else None,
        image_format=capture.format,
        snapshot_intake=snapshot.intake,
        window=None,
    )
    result._capture = capture
    result._snapshot = snapshot

    blockers = snapshot.positioning_notices(capture_id)
    result.positioning_blockers = blockers
    if geometry is None or (blockers and not pinned_geometry):
        # 无有效合同或定位级作废：不能把工件坐标换到成像网格
        result.tile_notes = tuple(
            f"{n.id}: {n.effect} 级作废（{n.cause}），issued_for={n.issued_for}"
            for n in blockers
        ) or ("无有效取向合同，无法定位",)
        return result

    window = select_window(geometry, rectangle, capture.width, capture.height)
    result.window = window

    # 先缓存本拍次所有有效小块网格，出处与值来自同一份字节
    grid_cache: dict[str, list[list[int]]] = {}
    tile_notes: list[str] = []
    for state in snapshot.tile_states.values():
        if state.record.capture != capture_id:
            continue
        if state.verified:
            grid_cache[state.record.id] = _tile_grid(snapshot, state)
        else:
            tile_notes.append(state.revoke_note or state.record.id)
    for wcell in window.cells:
        col, row = wcell.col, wcell.row
        if not wcell.in_frame:
            result.cells.append(
                Cell(col, row, int(wcell.x), int(wcell.y), False,
                     CellStatus.OUTSIDE, None, None,
                     "窗口在有效合同下落到画面之外", ())
            )
            continue

        conflict = snapshot.conflict_for_pixel(capture_id, col, row)
        if conflict is not None:
            result.cells.append(
                Cell(col, row, int(wcell.x), int(wcell.y), True,
                     CellStatus.CONFLICT, None, None,
                     "并存且互相矛盾的来源：" + ",".join(s.record.id for s in conflict),
                     tuple(
                         ByteRange(s.record.file, 0, s.record.size, s.record.sha256)
                         for s in conflict
                     ))
            )
            continue

        covering = snapshot.tiles_covering(capture_id, col, row)
        if not covering:
            note = "没有任何已发布小块覆盖该像素"
            for state in snapshot.tile_states.values():
                if state.record.capture != capture_id or state.verified:
                    continue
                if state.record.covers(col, row):
                    note = state.revoke_note or note
            result.cells.append(
                Cell(col, row, int(wcell.x), int(wcell.y), True,
                     CellStatus.MISSING, None, None, note, ())
            )
            continue

        state = covering[0]
        record = state.record
        local_col, local_row = col - record.x, row - record.y
        value = grid_cache[record.id][local_row][local_col]
        ranges = _row_byte_ranges(snapshot, state, local_row)
        void_notice = snapshot.is_pixel_void(capture_id, col, row)
        if void_notice is not None:
            result.cells.append(
                Cell(col, row, int(wcell.x), int(wcell.y), True,
                     CellStatus.VOID, value, record.id,
                     f"源像素被 {void_notice.id} 作废（{void_notice.cause}）", ranges)
            )
        else:
            result.cells.append(
                Cell(col, row, int(wcell.x), int(wcell.y), True,
                     CellStatus.OK, value, record.id, None, ranges)
            )

    result.tile_notes = tuple(tile_notes)
    return result


# ---------------------------------------------------------------- 导出
@dataclass
class RawBundle:
    """原始窗口导出：未拉伸灰度 + 独立校验所需的全部出处。"""

    capture_id: str
    geometry_id: str
    format: str
    width: int
    height: int
    blob: bytes
    values: list[list[int | None]]
    present_mask: list[list[bool]]
    missing_mask: list[list[bool]]
    void_mask: list[list[bool]]
    tiles: tuple[dict, ...]
    sha256: str

    def manifest(self) -> dict:
        return {
            "capture": self.capture_id,
            "geometry": self.geometry_id,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "blob_sha256": self.sha256,
            "present_mask": self.present_mask,
            "missing_mask": self.missing_mask,
            "void_mask": self.void_mask,
            "tiles": list(self.tiles),
        }


def export_raw_bundle(result: ReadResult) -> RawBundle:
    """按源编码导出窗口原始字节。

    缺口位置在打包字节里补零，但权威状态以随附掩码为准——真实黑像素
    与缺片不会混淆。原始值始终来自源小块，绝不由预览反推。
    """
    if result.window is None:
        raise ValueError(result.tile_notes[0] if result.tile_notes else "窗口不可定位")
    values = result.values_grid()
    width, height = result.window.width, result.window.height
    safe = [[0 if v is None else v for v in row] for row in values]
    if result.image_format == "t12":
        blob = pack_t12(safe)
    elif result.image_format == "p8":
        blob = build_p8(safe)
    else:
        raise ValueError(f"不支持的编码 {result.image_format}")

    seen: dict[str, TileState] = {}
    snapshot = result._snapshot
    for cell in result.cells:
        if cell.tile_id and cell.tile_id not in seen:
            seen[cell.tile_id] = snapshot.tile_states[cell.tile_id]
    tile_manifest = tuple(
        {
            "tile": state.record.id,
            "file": state.record.file,
            "pixel_box": [
                state.record.x, state.record.y,
                state.record.x + state.record.width,
                state.record.y + state.record.height,
            ],
            "size": state.record.size,
            "sha256": state.record.sha256,
        }
        for state in sorted(seen.values(), key=lambda s: s.record.id)
    )
    return RawBundle(
        capture_id=result.capture_id,
        geometry_id=result.geometry_id,
        format=result.image_format,
        width=width,
        height=height,
        blob=blob,
        values=values,
        present_mask=result.present_mask(),
        missing_mask=result.missing_mask(),
        void_mask=result.void_mask(),
        tiles=tile_manifest,
        sha256=sha256_bytes(blob),
    )


@dataclass
class PreviewExport:
    available: bool
    reason: str | None
    gap_cells: list[dict]
    pgm: bytes | None
    width: int | None
    height: int | None
    black: int | None
    white: int | None
    sha256: str | None

    def manifest(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "gap_cells": self.gap_cells,
            "width": self.width,
            "height": self.height,
            "black": self.black,
            "white": self.white,
            "pgm_sha256": self.sha256,
        }


def export_preview_pgm(result: ReadResult) -> PreviewExport:
    """生成现有查阅端可打开的八位 PGM；缺口横跨选区时明确拒绝整幅预览。"""
    if result.window is None:
        return PreviewExport(
            False,
            result.tile_notes[0] if result.tile_notes else "窗口不可定位",
            [], None, None, None, None, None, None,
        )
    gaps = result.gap_cells()
    if gaps:
        return PreviewExport(
            available=False,
            reason="缺口横跨选取区，预览端无法可靠显示缺失状态，拒绝提供完整预览",
            gap_cells=[cell.as_dict() for cell in gaps],
            pgm=None, width=result.window.width, height=result.window.height,
            black=None, white=None, sha256=None,
        )
    preview = result._preview_table()
    grid = result.preview_grid()
    # VOID 格没有源证据，同样不能生成预览像素；按缺口处理并说明作废缘由
    voids = [cell for cell in result.cells if cell.status is CellStatus.VOID]
    if voids:
        return PreviewExport(
            available=False,
            reason="选取区包含质量作废像素，不能作为预览证据",
            gap_cells=[cell.as_dict() for cell in voids],
            pgm=None, width=result.window.width, height=result.window.height,
            black=preview.black, white=preview.white, sha256=None,
        )
    pgm = build_p8(grid)  # 此处所有格均为 OK
    return PreviewExport(
        available=True, reason=None, gap_cells=[],
        pgm=pgm, width=result.window.width, height=result.window.height,
        black=preview.black, white=preview.white, sha256=sha256_bytes(pgm),
    )
