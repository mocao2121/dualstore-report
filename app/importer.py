from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from .aggregate import save_cache
from .db import UPLOAD_DIR, connect
from .parse import (
    detect_kind,
    detect_store,
    parse_cabinet,
    parse_purchase,
    parse_retail,
)
from .parse.detect import normalize_month_from_name


def ensure_month(ym: str, notes: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO months (ym, status, notes, updated_at)
            VALUES (?, 'draft', ?, ?)
            ON CONFLICT(ym) DO UPDATE SET notes=COALESCE(excluded.notes, months.notes),
              updated_at=excluded.updated_at
            """,
            (ym, notes, datetime.now().isoformat(timespec="seconds")),
        )


def clear_month_kind(ym: str, store: str, kind: str) -> None:
    with connect() as conn:
        if kind == "retail":
            conn.execute("DELETE FROM retail_lines WHERE ym=? AND store=?", (ym, store))
        elif kind == "purchase":
            conn.execute("DELETE FROM purchase_lines WHERE ym=? AND store=?", (ym, store))
        elif kind == "cabinet":
            conn.execute(
                "DELETE FROM cabinet_lines WHERE ym=? AND store=?", (ym, store)
            )
        conn.execute(
            "DELETE FROM uploads WHERE ym=? AND store=? AND kind=?",
            (ym, store, kind),
        )


def insert_lines(ym: str, store: str, kind: str, rows: list[dict]) -> int:
    with connect() as conn:
        if kind == "retail":
            conn.executemany(
                """
                INSERT INTO retail_lines
                (ym, store, barcode, code, name, category, qty, amount, cost, profit, margin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ym,
                        store,
                        r.get("barcode"),
                        r.get("code"),
                        r["name"],
                        r.get("category"),
                        r.get("qty", 0),
                        r.get("amount", 0),
                        r.get("cost", 0),
                        r.get("profit", 0),
                        r.get("margin"),
                    )
                    for r in rows
                ],
            )
        elif kind == "purchase":
            conn.executemany(
                """
                INSERT INTO purchase_lines
                (ym, store, date, supplier, name, category, qty, amount, biz_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ym,
                        store,
                        r.get("date"),
                        r.get("supplier"),
                        r.get("name"),
                        r.get("category"),
                        r.get("qty", 0),
                        r.get("amount", 0),
                        r.get("biz_type"),
                    )
                    for r in rows
                ],
            )
        elif kind == "cabinet":
            conn.executemany(
                """
                INSERT INTO cabinet_lines
                (ym, store, name, category, qty, retail_amt, cost_amt, point, is_transfer, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ym,
                        store,
                        r.get("name"),
                        r.get("category"),
                        r.get("qty", 0),
                        r.get("retail_amt", 0),
                        r.get("cost_amt", 0),
                        r.get("point"),
                        r.get("is_transfer", 0),
                        r.get("date"),
                    )
                    for r in rows
                ],
            )
        else:
            raise ValueError(f"未知类型: {kind}")
    return len(rows)


def import_file(
    path: str | Path,
    ym: str,
    store: str | None = None,
    kind: str | None = None,
    uploaded_by: str | None = None,
    replace: bool = True,
) -> dict:
    path = Path(path)
    xl = pd.ExcelFile(path)
    kind = kind or detect_kind(path.name, xl.sheet_names)
    if not kind:
        raise ValueError(f"无法识别文件类型: {path.name}")

    # 先粗读用于门店识别
    peek = pd.read_excel(path, sheet_name=xl.sheet_names[0], header=0, nrows=30)
    store = store or detect_store(str(path), peek)
    if kind == "cabinet" and not store:
        store = "金力"
    if not store:
        raise ValueError(f"无法识别门店: {path.name}")

    if kind == "retail":
        rows = parse_retail(path)
    elif kind == "purchase":
        rows = parse_purchase(path)
    else:
        rows = parse_cabinet(path)
        if store == "江升":
            for r in rows:
                r["is_transfer"] = 0

    warnings: list[str] = []
    affected: list[str] = []

    dest_dir = UPLOAD_DIR / ym
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{store}_{kind}_{path.name}"
    if Path(path).resolve() != dest.resolve():
        shutil.copy2(path, dest)

    if kind == "cabinet":
        # 业务按「上传所选月份」整月覆盖，不再按出库日期拆月
        # （汇总周期常是上月下旬到本月下旬，文件内日期会跨月）
        date_yms = sorted({r.get("ym") for r in rows if r.get("ym")})
        dates = [r.get("date") for r in rows if r.get("date")]
        for r in rows:
            r["ym"] = ym
        ensure_month(ym)
        if replace:
            clear_month_kind(ym, store, kind)
            # 清掉同文件以前按日期拆到其他月份的残留（旧逻辑 / 旧种子）
            cleaned = _cleanup_stale_cabinet_splits(ym, store, path.name, dates)
            if cleaned:
                warnings.append(
                    "已清理同文件在其他月份的旧拆月残留: " + ", ".join(cleaned)
                )
        n = insert_lines(ym, store, kind, rows)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (ym, store, kind, filename, path, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ym,
                    store,
                    kind,
                    path.name,
                    str(dest),
                    uploaded_by,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        known_points = {"金力食堂", "金力宿舍", "智能柜", "江升", "江升食堂"}
        unknown = {r["point"] for r in rows if r["point"] not in known_points}
        if unknown:
            warnings.append(f"未知柜点: {', '.join(sorted(unknown))}")
        if date_yms and (len(date_yms) > 1 or date_yms != [ym]):
            warnings.append(
                f"文件内出库日期含 {', '.join(date_yms)}，已全部计入所选月份 {ym}"
            )
        affected.append(f"{ym}:{n}")
        status = _refresh_month_status(ym)
    else:
        ensure_month(ym)
        if replace:
            clear_month_kind(ym, store, kind)
        n = insert_lines(ym, store, kind, rows)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (ym, store, kind, filename, path, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ym,
                    store,
                    kind,
                    path.name,
                    str(dest),
                    uploaded_by,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        status = _refresh_month_status(ym)

    save_cache()
    return {
        "ym": ym,
        "store": store,
        "kind": kind,
        "rows": n,
        "filename": path.name,
        "warnings": warnings,
        "status": status,
        "affected": affected,
    }


def _delete_cabinet_dates(ym: str, store: str, dates: list[str]) -> None:
    dates = [d for d in dates if d]
    if not dates:
        return
    with connect() as conn:
        qmarks = ",".join("?" for _ in dates)
        conn.execute(
            f"DELETE FROM cabinet_lines WHERE ym=? AND store=? AND date IN ({qmarks})",
            [ym, store, *dates],
        )


def _cleanup_stale_cabinet_splits(
    ym: str, store: str, filename: str, dates: list[str]
) -> list[str]:
    """同一柜/机文件若曾被旧逻辑拆到其他月份，清掉那些月份的对应残留。"""
    cleaned: list[str] = []
    with connect() as conn:
        others = conn.execute(
            """
            SELECT DISTINCT ym FROM uploads
            WHERE store=? AND kind='cabinet' AND filename=? AND ym<>?
            ORDER BY ym
            """,
            (store, filename, ym),
        ).fetchall()
        other_yms = [row["ym"] for row in others]
    for other_ym in other_yms:
        with connect() as conn:
            file_rows = conn.execute(
                """
                SELECT DISTINCT filename FROM uploads
                WHERE ym=? AND store=? AND kind='cabinet'
                """,
                (other_ym, store),
            ).fetchall()
            filenames = {row["filename"] for row in file_rows}
        if filenames == {filename}:
            # 该月该店柜/机只剩这份旧文件 → 整月清掉
            clear_month_kind(other_ym, store, "cabinet")
        else:
            # 同月还有别的柜/机文件，只按本文件日期尽量清残留
            _delete_cabinet_dates(other_ym, store, dates)
            with connect() as conn:
                conn.execute(
                    """
                    DELETE FROM uploads
                    WHERE ym=? AND store=? AND kind='cabinet' AND filename=?
                    """,
                    (other_ym, store, filename),
                )
        _refresh_month_status(other_ym)
        cleaned.append(other_ym)
    return cleaned


def _refresh_month_status(ym: str) -> str:
    with connect() as conn:
        retail_ok = conn.execute(
            "SELECT COUNT(DISTINCT store) AS c FROM retail_lines WHERE ym=?", (ym,)
        ).fetchone()["c"]
        purchase_ok = conn.execute(
            "SELECT COUNT(DISTINCT store) AS c FROM purchase_lines WHERE ym=?", (ym,)
        ).fetchone()["c"]
        status = "ready" if retail_ok >= 2 and purchase_ok >= 2 else "draft"
        conn.execute(
            "UPDATE months SET status=?, updated_at=? WHERE ym=?",
            (status, datetime.now().isoformat(timespec="seconds"), ym),
        )
    return status


def delete_month(ym: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM retail_lines WHERE ym=?", (ym,))
        conn.execute("DELETE FROM purchase_lines WHERE ym=?", (ym,))
        conn.execute("DELETE FROM cabinet_lines WHERE ym=?", (ym,))
        conn.execute("DELETE FROM uploads WHERE ym=?", (ym,))
        conn.execute("DELETE FROM months WHERE ym=?", (ym,))
    # 清上传目录
    d = UPLOAD_DIR / ym
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    save_cache()


def list_months() -> list[dict]:
    with connect() as conn:
        months = conn.execute(
            "SELECT ym, status, notes, updated_at FROM months ORDER BY ym DESC"
        ).fetchall()
        out = []
        for m in months:
            ups = conn.execute(
                "SELECT store, kind, filename, uploaded_at FROM uploads WHERE ym=? ORDER BY uploaded_at",
                (m["ym"],),
            ).fetchall()
            out.append(
                {
                    "ym": m["ym"],
                    "status": m["status"],
                    "notes": m["notes"],
                    "updated_at": m["updated_at"],
                    "uploads": [dict(u) for u in ups],
                }
            )
        return out


def guess_month(filename: str) -> str | None:
    return normalize_month_from_name(filename)
