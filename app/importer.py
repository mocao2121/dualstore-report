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
            conn.execute("DELETE FROM cabinet_lines WHERE ym=?", (ym,))
        conn.execute(
            "DELETE FROM uploads WHERE ym=? AND store=? AND kind=?",
            (ym, store if kind != "cabinet" else "金力", kind),
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
                (ym, name, category, qty, retail_amt, cost_amt, point, is_transfer, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ym,
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
    if kind == "cabinet":
        store = "金力"
    if not store:
        raise ValueError(f"无法识别门店: {path.name}")

    if kind == "retail":
        rows = parse_retail(path)
    elif kind == "purchase":
        rows = parse_purchase(path)
    else:
        rows = parse_cabinet(path)

    warnings: list[str] = []
    affected: list[str] = []

    dest_dir = UPLOAD_DIR / ym
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{store}_{kind}_{path.name}"
    if Path(path).resolve() != dest.resolve():
        shutil.copy2(path, dest)

    if kind == "cabinet":
        # 按出库日期拆月（9月文件常含上月末出库）
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            row_ym = r.get("ym") or ym
            buckets.setdefault(row_ym, []).append(r)
        if replace:
            # 只整月覆盖「上传所选月份」，避免把其它月柜数据清掉
            clear_month_kind(ym, store, kind)
        n = 0
        for row_ym, chunk in sorted(buckets.items()):
            ensure_month(row_ym)
            if row_ym != ym:
                dates = [r.get("date") for r in chunk if r.get("date")]
                _delete_cabinet_dates(row_ym, dates)
            n += insert_lines(row_ym, store, kind, chunk)
            affected.append(f"{row_ym}:{len(chunk)}")
            _refresh_month_status(row_ym)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO uploads (ym, store, kind, filename, path, uploaded_by, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_ym,
                        store,
                        kind,
                        path.name,
                        str(dest),
                        uploaded_by,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
        unknown = {
            r["point"]
            for r in rows
            if r["point"] not in {"金力食堂", "金力宿舍", "智能柜", "江升"}
        }
        if unknown:
            warnings.append(f"未知柜点: {', '.join(sorted(unknown))}")
        if len(buckets) > 1:
            warnings.append("已按出库日期拆到多个月份: " + ", ".join(affected))
        status = "ready"
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


def _delete_cabinet_dates(ym: str, dates: list[str]) -> None:
    dates = [d for d in dates if d]
    if not dates:
        return
    with connect() as conn:
        qmarks = ",".join("?" for _ in dates)
        conn.execute(
            f"DELETE FROM cabinet_lines WHERE ym=? AND date IN ({qmarks})",
            [ym, *dates],
        )


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
