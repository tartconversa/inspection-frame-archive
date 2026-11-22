"""源交付记录的数据类型与解析辅助。

记录字段类型以 ``source-fields.json`` 为准；业务上区域、矩形在 JSON 里
以字符串毫米给出，统一在入口转成整数，避免比较时混入文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def as_int(value: str | int) -> int:
    return int(value)


def as_int_seq(value: list) -> tuple[int, ...]:
    return tuple(int(item) for item in value)


@dataclass(frozen=True)
class Capture:
    id: str
    item: str
    face: str
    width: int
    height: int
    format: str
    preview: str


@dataclass(frozen=True)
class TileRecord:
    id: str
    capture: str
    x: int
    y: int
    width: int
    height: int
    file: str
    replaces: tuple[str, ...]
    size: int
    sha256: str
    intake: int

    def covers(self, col: int, row: int) -> bool:
        return (
            self.x <= col < self.x + self.width
            and self.y <= row < self.y + self.height
        )


@dataclass(frozen=True)
class PreviewTable:
    id: str
    black: int
    white: int


@dataclass(frozen=True)
class QualityNotice:
    id: str
    issued_for: str
    effect: str  # geometry | pixels | whole
    region: tuple[int, ...]  # 像素区间 x0,y0,x1,y1；空集合表示无区域限定
    cause: str
    intake: int


@dataclass(frozen=True)
class ReadRequest:
    id: str
    item: str
    face: str
    capture: str
    rectangle: tuple[int, int, int, int]
    consumer: str


@dataclass(frozen=True)
class ReferencePixel:
    id: str
    capture: str
    tile: str
    x: int
    y: int
    sample: int
    intake: int


@dataclass(frozen=True)
class PriorClaim:
    id: str
    capture: str
    geometry: str
    region: tuple[int, ...]
    tiles: tuple[str, ...]
    form: str
    recipient: str
    intake: int


@dataclass
class DeliveryBundle:
    """一次到达（同一 intake 序号）内各表的原始记录。"""

    intake: int = 0
    captures: list[Capture] = field(default_factory=list)
    geometries: list = field(default_factory=list)  # Geometry，避免循环导入
    tiles: list[TileRecord] = field(default_factory=list)
    previews: list[PreviewTable] = field(default_factory=list)
    quality: list[QualityNotice] = field(default_factory=list)
    requests: list[ReadRequest] = field(default_factory=list)
    reference_pixels: list[ReferencePixel] = field(default_factory=list)
    prior_claims: list[PriorClaim] = field(default_factory=list)
