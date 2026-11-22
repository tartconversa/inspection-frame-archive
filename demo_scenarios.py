"""端到端情景演示：相同像素在不同编码中的精确对应，以及局部损坏、
坐标更正、窗口跨块如何改变可交付的读片结论。

运行：python demo_scenarios.py
"""
from pathlib import Path

from filmarchive.archive import Archive
from filmarchive.claims import verify_claim
from filmarchive.coding import map_preview, parse_p8, unpack_t12
from filmarchive.query import (
    CellStatus, export_preview_pgm, export_raw_bundle, read_window,
)

ROOT = Path(__file__).resolve().parent
ARCHIVE = Archive(ROOT / "sources", ROOT / "assets")


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_grid(title, grid) -> None:
    print(f"\n{title}")
    for row in grid:
        print("  " + "  ".join("  ." if v is None else f"{v:4d}" for v in row))


def scenario_encoding_equivalence() -> None:
    banner("情景 1：相同像素值在 t12 源编码与八位预览中的精确对应")
    snap = ARCHIVE.snapshot(1)

    # 直接从 t12 源小块译码（未拉伸的 12 位原始灰度）
    b0 = unpack_t12((ROOT / "assets" / "b0.raw").read_bytes(), 5, 4)
    show_grid("b0.raw 解出的 12 位原始灰度", b0)

    # 同一批值经 grayB 固定映射得到八位预览
    preview_rows = [
        [map_preview(v, 128, 3800) for v in row] for row in b0
    ]
    show_grid("按 grayB(black=128, white=3800) 线性映射的八位预览", preview_rows)

    # p8 拍次：原始灰度与预览逐点相同（grayA 恒等映射）
    _, _, a_grid = parse_p8((ROOT / "assets" / "a.pgm").read_bytes())
    result = read_window(snap, "shot-a", (104, 204, 112, 210), "qa")
    show_grid("a.pgm 窗口（p8 原始灰度 == 预览值）", result.values_grid())
    print(f"\n定点 a-ref 像素 (2,2)：源文件直读={a_grid[2][2]}，窗口读回="
          f"{result.values_grid()[0][0]}，预览={result.preview_grid()[0][0]}")
    print("结论：预览只由原始值单向映射，归档从不从预览反推原始灰度。")


def scenario_local_damage() -> None:
    banner("情景 2：局部损坏只影响碰到的小块，独立小窗口仍可交付并自校验")
    for intake in (1, 2, 3):
        snap = ARCHIVE.snapshot(intake)
        result = read_window(snap, "shot-b", snap.requests["qb"].rectangle, "qb")
        counts = {}
        for cell in result.cells:
            counts[cell.status.value] = counts.get(cell.status.value, 0) + 1
        preview = export_preview_pgm(result)
        print(f"intake {intake}: 单元格状态 {counts}")
        print(f"  完整八位预览可提供：{preview.available}"
              + ("" if preview.available else f"（{preview.reason}）"))

    # 完全落在完好小块 c0 内的小窗口：别处损坏不影响它
    snap2 = ARCHIVE.snapshot(2)
    local = read_window(snap2, "shot-c", (160, 212, 166, 220), "local")
    print("\nshot-c 上完全位于 c0 的小窗口（qc-lower 作废区之外）：")
    show_grid("可独立校验的原始窗口", local.values_grid())
    print(f"出处小块与哈希："
          f"{[(t['tile'], t['sha256'][:12] + '…') for t in export_raw_bundle(local).tiles]}")


def scenario_geometry_correction() -> None:
    banner("情景 3：取向合同更正改变新请求的定位，旧引用仍返回原字节")
    qb_rect = ARCHIVE.snapshot(3).requests["qb"].rectangle

    snap1 = ARCHIVE.snapshot(1)
    old = read_window(snap1, "shot-b", qb_rect, "qb")
    snap3 = ARCHIVE.snapshot(3)
    new = read_window(snap3, "shot-b", qb_rect, "qb")
    print(f"gb 合同下窗口：列 {old.window.col_min}..{old.window.col_max}，"
          f"行 {old.window.row_min}..{old.window.row_max}")
    print(f"gb2 合同下窗口：列 {new.window.col_min}..{new.window.col_max}，"
          f"行 {new.window.row_min}..{new.window.row_max}（含画面外格 "
          f"{sum(c.status is CellStatus.OUTSIDE for c in new.cells)} 个）")

    claim = next(c for c in snap3.prior_claims if c.id == "z02")
    report = verify_claim(snap3, claim)
    print(f"\n已签发引用 z02：状态={report.status.value}，仍按 gb 定位，"
          f"重算字节 sha256={report.recomputed_sha256[:16]}…")
    print("新请求按有效合同 gb2 定位；旧引用坐标出处与字节均不变。")


def scenario_cross_tile_window() -> None:
    banner("情景 4：窗口跨小块——出处、行填充与字节范围逐格可核")
    snap = ARCHIVE.snapshot(1)
    result = read_window(snap, "shot-b", snap.requests["qb"].rectangle, "qb")
    show_grid("qb 窗口原始灰度（. 为缺片）", result.values_grid())
    print("\n取样出处（像素 -> 小块/局部行/源文件字节范围）：")
    for cell in result.cells:
        if cell.status is CellStatus.OK and (cell.col, cell.row) in {(3, 2), (8, 3), (4, 5)}:
            rng = cell.byte_ranges[0]
            print(f"  像素({cell.col},{cell.row}) 值={cell.value:4d} "
                  f"tile={cell.tile_id:3s} {rng.file} 字节[{rng.start}:{rng.end}]")
    missing = result.gap_cells()
    print(f"\n缺口 {len(missing)} 格（b3 未到达），与真实黑像素严格区分："
          f"缺口 value=None 且无字节出处，值 0 仍是状态 OK 的真实样本。")


def scenario_quality_void() -> None:
    banner("情景 5：质量作废的三种作用范围与已签发引用的终局结论")
    snap3 = ARCHIVE.snapshot(3)
    for claim in snap3.prior_claims:
        report = verify_claim(snap3, claim)
        reasons = ", ".join(
            f"{q['effect']}({q['affected']})" for q in report.quality_reasons
        ) or "无作废"
        print(f"{claim.id}: {report.status.value:18s} 作废范围：{reasons}")
        for note in report.notes:
            print(f"   - {note}")


def main() -> None:
    scenario_encoding_equivalence()
    scenario_local_damage()
    scenario_geometry_correction()
    scenario_cross_tile_window()
    scenario_quality_void()
    print("\n演示结束。测试：python -m unittest discover -s tests -v")


if __name__ == "__main__":
    main()
