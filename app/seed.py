from __future__ import annotations

import json
import re
from pathlib import Path

from .db import STORE_IDS, connect, init_db
from .importer import import_file
from .parse.detect import normalize_month_from_name

ROOT = Path(__file__).resolve().parent.parent
JL_DIR = ROOT / "金力"
JS_DIR = ROOT / "江升"


def _iter_excels(folder: Path):
    if not folder.exists():
        return
    for p in sorted(folder.glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        yield p


def reset_data() -> None:
    """清空业务数据（保留用户表），用于重新种子导入。"""
    init_db()
    with connect() as conn:
        for table in (
            "retail_lines",
            "purchase_lines",
            "cabinet_lines",
            "uploads",
            "months",
            "report_cache",
        ):
            conn.execute(f"DELETE FROM {table}")


def seed_existing(rebuild: bool = True, reset: bool = True) -> list[dict]:
    init_db()
    if reset:
        reset_data()
    results = []
    # 柜文件按月份排序，保证先 8 后 9
    jobs: list[tuple] = []
    for folder, store in ((JL_DIR, "金力"), (JS_DIR, "江升")):
        for path in _iter_excels(folder):
            ym = normalize_month_from_name(path.name)
            jobs.append((ym or "9999-99", folder, store, path))
    jobs.sort(key=lambda x: (x[0], x[3].name))
    for ym, folder, store, path in jobs:
        if ym == "9999-99":
            results.append({"file": path.name, "error": "无法识别月份"})
            continue
        try:
            info = import_file(path, ym=ym, store=store, uploaded_by="seed", replace=True)
            results.append(info)
        except Exception as e:  # noqa: BLE001
            results.append({"file": path.name, "store": store, "error": str(e)})
    if rebuild:
        from .aggregate import save_cache

        save_cache()
    return results


def compare_with_html(html_path: Path | None = None) -> dict:
    """与现有静态 HTML 中的 P.grand / totals 做粗对比。"""
    html_path = html_path or (ROOT / "金力_江升_双店月度对比报告_2026年5-9月.html")
    text = html_path.read_text(encoding="utf-8")
    m = re.search(r"const P = (\{.*?\});\s*\nconst JL", text, re.S)
    if not m:
        return {"error": "未在 HTML 中找到 P"}
    old = json.loads(m.group(1))
    from .aggregate import build_report

    new = build_report()
    diffs = []
    for key in old.get("grand", {}):
        a = float(old["grand"].get(key) or 0)
        b = float(new.get("grand", {}).get(key) or 0)
        if abs(a - b) > 1.0:
            diffs.append({"field": f"grand.{key}", "old": a, "new": b, "delta": round(b - a, 2)})
    for store in ("金力", "江升", "金力自取柜"):
        for key in ("零售金额", "毛利", "进货金额"):
            if key not in old.get("totals", {}).get(store, {}):
                continue
            a = float(old["totals"][store][key])
            b = float(new.get("totals", {}).get(store, {}).get(key) or 0)
            if abs(a - b) > 1.0:
                diffs.append(
                    {
                        "field": f"totals.{store}.{key}",
                        "old": a,
                        "new": b,
                        "delta": round(b - a, 2),
                    }
                )
    return {
        "old_grand": old.get("grand"),
        "new_grand": new.get("grand"),
        "diffs": diffs,
        "ok": len(diffs) == 0,
    }


if __name__ == "__main__":
    print("导入现有 Excel …")
    for r in seed_existing():
        if "error" in r:
            print("  ERR", r)
        else:
            print(f"  OK {r['ym']} {r['store']} {r['kind']} rows={r['rows']}")
    print("对比静态报告 …")
    cmp = compare_with_html()
    if cmp.get("error"):
        print(cmp)
    else:
        print("ok=", cmp["ok"], "diffs=", len(cmp["diffs"]))
        for d in cmp["diffs"][:20]:
            print(" ", d)
