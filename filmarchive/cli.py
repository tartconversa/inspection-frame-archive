"""本地读片归档的命令行入口（仅标准库）。

用法示例::

    python -m filmarchive inventory
    python -m filmarchive snapshot --intake 2
    python -m filmarchive request --id qb
    python -m filmarchive request --id qd --intake 3
    python -m filmarchive verify-claims
    python -m filmarchive reference-check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .archive import Archive
from .claims import check_reference_pixels, verify_claim
from .query import export_preview_pgm, export_raw_bundle, read_window

ROOT = Path(__file__).resolve().parents[1]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _cell_dump(cell):
    data = cell.as_dict()
    data["x_mm"] = int(cell.x_mm)
    data["y_mm"] = int(cell.y_mm)
    return data


def cmd_inventory(archive: Archive, args) -> None:
    rows = []
    for bundle in archive.bundles:
        rows.append({
            "intake": bundle.intake,
            "captures": len(bundle.captures),
            "geometries": len(bundle.geometries),
            "tiles": len(bundle.tiles),
            "previews": len(bundle.previews),
            "quality": len(bundle.quality),
            "requests": len(bundle.requests),
            "reference_pixels": len(bundle.reference_pixels),
            "prior_claims": len(bundle.prior_claims),
        })
    print(_json(rows))


def cmd_snapshot(archive: Archive, args) -> None:
    snap = archive.snapshot(args.intake)
    print(_json({
        "intake": snap.intake,
        "captures": sorted(snap.captures),
        "effective_geometry": {
            capture: geometry.id for capture, geometry in snap.effective_geometry.items()
        },
        "tiles": [
            {
                "tile": state.record.id,
                "capture": state.record.capture,
                "file": state.record.file,
                "status": state.status,
                "replaces": list(state.record.replaces),
                "sha256": state.record.sha256,
                "note": state.revoke_note,
            }
            for state in sorted(snap.tile_states.values(), key=lambda s: s.record.id)
        ],
        "active_tiles": sorted(snap.active_tiles),
        "conflicts": [
            {"pixel_box": list(box), "sources": [s.record.id for s in states]}
            for box, states in sorted(snap.conflicts.items())
        ],
        "quality": [
            {
                "id": q.id, "issued_for": q.issued_for, "effect": q.effect,
                "region": list(q.region), "cause": q.cause,
            }
            for q in snap.quality
        ],
    }))


def cmd_request(archive: Archive, args) -> None:
    snap = archive.snapshot(args.intake)
    request = snap.requests[args.id]
    result = read_window(snap, request.capture, request.rectangle, request_id=request.id)
    payload = {
        "request": request.id,
        "consumer": request.consumer,
        "capture": request.capture,
        "rectangle_mm": list(request.rectangle),
        "snapshot_intake": snap.intake,
        "geometry": result.geometry_id,
        "positioning_blockers": [
            {"id": n.id, "effect": n.effect, "cause": n.cause}
            for n in result.positioning_blockers
        ],
        "tile_notes": list(result.tile_notes),
    }
    if result.window is not None:
        raw = export_raw_bundle(result)
        preview = export_preview_pgm(result)
        payload.update({
            "window": {
                "col_min": result.window.col_min,
                "col_max": result.window.col_max,
                "row_min": result.window.row_min,
                "row_max": result.window.row_max,
                "width": result.window.width,
                "height": result.window.height,
            },
            "values": result.values_grid(),
            "preview_values": result.preview_grid(),
            "missing_mask": raw.missing_mask,
            "void_mask": raw.void_mask,
            "present_mask": raw.present_mask,
            "cells": [_cell_dump(c) for c in result.cells],
            "raw_export": raw.manifest(),
            "preview_export": preview.manifest(),
        })
    print(_json(payload))


def cmd_reference_check(archive: Archive, args) -> None:
    snap = archive.snapshot(args.intake)
    print(_json([check.as_dict() for check in check_reference_pixels(snap)]))


def cmd_verify_claims(archive: Archive, args) -> None:
    snap = archive.snapshot(args.intake)
    reports = []
    for claim in snap.prior_claims:
        verification = verify_claim(snap, claim)
        report = verification.as_dict()
        if verification.read is not None and verification.read.window is not None:
            report["window"] = {
                "geometry": verification.read.geometry_id,
                "col_min": verification.read.window.col_min,
                "col_max": verification.read.window.col_max,
                "row_min": verification.read.window.row_min,
                "row_max": verification.read.window.row_max,
                "tiles": sorted({c.tile_id for c in verification.read.cells if c.tile_id}),
            }
        reports.append(report)
    print(_json(reports))


def cmd_status_summary(archive: Archive, args) -> None:
    for intake in range(1, archive.max_intake + 1):
        snap = archive.snapshot(intake)
        print(f"intake {intake}:")
        for state in sorted(snap.tile_states.values(), key=lambda s: s.record.id):
            print(f"  tile {state.record.id:9s} {state.status:8s} "
                  f"replaces={','.join(state.record.replaces) or '-'}")
        for box, states in sorted(snap.conflicts.items()):
            print(f"  conflict box={box} sources={[s.record.id for s in states]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filmarchive", description="局部读片归档")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources")
    parser.add_argument("--assets", type=Path, default=ROOT / "assets")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inventory").set_defaults(func=cmd_inventory)

    p = sub.add_parser("snapshot", help="查看某到达时点的发布状态")
    p.add_argument("--intake", type=int, default=None)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("request", help="执行一条取片请求")
    p.add_argument("--id", required=True)
    p.add_argument("--intake", type=int, default=None)
    p.set_defaults(func=cmd_request)

    p = sub.add_parser("reference-check", help="供应方定点像素校准")
    p.add_argument("--intake", type=int, default=None)
    p.set_defaults(func=cmd_reference_check)

    p = sub.add_parser("verify-claims", help="核对已签发取片引用")
    p.add_argument("--intake", type=int, default=None)
    p.set_defaults(func=cmd_verify_claims)

    p = sub.add_parser("status", help="逐批次打印小块核验/替换/冲突状态")
    p.set_defaults(func=cmd_status_summary)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    archive = Archive(args.sources, args.assets)
    args.func(archive, args)


if __name__ == "__main__":
    main()
