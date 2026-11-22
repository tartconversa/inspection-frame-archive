"""工件坐标与成像网格之间的位置合同。

工件原点 ``(ox, oy)`` 对应画面像素网格的一个角（零起始像素边缘约定）；
``(ux, uy)`` 是相机像素向右一步在工件上的偏移，``(vx, vy)`` 是向下一步
的偏移，单位毫米。像素 ``(col, row)`` 的中心工件坐标为::

    X = ox + col*ux + row*vx
    Y = oy + col*uy + row*vy

取片矩形是工件平面上的左闭右开外框，像素中心落在其中才纳入。
翻转、旋转通过基向量整体表达，调用方不需要单独猜测方向。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Geometry:
    id: str
    capture: str
    ox: int
    oy: int
    ux: int
    uy: int
    vx: int
    vy: int
    replaces: str | None

    @classmethod
    def from_record(cls, record: dict) -> "Geometry":
        return cls(
            id=record["id"],
            capture=record["capture"],
            ox=record["ox"],
            oy=record["oy"],
            ux=record["ux"],
            uy=record["uy"],
            vx=record["vx"],
            vy=record["vy"],
            replaces=record["replaces"],
        )

    def pixel_center(self, col: int, row: int) -> tuple[Fraction, Fraction]:
        """返回像素中心的工件坐标（毫米，精确分数）。"""
        x = Fraction(self.ox) + col * self.ux + row * self.vx
        y = Fraction(self.oy) + col * self.uy + row * self.vy
        return x, y

    def determinant(self) -> int:
        return self.ux * self.vy - self.uy * self.vx


@dataclass(frozen=True)
class WindowCell:
    col: int
    row: int
    x: Fraction  # 像素中心工件坐标
    y: Fraction
    in_frame: bool


@dataclass(frozen=True)
class WindowMap:
    """一个工件矩形对应到的画面行列窗口。

    ``cells`` 按画面行列顺序给出（行优先），包括落在画面外的候选格；
    ``in_frame`` 为真的才是能取片的像素。窗口在画面上的包围盒由
    ``col_min..col_max``、``row_min..row_max``（左闭右开）给出。
    """

    geometry_id: str
    x0: int
    y0: int
    x1: int
    y1: int
    col_min: int
    col_max: int
    row_min: int
    row_max: int
    width: int
    height: int
    cells: tuple[WindowCell, ...]

    @property
    def in_frame_cells(self) -> tuple[WindowCell, ...]:
        return tuple(cell for cell in self.cells if cell.in_frame)

    def grid_index(self, col: int, row: int) -> int:
        """像素坐标到 ``cells`` 行优先下标。"""
        return (row - self.row_min) * self.width + (col - self.col_min)


def select_window(
    geometry: Geometry,
    rectangle: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
) -> WindowMap:
    """把工件矩形 ``(x0, y0, x1, y1)`` 换到成像网格窗口。

    用基向量求逆得到候选行列范围，再逐格以像素中心是否落在左闭右开
    矩形内判定，避免在翻转/旋转取向下弄混边界方向。
    """
    x0, y0, x1, y1 = rectangle
    if not (x0 < x1 and y0 < y1):
        raise ValueError("取片矩形必须是左闭右开且面积为正")
    det = geometry.determinant()
    if det == 0:
        raise ValueError(f"几何合同 {geometry.id} 的基向量退化，无法定位")

    # 逆变换：col = (vy*(X-ox) - vx*(Y-oy)) / det
    #         row = (-uy*(X-ox) + ux*(Y-oy)) / det
    def inverse(x: int, y: int) -> tuple[Fraction, Fraction]:
        dx, dy = x - geometry.ox, y - geometry.oy
        col = Fraction(geometry.vy * dx - geometry.vx * dy, det)
        row = Fraction(-geometry.uy * dx + geometry.ux * dy, det)
        return col, row

    corners = [inverse(x, y) for x in (x0, x1) for y in (y0, y1)]
    col_lo = min(c[0] for c in corners)
    col_hi = max(c[0] for c in corners)
    row_lo = min(c[1] for c in corners)
    row_hi = max(c[1] for c in corners)

    # 中心判定是严格不等式（左闭右开），向两侧各放宽一格保证不漏边界格。
    col_start = int(col_lo) - 1
    col_stop = int(col_hi) + 2
    row_start = int(row_lo) - 1
    row_stop = int(row_hi) + 2

    cells: list[WindowCell] = []
    kept_cols: list[int] = []
    kept_rows: list[int] = []
    for row in range(row_start, row_stop):
        for col in range(col_start, col_stop):
            cx, cy = geometry.pixel_center(col, row)
            inside = Fraction(x0) <= cx < Fraction(x1) and Fraction(y0) <= cy < Fraction(y1)
            if not inside:
                continue
            in_frame = 0 <= col < image_width and 0 <= row < image_height
            cells.append(WindowCell(col, row, cx, cy, in_frame))
            kept_cols.append(col)
            kept_rows.append(row)

    col_min, col_max = min(kept_cols), max(kept_cols) + 1
    row_min, row_max = min(kept_rows), max(kept_rows) + 1
    width, height = col_max - col_min, row_max - row_min
    # cells 已按行优先生成；补齐顺序断言保护
    cells.sort(key=lambda c: (c.row, c.col))
    return WindowMap(
        geometry_id=geometry.id,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        col_min=col_min,
        col_max=col_max,
        row_min=row_min,
        row_max=row_max,
        width=width,
        height=height,
        cells=tuple(cells),
    )
