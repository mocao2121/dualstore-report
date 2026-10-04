from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from .db import connect

CABINET_POINTS = ["智能柜", "金力宿舍", "金力食堂"]
JS_CABINET_POINTS = ["江升食堂"]
MAIN_CATS = ["饮料", "香烟", "副食", "槟榔", "冰淇淋", "乳饮", "方便面", "百货"]


def _r2(v: float) -> float:
    return round(float(v or 0), 2)


def _r1(v: float) -> float:
    return round(float(v or 0), 1)


def _month_label(ym: str) -> str:
    return f"{int(ym.split('-')[1])}月"


def _sorted_months(conn) -> list[str]:
    rows = conn.execute("SELECT ym FROM months ORDER BY ym").fetchall()
    return [r["ym"] for r in rows]


def _retail_month_store(conn, ym: str, store: str) -> dict:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS amount,
               COALESCE(SUM(qty),0) AS qty,
               COALESCE(SUM(profit),0) AS profit,
               COALESCE(SUM(cost),0) AS cost,
               COUNT(DISTINCT name) AS sku
        FROM retail_lines WHERE ym=? AND store=?
        """,
        (ym, store),
    ).fetchone()
    amount = float(row["amount"])
    profit = float(row["profit"])
    margin = (profit / amount * 100) if amount else 0.0
    return {
        "amount": amount,
        "qty": float(row["qty"]),
        "profit": profit,
        "cost": float(row["cost"]),
        "sku": float(row["sku"]),
        "margin": margin,
    }


def _purchase_month_store(conn, ym: str, store: str) -> dict:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS amount,
               COALESCE(SUM(qty),0) AS qty
        FROM purchase_lines WHERE ym=? AND store=?
        """,
        (ym, store),
    ).fetchone()
    return {"amount": float(row["amount"]), "qty": float(row["qty"])}


def _cabinet_month(
    conn, ym: str, store: str = "金力", transfers: bool = False
) -> dict:
    flag = 1 if transfers else 0
    row = conn.execute(
        """
        SELECT COALESCE(SUM(retail_amt),0) AS amount,
               COALESCE(SUM(qty),0) AS qty,
               COALESCE(SUM(cost_amt),0) AS cost
        FROM cabinet_lines WHERE ym=? AND store=? AND is_transfer=?
        """,
        (ym, store, flag),
    ).fetchone()
    amount = float(row["amount"])
    cost = float(row["cost"])
    profit = amount - cost
    margin = (profit / amount * 100) if amount else 0.0
    return {
        "amount": amount,
        "qty": float(row["qty"]),
        "cost": cost,
        "profit": profit,
        "margin": margin,
    }


def _discover_cabinet_points(conn, store: str, fallback: list[str]) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT point FROM cabinet_lines
        WHERE store=? AND is_transfer=0
          AND point IS NOT NULL AND TRIM(point) != ''
        ORDER BY point
        """,
        (store,),
    ).fetchall()
    pts = [r["point"] for r in rows]
    if not pts:
        return list(fallback)
    # 金力：按固定顺序优先，其余追加
    if fallback:
        ordered = [p for p in fallback if p in pts]
        ordered.extend(p for p in pts if p not in ordered)
        return ordered
    return pts


def _cab_point_month(
    conn, months: list[str], store: str = "金力", points: list[str] | None = None
) -> dict[str, list[float]]:
    points = points if points is not None else CABINET_POINTS
    out = {p: [] for p in points}
    for ym in months:
        for p in points:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(retail_amt),0) AS amount
                FROM cabinet_lines
                WHERE ym=? AND store=? AND is_transfer=0 AND point=?
                """,
                (ym, store, p),
            ).fetchone()
            out[p].append(_r2(row["amount"]))
    return out


def _top_n_from_rows(rows: list[tuple], n: int = 10) -> dict:
    # rows: (name, amount, qty, profit)
    rows = sorted(rows, key=lambda x: x[1], reverse=True)[:n]
    return {
        "names": [r[0] for r in rows],
        "amt": [_r2(r[1]) for r in rows],
        "qty": [_r2(r[2]) for r in rows],
        "profit": [_r2(r[3]) for r in rows],
    }


def _bottom_n_from_rows(rows: list[tuple], n: int = 5) -> dict:
    # rows: (name, amount, qty, profit) — rank by qty ASC
    rows = sorted(rows, key=lambda x: (x[2], x[1]))[:n]
    return {
        "names": [r[0] for r in rows],
        "amt": [_r2(r[1]) for r in rows],
        "qty": [_r2(r[2]) for r in rows],
        "profit": [_r2(r[3]) for r in rows],
    }


def _product_agg(conn, ym: str | None, store: str | None) -> list[tuple]:
    sql = """
        SELECT name,
               SUM(amount) AS amount,
               SUM(qty) AS qty,
               SUM(profit) AS profit
        FROM retail_lines WHERE 1=1
    """
    params: list[Any] = []
    if ym:
        sql += " AND ym=?"
        params.append(ym)
    if store:
        sql += " AND store=?"
        params.append(store)
    sql += " GROUP BY name"
    rows = conn.execute(sql, params).fetchall()
    return [(r["name"], float(r["amount"]), float(r["qty"]), float(r["profit"])) for r in rows]


def _product_agg_cat(conn, ym: str, store: str, category: str) -> list[tuple]:
    rows = conn.execute(
        """
        SELECT name,
               SUM(amount) AS amount,
               SUM(qty) AS qty,
               SUM(profit) AS profit
        FROM retail_lines
        WHERE ym=? AND store=? AND category=?
        GROUP BY name
        """,
        (ym, store, category),
    ).fetchall()
    return [(r["name"], float(r["amount"]), float(r["qty"]), float(r["profit"])) for r in rows]


def _cabinet_product_agg(conn, ym: str | None, store: str = "金力") -> list[tuple]:
    sql = """
        SELECT name,
               SUM(retail_amt) AS amount,
               SUM(qty) AS qty,
               SUM(retail_amt - cost_amt) AS profit
        FROM cabinet_lines WHERE is_transfer=0 AND store=?
    """
    params: list[Any] = [store]
    if ym:
        sql += " AND ym=?"
        params.append(ym)
    sql += " GROUP BY name"
    rows = conn.execute(sql, params).fetchall()
    return [(r["name"], float(r["amount"]), float(r["qty"]), float(r["profit"])) for r in rows]


def _category_agg(conn, store: str) -> dict:
    rows = conn.execute(
        """
        SELECT category,
               SUM(amount) AS amount,
               SUM(profit) AS profit
        FROM retail_lines WHERE store=?
        GROUP BY category
        """,
        (store,),
    ).fetchall()
    items = []
    for r in rows:
        cat = r["category"] or "其他"
        amt = float(r["amount"])
        profit = float(r["profit"])
        margin = (profit / amt * 100) if amt else 0.0
        items.append((cat, amt, margin, profit))
    items.sort(key=lambda x: x[1], reverse=True)
    # cat_pivot 用主品类 + 其他
    pivot_map = {c: 0.0 for c in MAIN_CATS}
    other = 0.0
    for cat, amt, _, _ in items:
        if cat in pivot_map:
            pivot_map[cat] = amt
        else:
            other += amt
    cats = MAIN_CATS + ["其他"]
    vals = [_r2(pivot_map[c]) for c in MAIN_CATS] + [_r2(other)]
    # cat_margin top 10 by amount
    top = items[:10]
    return {
        "pivot": {"cats": cats, "vals": vals},
        "margin": {
            "names": [t[0] for t in top],
            "amt": [_r2(t[1]) for t in top],
            "margin": [_r2(t[2]) for t in top],
            "profit": [_r2(t[3]) for t in top],
        },
        "items": items,
    }


def _cat_month_stack(conn, months: list[str]) -> dict[str, list[float]]:
    stack_cats = ["饮料", "香烟", "副食", "槟榔", "冰淇淋", "其他"]
    out = {c: [] for c in stack_cats}
    for ym in months:
        buckets = {c: 0.0 for c in stack_cats}
        rows = conn.execute(
            """
            SELECT category, SUM(amount) AS amount
            FROM retail_lines WHERE ym=?
            GROUP BY category
            """,
            (ym,),
        ).fetchall()
        for r in rows:
            cat = r["category"] or "其他"
            amt = float(r["amount"])
            if cat in buckets and cat != "其他":
                buckets[cat] += amt
            else:
                buckets["其他"] += amt
        for c in stack_cats:
            out[c].append(_r2(buckets[c]))
    return out


def _suppliers(conn, store: str, n: int = 8) -> dict:
    rows = conn.execute(
        """
        SELECT supplier, SUM(amount) AS amount
        FROM purchase_lines WHERE store=?
        GROUP BY supplier
        ORDER BY amount DESC
        LIMIT ?
        """,
        (store, n),
    ).fetchall()
    return {
        "names": [r["supplier"] for r in rows],
        "amt": [_r2(r["amount"]) for r in rows],
    }


def _common_prod(conn, n: int = 12) -> dict:
    jl = {
        r["name"]: float(r["amount"])
        for r in conn.execute(
            "SELECT name, SUM(amount) AS amount FROM retail_lines WHERE store='金力' GROUP BY name"
        )
    }
    js = {
        r["name"]: float(r["amount"])
        for r in conn.execute(
            "SELECT name, SUM(amount) AS amount FROM retail_lines WHERE store='江升' GROUP BY name"
        )
    }
    common = set(jl) & set(js)
    ranked = sorted(common, key=lambda n: jl[n] + js[n], reverse=True)[:n]
    return {
        "names": ranked,
        "金力": [_r2(jl[n]) for n in ranked],
        "江升": [_r2(js[n]) for n in ranked],
    }


def build_report() -> dict:
    with connect() as conn:
        months = _sorted_months(conn)
        if not months:
            return empty_report()

        labels = [_month_label(m) for m in months]

        jl_m = [_retail_month_store(conn, m, "金力") for m in months]
        js_m = [_retail_month_store(conn, m, "江升") for m in months]
        cab_m = [_cabinet_month(conn, m, "金力", transfers=False) for m in months]
        js_cab_m = [_cabinet_month(conn, m, "江升", transfers=False) for m in months]
        xfer_m = [_cabinet_month(conn, m, "金力", transfers=True) for m in months]
        jl_p = [_purchase_month_store(conn, m, "金力") for m in months]
        js_p = [_purchase_month_store(conn, m, "江升") for m in months]
        js_cab_points = _discover_cabinet_points(conn, "江升", JS_CABINET_POINTS)

        retail_amt = {
            "金力门店": [_r2(x["amount"]) for x in jl_m],
            "金力自取柜": [_r2(x["amount"]) for x in cab_m],
            "金力合计": [_r2(a["amount"] + b["amount"]) for a, b in zip(jl_m, cab_m)],
            "江升": [_r2(x["amount"]) for x in js_m],
            "江升售卖机": [_r2(x["amount"]) for x in js_cab_m],
            "江升合计": [_r2(a["amount"] + b["amount"]) for a, b in zip(js_m, js_cab_m)],
        }
        retail_qty = {
            "金力门店": [_r2(x["qty"]) for x in jl_m],
            "金力自取柜": [_r2(x["qty"]) for x in cab_m],
            "江升": [_r2(x["qty"]) for x in js_m],
            "江升售卖机": [_r2(x["qty"]) for x in js_cab_m],
        }
        gross_profit = {
            "金力门店": [_r2(x["profit"]) for x in jl_m],
            "金力自取柜": [_r2(x["profit"]) for x in cab_m],
            "江升": [_r2(x["profit"]) for x in js_m],
            "江升售卖机": [_r2(x["profit"]) for x in js_cab_m],
        }
        gross_margin = {
            "金力门店": [_r2(x["margin"]) for x in jl_m],
            "金力自取柜": [
                (_r2(x["margin"]) if x["amount"] else None) for x in cab_m
            ],
            "江升": [_r2(x["margin"]) for x in js_m],
            "江升售卖机": [
                (_r2(x["margin"]) if x["amount"] else None) for x in js_cab_m
            ],
        }
        sku = {
            "金力": [_r2(x["sku"]) for x in jl_m],
            "江升": [_r2(x["sku"]) for x in js_m],
        }
        avg_price = {
            "金力": [
                _r2(x["amount"] / x["qty"]) if x["qty"] else 0 for x in jl_m
            ],
            "江升": [
                _r2(x["amount"] / x["qty"]) if x["qty"] else 0 for x in js_m
            ],
        }
        purchase_amt = {
            "金力": [_r2(x["amount"]) for x in jl_p],
            "江升": [_r2(x["amount"]) for x in js_p],
        }

        def mom(series: list[float]) -> list[float | None]:
            out: list[float | None] = [None]
            for i in range(1, len(series)):
                prev = series[i - 1]
                out.append(_r2((series[i] - prev) / prev * 100) if prev else None)
            return out

        mom_retail = {
            "金力门店": mom(retail_amt["金力门店"]),
            "金力合计": mom(retail_amt["金力合计"]),
            "江升": mom(retail_amt["江升"]),
            "江升合计": mom(retail_amt["江升合计"]),
        }
        combined_series = [
            _r2(a + b + c + d)
            for a, b, c, d in zip(
                retail_amt["金力门店"],
                retail_amt["金力自取柜"],
                retail_amt["江升"],
                retail_amt["江升售卖机"],
            )
        ]
        mom_combined = mom(combined_series)

        combined = []
        for i, ym in enumerate(months):
            jl, js, cab, js_cab, xfer = (
                jl_m[i],
                js_m[i],
                cab_m[i],
                js_cab_m[i],
                xfer_m[i],
            )
            store_sum = jl["amount"] + js["amount"]
            all_sum = store_sum + cab["amount"] + js_cab["amount"]
            combined.append(
                {
                    "month": labels[i],
                    "金力门店零售": _r2(jl["amount"]),
                    "金力自取柜零售": _r2(cab["amount"]),
                    "金力合计零售": _r2(jl["amount"] + cab["amount"]),
                    "江升零售": _r2(js["amount"]),
                    "江升售卖机零售": _r2(js_cab["amount"]),
                    "江升合计零售": _r2(js["amount"] + js_cab["amount"]),
                    "两店门店合计": _r2(store_sum),
                    "含柜总合计": _r2(all_sum),
                    "金力门店毛利": _r2(jl["profit"]),
                    "金力自取柜毛利": _r2(cab["profit"]),
                    "金力合计毛利": _r2(jl["profit"] + cab["profit"]),
                    "江升毛利": _r2(js["profit"]),
                    "江升售卖机毛利": _r2(js_cab["profit"]),
                    "合计毛利": _r2(
                        jl["profit"] + cab["profit"] + js["profit"] + js_cab["profit"]
                    ),
                    "金力门店数量": _r2(jl["qty"]),
                    "金力自取柜数量": _r2(cab["qty"]),
                    "江升数量": _r2(js["qty"]),
                    "江升售卖机数量": _r2(js_cab["qty"]),
                    "金力门店成本": _r2(jl["cost"]),
                    "金力自取柜成本": _r2(cab["cost"]),
                    "江升成本": _r2(js["cost"]),
                    "江升售卖机成本": _r2(js_cab["cost"]),
                    "金力SKU": _r2(jl["sku"]),
                    "江升SKU": _r2(js["sku"]),
                    "金力门店毛利率": _r2(jl["margin"]),
                    "金力自取柜毛利率": _r2(cab["margin"]) if cab["amount"] else 0,
                    "江升毛利率": _r2(js["margin"]),
                    "江升售卖机毛利率": _r2(js_cab["margin"]) if js_cab["amount"] else 0,
                    "金力门店占比": _r2(jl["amount"] / store_sum * 100) if store_sum else 0,
                    "金力含柜占比": _r2((jl["amount"] + cab["amount"]) / all_sum * 100)
                    if all_sum
                    else 0,
                    "金力进货": _r2(jl_p[i]["amount"]),
                    "江升进货": _r2(js_p[i]["amount"]),
                    "调拨江升": _r2(xfer["amount"]),
                }
            )

        # totals
        def sum_field(items, key):
            return sum(x[key] for x in items)

        t_jl = {
            "零售金额": _r2(sum_field(jl_m, "amount")),
            "零售数量": _r2(sum_field(jl_m, "qty")),
            "毛利": _r2(sum_field(jl_m, "profit")),
            "成本": _r2(sum_field(jl_m, "cost")),
            "进货金额": _r2(sum_field(jl_p, "amount")),
            "进货数量": _r2(sum_field(jl_p, "qty")),
        }
        t_jl["毛利率"] = _r2(t_jl["毛利"] / t_jl["零售金额"] * 100) if t_jl["零售金额"] else 0
        t_jl["平均月销"] = _r2(t_jl["零售金额"] / len(months)) if months else 0
        t_jl["SKU月均"] = _r2(sum_field(jl_m, "sku") / len(months)) if months else 0

        t_js = {
            "零售金额": _r2(sum_field(js_m, "amount")),
            "零售数量": _r2(sum_field(js_m, "qty")),
            "毛利": _r2(sum_field(js_m, "profit")),
            "成本": _r2(sum_field(js_m, "cost")),
            "进货金额": _r2(sum_field(js_p, "amount")),
            "进货数量": _r2(sum_field(js_p, "qty")),
        }
        t_js["毛利率"] = _r2(t_js["毛利"] / t_js["零售金额"] * 100) if t_js["零售金额"] else 0
        t_js["平均月销"] = _r2(t_js["零售金额"] / len(months)) if months else 0
        t_js["SKU月均"] = _r2(sum_field(js_m, "sku") / len(months)) if months else 0

        t_cab = {
            "零售金额": _r2(sum_field(cab_m, "amount")),
            "零售数量": _r2(sum_field(cab_m, "qty")),
            "毛利": _r2(sum_field(cab_m, "profit")),
            "成本": _r2(sum_field(cab_m, "cost")),
        }
        t_cab["毛利率"] = (
            _r2(t_cab["毛利"] / t_cab["零售金额"] * 100) if t_cab["零售金额"] else 0
        )

        t_js_cab = {
            "零售金额": _r2(sum_field(js_cab_m, "amount")),
            "零售数量": _r2(sum_field(js_cab_m, "qty")),
            "毛利": _r2(sum_field(js_cab_m, "profit")),
            "成本": _r2(sum_field(js_cab_m, "cost")),
        }
        t_js_cab["毛利率"] = (
            _r2(t_js_cab["毛利"] / t_js_cab["零售金额"] * 100)
            if t_js_cab["零售金额"]
            else 0
        )

        t_jl_all = {
            "零售金额": _r2(t_jl["零售金额"] + t_cab["零售金额"]),
            "零售数量": _r2(t_jl["零售数量"] + t_cab["零售数量"]),
            "毛利": _r2(t_jl["毛利"] + t_cab["毛利"]),
            "成本": _r2(t_jl["成本"] + t_cab["成本"]),
        }
        t_jl_all["毛利率"] = (
            _r2(t_jl_all["毛利"] / t_jl_all["零售金额"] * 100) if t_jl_all["零售金额"] else 0
        )

        t_js_all = {
            "零售金额": _r2(t_js["零售金额"] + t_js_cab["零售金额"]),
            "零售数量": _r2(t_js["零售数量"] + t_js_cab["零售数量"]),
            "毛利": _r2(t_js["毛利"] + t_js_cab["毛利"]),
            "成本": _r2(t_js["成本"] + t_js_cab["成本"]),
        }
        t_js_all["毛利率"] = (
            _r2(t_js_all["毛利"] / t_js_all["零售金额"] * 100) if t_js_all["零售金额"] else 0
        )

        t_xfer = {
            "零售金额": _r2(sum_field(xfer_m, "amount")),
            "零售数量": _r2(sum_field(xfer_m, "qty")),
            "成本": _r2(sum_field(xfer_m, "cost")),
        }

        grand = {
            "门店零售金额": _r2(t_jl["零售金额"] + t_js["零售金额"]),
            "自取柜零售金额": t_cab["零售金额"],
            "江升售卖机零售金额": t_js_cab["零售金额"],
            "含柜总零售": _r2(
                t_jl["零售金额"]
                + t_js["零售金额"]
                + t_cab["零售金额"]
                + t_js_cab["零售金额"]
            ),
            "门店毛利": _r2(t_jl["毛利"] + t_js["毛利"]),
            "含柜总毛利": _r2(
                t_jl["毛利"] + t_js["毛利"] + t_cab["毛利"] + t_js_cab["毛利"]
            ),
            "进货金额": _r2(t_jl["进货金额"] + t_js["进货金额"]),
            "门店零售数量": _r2(t_jl["零售数量"] + t_js["零售数量"]),
            "自取柜数量": t_cab["零售数量"],
            "江升售卖机数量": t_js_cab["零售数量"],
            "调拨江升金额": t_xfer["零售金额"],
        }

        # peak / low by 含柜总合计
        peak = max(combined, key=lambda x: x["含柜总合计"])
        low = min(combined, key=lambda x: x["含柜总合计"])

        cat_jl = _category_agg(conn, "金力")
        cat_js = _category_agg(conn, "江升")

        monthly_top = {"金力": {}, "江升": {}}
        monthly_bottom = {"金力": {}, "江升": {}}
        for ym, lab in zip(months, labels):
            monthly_top["金力"][lab] = _top_n_from_rows(_product_agg(conn, ym, "金力"))
            monthly_top["江升"][lab] = _top_n_from_rows(_product_agg(conn, ym, "江升"))
            monthly_bottom["金力"][lab] = {}
            monthly_bottom["江升"][lab] = {}
            for cat in MAIN_CATS:
                monthly_bottom["金力"][lab][cat] = _bottom_n_from_rows(
                    _product_agg_cat(conn, ym, "金力", cat)
                )
                monthly_bottom["江升"][lab][cat] = _bottom_n_from_rows(
                    _product_agg_cat(conn, ym, "江升", cat)
                )

        cab_monthly_top = {}
        js_cab_monthly_top = {}
        for ym, lab in zip(months, labels):
            if cab_m[months.index(ym)]["amount"] > 0:
                cab_monthly_top[lab] = _top_n_from_rows(
                    _cabinet_product_agg(conn, ym, "金力")
                )
            if js_cab_m[months.index(ym)]["amount"] > 0:
                js_cab_monthly_top[lab] = _top_n_from_rows(
                    _cabinet_product_agg(conn, ym, "江升")
                )

        sell_through = []
        for i, lab in enumerate(labels):
            jl_st = (
                _r2(jl_m[i]["amount"] / jl_p[i]["amount"] * 100)
                if jl_p[i]["amount"]
                else 0
            )
            js_st = (
                _r2(js_m[i]["amount"] / js_p[i]["amount"] * 100)
                if js_p[i]["amount"]
                else 0
            )
            sell_through.append({"month": lab, "金力": jl_st, "江升": js_st})

        report = {
            "data": {
                "months": labels,
                "retail_amt": retail_amt,
                "retail_qty": retail_qty,
                "gross_profit": gross_profit,
                "gross_margin": gross_margin,
                "sku": sku,
                "avg_price": avg_price,
                "purchase_amt": purchase_amt,
                "mom_retail": mom_retail,
                "mom_combined": mom_combined,
            },
            "totals": {
                "金力": t_jl,
                "江升": t_js,
                "金力自取柜": t_cab,
                "金力含柜": t_jl_all,
                "江升售卖机": t_js_cab,
                "江升含柜": t_js_all,
                "调拨江升": t_xfer,
            },
            "combined": combined,
            "cat_pivot": {"金力": cat_jl["pivot"], "江升": cat_js["pivot"]},
            "cat_month_stack": _cat_month_stack(conn, months),
            "top_prod": {
                "金力": _top_n_from_rows(_product_agg(conn, None, "金力")),
                "江升": _top_n_from_rows(_product_agg(conn, None, "江升")),
            },
            "monthly_top": monthly_top,
            "monthly_bottom": monthly_bottom,
            "main_cats": list(MAIN_CATS),
            "cab_monthly_top": cab_monthly_top,
            "cab_top": _top_n_from_rows(_cabinet_product_agg(conn, None, "金力")),
            "cab_point_month": _cab_point_month(conn, months, "金力", CABINET_POINTS),
            "js_cab_monthly_top": js_cab_monthly_top,
            "js_cab_top": _top_n_from_rows(_cabinet_product_agg(conn, None, "江升")),
            "js_cab_point_month": _cab_point_month(
                conn, months, "江升", js_cab_points
            ),
            "js_cab_points": js_cab_points,
            "cat_margin": {"金力": cat_jl["margin"], "江升": cat_js["margin"]},
            "suppliers": {
                "金力": _suppliers(conn, "金力"),
                "江升": _suppliers(conn, "江升"),
            },
            "common_prod": _common_prod(conn),
            "sell_through": sell_through,
            "peak": peak,
            "low": low,
            "grand": grand,
            "months": labels,
            "ym_list": months,
            "generated_at": datetime.now().strftime("%Y-%m-%d"),
            "period_label": f"{labels[0]} ~ {labels[-1]}" if labels else "",
        }
        return report


def empty_report() -> dict:
    empty_top = {"names": [], "amt": [], "qty": [], "profit": []}
    return {
        "data": {
            "months": [],
            "retail_amt": {
                "金力门店": [],
                "金力自取柜": [],
                "金力合计": [],
                "江升": [],
                "江升售卖机": [],
                "江升合计": [],
            },
            "retail_qty": {
                "金力门店": [],
                "金力自取柜": [],
                "江升": [],
                "江升售卖机": [],
            },
            "gross_profit": {
                "金力门店": [],
                "金力自取柜": [],
                "江升": [],
                "江升售卖机": [],
            },
            "gross_margin": {
                "金力门店": [],
                "金力自取柜": [],
                "江升": [],
                "江升售卖机": [],
            },
            "sku": {"金力": [], "江升": []},
            "avg_price": {"金力": [], "江升": []},
            "purchase_amt": {"金力": [], "江升": []},
            "mom_retail": {
                "金力门店": [],
                "金力合计": [],
                "江升": [],
                "江升合计": [],
            },
            "mom_combined": [],
        },
        "totals": {
            "金力": {},
            "江升": {},
            "金力自取柜": {},
            "金力含柜": {},
            "江升售卖机": {},
            "江升含柜": {},
            "调拨江升": {},
        },
        "combined": [],
        "cat_pivot": {
            "金力": {"cats": [], "vals": []},
            "江升": {"cats": [], "vals": []},
        },
        "cat_month_stack": {},
        "top_prod": {"金力": empty_top, "江升": empty_top},
        "monthly_top": {"金力": {}, "江升": {}},
        "monthly_bottom": {"金力": {}, "江升": {}},
        "main_cats": list(MAIN_CATS),
        "cab_monthly_top": {},
        "cab_top": empty_top,
        "cab_point_month": {p: [] for p in CABINET_POINTS},
        "js_cab_monthly_top": {},
        "js_cab_top": empty_top,
        "js_cab_point_month": {},
        "js_cab_points": [],
        "cat_margin": {
            "金力": {"names": [], "amt": [], "margin": [], "profit": []},
            "江升": {"names": [], "amt": [], "margin": [], "profit": []},
        },
        "suppliers": {
            "金力": {"names": [], "amt": []},
            "江升": {"names": [], "amt": []},
        },
        "common_prod": {"names": [], "金力": [], "江升": []},
        "sell_through": [],
        "peak": {},
        "low": {},
        "grand": {
            "门店零售金额": 0,
            "自取柜零售金额": 0,
            "江升售卖机零售金额": 0,
            "含柜总零售": 0,
            "门店毛利": 0,
            "含柜总毛利": 0,
            "进货金额": 0,
            "门店零售数量": 0,
            "自取柜数量": 0,
            "江升售卖机数量": 0,
            "调拨江升金额": 0,
        },
        "months": [],
        "ym_list": [],
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
        "period_label": "暂无数据",
    }


def save_cache(report: dict | None = None) -> dict:
    report = report or build_report()
    payload = json.dumps(report, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO report_cache (id, payload, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (payload, datetime.now().isoformat(timespec="seconds")),
        )
    return report


def load_cache(rebuild: bool = False) -> dict:
    if rebuild:
        return save_cache()
    with connect() as conn:
        row = conn.execute("SELECT payload FROM report_cache WHERE id=1").fetchone()
    if row:
        report = json.loads(row["payload"])
        # 旧缓存缺新字段时自动重算（例如刚部署滞销 Bottom5）
        if "monthly_bottom" not in report or "main_cats" not in report:
            return save_cache()
        return report
    return save_cache()
