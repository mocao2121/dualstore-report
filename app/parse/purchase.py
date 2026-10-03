from __future__ import annotations

from pathlib import Path

import pandas as pd

from .detect import drop_total_rows, to_float


SHEET = "历史进货查询"


def parse_purchase(path: str | Path) -> list[dict]:
    path = Path(path)
    df = pd.read_excel(path, sheet_name=SHEET, header=0)
    df = drop_total_rows(df)
    rows: list[dict] = []
    for _, r in df.iterrows():
        name = str(r.get("商品名称") or "").strip()
        if not name or name in ("nan", "None"):
            continue
        supplier = str(r.get("供应商") or "").strip().rstrip(",， ")
        biz = str(r.get("业务类型") or "").strip()
        date_val = r.get("日期")
        if pd.isna(date_val):
            date_s = ""
        else:
            date_s = str(date_val)[:10]
        qty = to_float(r.get("进货数量"))
        amount = to_float(r.get("进货金额"))
        # 退货记为负
        if "退货" in biz:
            qty = -abs(qty)
            amount = -abs(amount)
        rows.append(
            {
                "date": date_s,
                "supplier": supplier or "未知供应商",
                "name": name,
                "category": str(r.get("商品分类") or "").strip() or "其他",
                "qty": qty,
                "amount": amount,
                "biz_type": biz,
            }
        )
    return rows
