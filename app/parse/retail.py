from __future__ import annotations

from pathlib import Path

import pandas as pd

from .detect import drop_total_rows, to_float


SHEET = "商品零售汇总"


def parse_retail(path: str | Path) -> list[dict]:
    path = Path(path)
    df = pd.read_excel(path, sheet_name=SHEET, header=0)
    df = drop_total_rows(df)
    rows: list[dict] = []
    for _, r in df.iterrows():
        name = str(r.get("商品名称") or "").strip()
        if not name or name in ("nan", "None"):
            continue
        qty = to_float(r.get("零售数量"))
        if qty == 0:
            qty = to_float(r.get("数量小计"))
        amount = to_float(r.get("零售金额"))
        if amount == 0:
            amount = to_float(r.get("金额小计"))
        cost = to_float(r.get("成本"))
        profit = to_float(r.get("毛利"))
        margin = r.get("毛利率")
        margin_f = None
        if margin is not None and not (isinstance(margin, float) and pd.isna(margin)):
            margin_f = to_float(margin)
            # 有的表毛利率是 0.24 有的是 24
            if abs(margin_f) <= 1.5 and amount:
                margin_f = margin_f * 100
        rows.append(
            {
                "barcode": str(r.get("商品条码") or "").strip(),
                "code": str(r.get("商品编码") or "").strip(),
                "name": name,
                "category": str(r.get("商品分类") or "").strip() or "其他",
                "qty": qty,
                "amount": amount,
                "cost": cost,
                "profit": profit,
                "margin": margin_f,
            }
        )
    return rows
