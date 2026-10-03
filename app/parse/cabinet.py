from __future__ import annotations

from pathlib import Path

import pandas as pd

from .detect import to_float


CABINET_POINTS = {"金力食堂", "金力宿舍", "智能柜"}


def _drop_empty_names(df: pd.DataFrame) -> pd.DataFrame:
    if "商品名称" not in df.columns:
        return df.iloc[0:0].copy()
    name = df["商品名称"].astype(str).str.strip()
    mask = df["商品名称"].notna() & ~name.isin({"", "nan", "None", "合计", "总计"})
    if "门店" in df.columns:
        mask = mask & ~df["门店"].astype(str).str.contains(r"合计", na=False)
    return df.loc[mask].copy()


def parse_cabinet(path: str | Path) -> list[dict]:
    """读取自取柜出库：遍历全部 sheet；按行日期归月；江升=调拨。"""
    path = Path(path)
    xl = pd.ExcelFile(path)
    rows: list[dict] = []
    for sheet in xl.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet, header=0)
        df = _drop_empty_names(df)
        if df.empty:
            continue
        sheet_point = sheet.strip() if sheet.strip() in CABINET_POINTS else ""
        for _, r in df.iterrows():
            name = str(r.get("商品名称") or "").strip()
            if not name:
                continue
            point_raw = r.get("单头备注")
            if point_raw is None or (isinstance(point_raw, float) and pd.isna(point_raw)):
                point = ""
            else:
                point = str(point_raw).strip()
                if point.lower() == "nan":
                    point = ""
            if not point:
                point = sheet_point or "其他"
            is_transfer = 1 if point == "江升" else 0
            date_val = r.get("日期")
            if pd.isna(date_val) if not isinstance(date_val, str) else not str(date_val).strip():
                date_s = ""
                ym = None
            else:
                ts = pd.to_datetime(date_val, errors="coerce")
                if pd.isna(ts):
                    date_s = str(date_val)[:10]
                    ym = None
                else:
                    date_s = ts.strftime("%Y-%m-%d")
                    ym = ts.strftime("%Y-%m")
            rows.append(
                {
                    "name": name,
                    "category": str(r.get("商品分类") or "").strip() or "其他",
                    "qty": to_float(r.get("出库数量")),
                    "retail_amt": to_float(r.get("零售金额")),
                    "cost_amt": to_float(r.get("成本金额")),
                    "point": point,
                    "is_transfer": is_transfer,
                    "date": date_s,
                    "ym": ym,
                }
            )
    return rows
