from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from ..db import STORE_IDS

KIND_PATTERNS = [
    ("retail", re.compile(r"商品零售汇总")),
    ("purchase", re.compile(r"历史进货查询")),
    ("cabinet", re.compile(r"自取柜|其他出库|出库数量|售卖机")),
]

MONTH_PATTERNS = [
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月"),
    re.compile(r"(20\d{2})\s*[-_/]?\s*(\d{1,2})\s*月"),
    re.compile(r"(20\d{2})(\d{2})\s*月?"),
]


def detect_kind(filename: str, sheet_names: list[str] | None = None) -> str | None:
    name = Path(filename).name
    for kind, pat in KIND_PATTERNS:
        if pat.search(name):
            return kind
    if sheet_names:
        joined = " ".join(sheet_names)
        if "商品零售汇总" in joined:
            return "retail"
        if "历史进货查询" in joined:
            return "purchase"
        if "其他出库" in joined or "出库" in joined or "自取柜" in joined or "售卖机" in joined:
            return "cabinet"
    return None


def detect_store(filename: str, df: pd.DataFrame | None = None, folder_hint: str | None = None) -> str | None:
    if folder_hint in ("金力", "江升"):
        return folder_hint
    path_str = str(filename)
    if "金力" in path_str:
        return "金力"
    if "江升" in path_str:
        return "江升"
    if df is not None and "门店" in df.columns:
        for val in df["门店"].dropna().astype(str).head(20):
            key = val.strip()
            if key in STORE_IDS:
                return STORE_IDS[key]
            if "金力" in key:
                return "金力"
            if "江升" in key:
                return "江升"
    return None


def normalize_month_from_name(filename: str) -> str | None:
    name = Path(filename).name
    for pat in MONTH_PATTERNS:
        m = pat.search(name)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
    return None


def to_float(val) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("%", "")
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def drop_total_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(False, index=df.index)
    for col in df.columns:
        series = df[col].astype(str)
        mask = mask | series.str.contains(r"^合计$|^总计$", na=False)
    if "门店" in df.columns:
        mask = mask | df["门店"].astype(str).str.contains(r"合计", na=False)
    return df.loc[~mask].copy()
