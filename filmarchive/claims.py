"""已签发取片引用（prior_claims）的核对与供应方参考像素校准。

已签发引用固定三样东西：拍次、取向合同、小块集合。复核时即使新合同
已经生效，也仍按签发时的合同把同一工件区域换回到同一批像素；源小块
字节未被替换时，重算字节必须与签发时完全一致。质量作废则按
geometry / pixels / whole 的作用范围说明缘由并停止作为复判证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .archive import Snapshot
from .coding import build_p8, pack_t12, sha256_bytes
from .model import PriorClaim, ReferencePixel
from .query import CellStatus, ReadResult, export_raw_bundle, read_window


class ClaimStatus(str, Enum):
    REPLAYABLE = "replayable"      # 可重放：同字节、同坐标出处
    SUPERSEDED = "superseded"      # 合同已被替换，但旧依据未作废，仍可重放
    VOID = "void"                  # 质量作废，停止作为复判证据
    UNRECONSTRUCTABLE = "unreconstructable"  # 固定小块已失效且无等价来源


class ReferenceVerdict(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    VOID = "void"


@dataclass
class ReferenceCheck:
    reference: ReferencePixel
    verdict: ReferenceVerdict
    expected: int
    observed: int | None
    note: str | None
    tile_status: str | None

    def as_dict(self) -> dict:
        return {
            "id": self.reference.id,
            "capture": self.reference.capture,
            "tile": self.reference.tile,
            "x": self.reference.x,
            "y": self.reference.y,
            "verdict": self.verdict.value,
            "expected": self.expected,
            "observed": self.observed,
            "note": self.note,
            "tile_status": self.tile_status,
        }


def check_reference_pixels(snapshot: Snapshot) -> list[ReferenceCheck]:
    """用供应方独立给出的定点样值核验译码链路。"""
    checks: list[ReferenceCheck] = []
    for ref in snapshot.reference_pixels:
        state = snapshot.tile_states.get(ref.tile)
        if state is None or not state.verified:
            checks.append(ReferenceCheck(
                ref, ReferenceVerdict.UNAVAILABLE, ref.sample, None,
                None if state is None else state.revoke_note,
                None if state is None else state.status,
            ))
            continue
        # 冲突位置有两份有效来源，单点样值无法判定哪一份是正本
        conflict = snapshot.conflict_for_pixel(ref.capture, ref.x, ref.y)
        if conflict is not None and any(s.record.id == ref.tile for s in conflict):
            checks.append(ReferenceCheck(
                ref, ReferenceVerdict.CONFLICT, ref.sample, None,
                "矛盾重传并存且无 replacement 合同："
                + ",".join(s.record.id for s in conflict),
                state.status,
            ))
            continue
        void_notice = snapshot.is_pixel_void(ref.capture, ref.x, ref.y)
        if void_notice is not None:
            checks.append(ReferenceCheck(
                ref, ReferenceVerdict.VOID, ref.sample, None,
                f"被 {void_notice.id} 作废（{void_notice.cause}）", state.status,
            ))
            continue
        grid_cache = {ref.tile: _grid_of(snapshot, state)}
        record = state.record
        local_col, local_row = ref.x - record.x, ref.y - record.y
        observed = grid_cache[ref.tile][local_row][local_col]
        checks.append(ReferenceCheck(
            ref,
            ReferenceVerdict.MATCH if observed == ref.sample else ReferenceVerdict.MISMATCH,
            ref.sample, observed,
            None if observed == ref.sample else "译码样值与供应方定点不一致",
            state.status,
        ))
    return checks


def _grid_of(snapshot: Snapshot, state):
    from .query import _tile_grid
    return _tile_grid(snapshot, state)


@dataclass
class ClaimVerification:
    claim: PriorClaim
    status: ClaimStatus
    geometry_id: str | None
    geometry_superseded: bool
    quality_reasons: list[dict] = field(default_factory=list)
    read: ReadResult | None = None
    recomputed_sha256: str | None = None
    form: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "claim": self.claim.id,
            "capture": self.claim.capture,
            "status": self.status.value,
            "geometry": self.geometry_id,
            "geometry_superseded": self.geometry_superseded,
            "quality_reasons": self.quality_reasons,
            "recomputed_sha256": self.recomputed_sha256,
            "form": self.form,
            "notes": self.notes,
        }

    def _finalize(self, claim: PriorClaim, geometry, current_geometry) -> None:
        """质量作废优先于一切；其次是合同推进；不可重建保持原状态。"""
        if self.quality_reasons:
            # 作废是终局结论：重建阶段的缺口降级为附注，不再决定引用状态
            reconstruction_notes = list(dict.fromkeys(
                note for note in self.notes
                if "固定小块" in note or "画面外" in note or "不可重放" in note
            ))
            self.notes = [
                note for note in self.notes if note not in reconstruction_notes
            ]
            self.status = ClaimStatus.VOID
            effects = {reason["effect"] for reason in self.quality_reasons}
            if "whole" in effects:
                self.notes.append("整幅图作废，全部依据停止作为复判证据")
            if "geometry" in effects:
                self.notes.append("坐标定位作废：源像素字节仍可留存，但坐标出处失效")
            if "pixels" in effects:
                self.notes.append("区域内源像素作废，受影响格以 void 掩码标记")
            if reconstruction_notes:
                self.notes.append(
                    "另：即使不作废，该引用也无法仅凭固定小块重建——"
                    + "；".join(reconstruction_notes)
                )
            return
        if self.geometry_superseded and self.status is ClaimStatus.REPLAYABLE:
            self.status = ClaimStatus.SUPERSEDED
            self.notes.append(
                f"取向合同已由 {geometry.id} 推进到 {current_geometry.id}；"
                "本引用仍按原合同返回相同字节与坐标出处，新请求应使用有效合同"
            )


def verify_claim(snapshot: Snapshot, claim: PriorClaim) -> ClaimVerification:
    """按签发时固定的合同与小块集合复核一条已取片引用。"""
    capture = snapshot.captures.get(claim.capture)
    geometry = snapshot.geometries.get(claim.geometry)
    current_geometry = snapshot.geometry_for(claim.capture)
    superseded = (
        current_geometry is not None
        and geometry is not None
        and current_geometry.id != geometry.id
    )

    verification = ClaimVerification(
        claim=claim,
        status=ClaimStatus.REPLAYABLE,
        geometry_id=geometry.id if geometry else None,
        geometry_superseded=superseded,
    )

    # ------------------------------------------------ 质量作废作用范围
    region = tuple(claim.region)
    pixel_boxes = [
        snapshot.tile_states[t].record
        for t in claim.tiles if t in snapshot.tile_states
    ]
    for notice in snapshot.quality:
        if not _notice_applies(notice, claim, geometry, pixel_boxes):
            continue
        affected = _affected_scope(notice, region, geometry, pixel_boxes,
                                   capture.width if capture else 0,
                                   capture.height if capture else 0)
        verification.quality_reasons.append({
            "notice": notice.id,
            "effect": notice.effect,
            "cause": notice.cause,
            "issued_for": notice.issued_for,
            "affected": affected,
        })

    if geometry is None:
        verification.status = ClaimStatus.UNRECONSTRUCTABLE
        verification.notes.append("签发所依据的取向合同已不在归档中")
        return verification

    # ------------------------------------------------ 固定小块现状
    for tile_id in claim.tiles:
        state = snapshot.tile_states.get(tile_id)
        if state is None or not state.verified:
            verification.status = ClaimStatus.UNRECONSTRUCTABLE
            verification.notes.append(
                f"固定小块 {tile_id} 不可用："
                + ("未登记" if state is None else (state.revoke_note or "核验未通过"))
            )

    # 固定窗口必须能被签发小块独立覆盖
    read = read_window(
        snapshot, claim.capture, region,
        request_id=claim.id, geometry=geometry, pinned_geometry=True,
    )
    verification.read = read
    if read.window is None:
        verification.status = ClaimStatus.UNRECONSTRUCTABLE
        verification.notes.extend(read.tile_notes)
        verification._finalize(claim, geometry, current_geometry)
        return verification

    pinned = set(claim.tiles)
    for cell in read.cells:
        if not cell.in_frame:
            verification.status = ClaimStatus.UNRECONSTRUCTABLE
            verification.notes.append(
                f"签发区域在原合同下出现画面外像素 ({cell.col},{cell.row})"
            )
        elif cell.tile_id not in pinned:
            verification.status = ClaimStatus.UNRECONSTRUCTABLE
            current = cell.tile_id or "无来源"
            current_state = snapshot.tile_states.get(current)
            if current_state is not None and pinned.intersection(current_state.record.replaces):
                reason = f"固定小块已被 {current} 明确替换，不能以新块字节冒充签发原件"
            else:
                reason = (f"像素落在固定小块集合之外（如 ({cell.col},{cell.row})，"
                          f"现由 {current} 覆盖），不得无声拼入其他小块")
            verification.notes.append(reason)
        elif cell.status in {CellStatus.MISSING, CellStatus.CONFLICT}:
            verification.status = ClaimStatus.UNRECONSTRUCTABLE
            verification.notes.append(cell.note or f"像素 ({cell.col},{cell.row}) 不可重放")
    # 同类附注只保留一条，避免逐像素刷屏
    verification.notes = list(dict.fromkeys(verification.notes))

    # ------------------------------------------------ 重算签发字节
    # 即使质量作废，只要像素仍可重建，就给出重算字节作为出处对照；
    # 状态随后由质量阶段改写，字节不代表证据仍然有效。
    if verification.status is ClaimStatus.REPLAYABLE:
        bundle = export_raw_bundle(read)
        verification.recomputed_sha256 = bundle.sha256
        verification.form = claim.form
        if claim.form == "pgm":
            expected = build_p8(
                [[0 if v is None else v for v in row] for row in bundle.values]
            )
            if sha256_bytes(expected) != bundle.sha256:
                verification.notes.append("PGM 重算与原始包不一致")
        elif claim.form == "raw":
            expected = pack_t12(
                [[0 if v is None else v for v in row] for row in bundle.values]
            )
            if sha256_bytes(expected) != bundle.sha256:
                verification.notes.append("RAW 重算与原始包不一致")
        else:
            verification.notes.append(f"未知签发形式 {claim.form}")

    verification._finalize(claim, geometry, current_geometry)
    return verification


def _notice_applies(notice, claim: PriorClaim, geometry, pixel_boxes) -> bool:
    if notice.issued_for == claim.capture:
        return True
    if geometry is not None and notice.issued_for == geometry.id:
        return True
    return False


def _affected_scope(notice, region, geometry, pixel_boxes, width, height) -> str:
    """说清作废影响的是坐标定位、源像素还是整幅图。"""
    if notice.effect == "whole":
        return "whole"
    if notice.effect == "geometry":
        return "geometry"
    # pixels：对照明区域像素区间与签发小块/窗口的交集
    if not notice.region:
        return "pixels"
    x0, y0, x1, y1 = notice.region
    intersects = any(
        not (x1 <= b.x or b.x + b.width <= x0 or y1 <= b.y or b.y + b.height <= y0)
        for b in pixel_boxes
    )
    return "pixels-in-region" if intersects else "pixels-elsewhere"
