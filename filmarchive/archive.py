"""归档装载与按到达批次发布的不可变快照。

装载顺序严格按 intake 序号；小块只有在字节长度与 SHA-256 都通过清单
校验后才算「已发布」。replacement 合同未明确前，互相矛盾的重传会同时
保留为有效来源，读片时以冲突状态暴露而不是静默选一份。

每次 :meth:`Archive.snapshot` 得到的是某一到达时点的不可变视图：之后
收到补块或合同修正不会改变已经发出的快照，已取片记录因此能固定在
同一次交付上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .coding import sha256_bytes
from .geometry import Geometry
from .model import (
    Capture,
    DeliveryBundle,
    PreviewTable,
    PriorClaim,
    QualityNotice,
    ReadRequest,
    ReferencePixel,
    TileRecord,
    as_int_seq,
)

# 小块核验状态
TILE_OK = "ok"              # 字节与清单一致，可作为来源
TILE_MISSING = "missing"    # 已登记但文件未随附
TILE_CORRUPT = "corrupt"    # 文件存在但长度或哈希不符（含截断）


@dataclass(frozen=True)
class TileState:
    record: TileRecord
    status: str
    observed_size: int | None
    observed_sha256: str | None
    bytes: bytes | None

    @property
    def verified(self) -> bool:
        return self.status == TILE_OK

    @property
    def revoke_note(self) -> str | None:
        if self.status == TILE_MISSING:
            return f"{self.record.id}: 文件 {self.record.file} 未随附"
        if self.status == TILE_CORRUPT:
            return (
                f"{self.record.id}: 文件 {self.record.file} 核验失败"
                f"（清单 {self.record.size} 字节/"
                f"{self.record.sha256[:12]}…，实际 "
                f"{self.observed_size} 字节/"
                f"{(self.observed_sha256 or '')[:12]}…）"
            )
        return None


@dataclass(frozen=True)
class Snapshot:
    intake: int
    captures: dict[str, Capture]
    previews: dict[str, PreviewTable]
    geometries: dict[str, Geometry]          # 该时点全部已知合同
    effective_geometry: dict[str, Geometry]  # 每个拍次当前有效合同
    tile_states: dict[str, TileState]        # 全部登记小块（含未通过核验）
    active_tiles: dict[str, TileState]       # 未被替换且核验通过
    conflicts: dict[tuple, tuple[TileState, ...]]  # 像素范围盒 -> 并存来源
    quality: tuple[QualityNotice, ...]
    requests: dict[str, ReadRequest]
    reference_pixels: tuple[ReferencePixel, ...]
    prior_claims: tuple[PriorClaim, ...]

    def geometry_for(self, capture_id: str) -> Geometry | None:
        return self.effective_geometry.get(capture_id)

    def tiles_covering(self, capture_id: str, col: int, row: int) -> tuple[TileState, ...]:
        return tuple(
            state
            for state in self.active_tiles.values()
            if state.record.capture == capture_id and state.record.covers(col, row)
        )

    def conflict_for_pixel(
        self, capture_id: str, col: int, row: int
    ) -> tuple[TileState, ...] | None:
        for (x0, y0, x1, y1), states in self.conflicts.items():
            if states[0].record.capture != capture_id:
                continue
            if x0 <= col < x1 and y0 <= row < y1:
                return states
        return None

    def is_pixel_void(self, capture_id: str, col: int, row: int) -> QualityNotice | None:
        for notice in self.quality:
            if notice.effect != "pixels" or notice.issued_for != capture_id:
                continue
            x0, y0, x1, y1 = notice.region
            if x0 <= col < x1 and y0 <= row < y1:
                return notice
        return None

    def positioning_notices(self, capture_id: str) -> tuple[QualityNotice, ...]:
        """作用于该拍次定位能力的作废（geometry/whole 级）。"""
        geometry = self.effective_geometry.get(capture_id)
        targets = {capture_id}
        if geometry is not None:
            targets.add(geometry.id)
        return tuple(
            notice
            for notice in self.quality
            if notice.effect in {"geometry", "whole"} and notice.issued_for in targets
        )

    def notices_targeting(self, target_id: str) -> tuple[QualityNotice, ...]:
        """按 issued_for（采集身份或取向合同 id）直接取作废说明。"""
        return tuple(q for q in self.quality if q.issued_for == target_id)


class Archive:
    def __init__(
        self,
        source_dir: str | Path,
        asset_dir: str | Path | None = None,
        injected_files: dict[str, bytes] | None = None,
    ):
        self.source_dir = Path(source_dir)
        self.asset_dir = Path(asset_dir) if asset_dir else self.source_dir.parent / "assets"
        self.injected_files = dict(injected_files or {})
        self.bundles: list[DeliveryBundle] = []
        self._load()
        self.max_intake = self.bundles[-1].intake if self.bundles else 0

    # ------------------------------------------------------------------ 装载
    def _load(self) -> None:
        paths = sorted(self.source_dir.glob("*.json"))
        deliveries = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        by_intake: dict[int, DeliveryBundle] = {}
        for delivery in deliveries:
            intake = delivery["intake"]
            bundle = by_intake.setdefault(intake, DeliveryBundle(intake=intake))
            table = delivery["table"]
            for raw in delivery["records"]:
                self._ingest(bundle, table, raw, intake)
        self.bundles = [by_intake[key] for key in sorted(by_intake)]

    def _ingest(self, bundle: DeliveryBundle, table: str, raw: dict, intake: int) -> None:
        if table == "captures":
            bundle.captures.append(
                Capture(
                    id=raw["id"], item=raw["item"], face=raw["face"],
                    width=raw["width"], height=raw["height"],
                    format=raw["format"], preview=raw["preview"],
                )
            )
        elif table == "geometry":
            bundle.geometries.append(Geometry.from_record(raw))
        elif table == "tiles":
            bundle.tiles.append(
                TileRecord(
                    id=raw["id"], capture=raw["capture"], x=raw["x"], y=raw["y"],
                    width=raw["width"], height=raw["height"], file=raw["file"],
                    replaces=tuple(raw["replaces"].split(",")) if raw["replaces"] else (),
                    size=raw["size"], sha256=raw["sha256"], intake=intake,
                )
            )
        elif table == "preview":
            bundle.previews.append(
                PreviewTable(id=raw["id"], black=raw["black"], white=raw["white"])
            )
        elif table == "quality":
            bundle.quality.append(
                QualityNotice(
                    id=raw["id"], issued_for=raw["issued_for"], effect=raw["effect"],
                    region=as_int_seq(raw["region"]), cause=raw["cause"], intake=intake,
                )
            )
        elif table == "requests":
            bundle.requests.append(
                ReadRequest(
                    id=raw["id"], item=raw["item"], face=raw["face"],
                    capture=raw["capture"], rectangle=as_int_seq(raw["rectangle"]),
                    consumer=raw["consumer"],
                )
            )
        elif table == "reference_pixels":
            bundle.reference_pixels.append(
                ReferencePixel(
                    id=raw["id"], capture=raw["capture"], tile=raw["tile"],
                    x=raw["x"], y=raw["y"], sample=raw["sample"], intake=intake,
                )
            )
        elif table == "prior_claims":
            bundle.prior_claims.append(
                PriorClaim(
                    id=raw["id"], capture=raw["capture"], geometry=raw["geometry"],
                    region=as_int_seq(raw["region"]), tiles=tuple(raw["tiles"]),
                    form=raw["form"], recipient=raw["recipient"], intake=intake,
                )
            )
        else:
            raise ValueError(f"未知数据表 {table}")

    # ------------------------------------------------------------- 小块核验
    def verify_tile(self, record: TileRecord) -> TileState:
        if record.file in self.injected_files:
            data = self.injected_files[record.file]
        else:
            path = self.asset_dir / record.file
            if not path.is_file():
                return TileState(record, TILE_MISSING, None, None, None)
            data = path.read_bytes()
        observed_size = len(data)
        observed_sha = sha256_bytes(data)
        if observed_size != record.size or observed_sha != record.sha256:
            return TileState(record, TILE_CORRUPT, observed_size, observed_sha, None)
        return TileState(record, TILE_OK, observed_size, observed_sha, data)

    # ------------------------------------------------------------------ 快照
    def snapshot(self, intake: int | None = None) -> Snapshot:
        """返回截至某到达批次（含）的不可变读片视图。"""
        cutoff = self.max_intake if intake is None else intake
        if cutoff < 0:
            raise ValueError("intake 不能为负")

        captures: dict[str, Capture] = {}
        previews: dict[str, PreviewTable] = {}
        geometries: dict[str, Geometry] = {}
        tile_records: list[TileRecord] = []
        quality: list[QualityNotice] = []
        requests: dict[str, ReadRequest] = {}
        reference_pixels: list[ReferencePixel] = []
        prior_claims: list[PriorClaim] = []

        for bundle in self.bundles:
            if bundle.intake > cutoff:
                break
            for record in bundle.captures:
                captures[record.id] = record
            for record in bundle.previews:
                previews[record.id] = record
            for record in bundle.geometries:
                geometries[record.id] = record
            tile_records.extend(bundle.tiles)
            quality.extend(bundle.quality)
            for record in bundle.requests:
                requests[record.id] = record
            reference_pixels.extend(bundle.reference_pixels)
            prior_claims.extend(bundle.prior_claims)

        # 有效几何：沿 replaces 链取每个拍次最新合同
        effective_geometry: dict[str, Geometry] = {}
        for geometry in geometries.values():
            current = effective_geometry.get(geometry.capture)
            if current is None or geometry.replaces == current.id:
                effective_geometry[geometry.capture] = geometry

        # 小块核验 + 替换链；补块未通过核验时不产生替换效力
        states = {record.id: self.verify_tile(record) for record in tile_records}
        revoked: set[str] = set()
        for record in tile_records:
            if record.replaces and states[record.id].verified:
                revoked.update(record.replaces)
        active = {
            tid: state
            for tid, state in states.items()
            if tid not in revoked and state.verified
        }

        # 并存来源冲突：同一拍次、同一像素范围有多个有效小块
        grouped: dict[tuple, list[TileState]] = {}
        for state in active.values():
            record = state.record
            key = (record.capture, record.x, record.y, record.width, record.height)
            grouped.setdefault(key, []).append(state)
        conflict_map = {
            (group[0].record.x, group[0].record.y,
             group[0].record.x + group[0].record.width,
             group[0].record.y + group[0].record.height): tuple(group)
            for group in grouped.values()
            if len(group) > 1
        }

        return Snapshot(
            intake=cutoff,
            captures=captures,
            previews=previews,
            geometries=geometries,
            effective_geometry=effective_geometry,
            tile_states=states,
            active_tiles=active,
            conflicts=conflict_map,
            quality=tuple(quality),
            requests=requests,
            reference_pixels=tuple(reference_pixels),
            prior_claims=tuple(prior_claims),
        )
