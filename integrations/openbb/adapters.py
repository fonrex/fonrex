"""OpenBB Workspace response adapters and formatters.

Transforms Fonrex domain responses into contracts expected by OpenBB Workspace widgets:
- type: "metric"  -> list of {"label": str, "value": Any, "delta": Optional[Any]}
- type: "chart"   -> Plotly Figure JSON {"data": [...], "layout": {...}}
- type: "table"   -> AgGrid row array [ { ... }, ... ]
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


def _to_plain_value(val: Any) -> Any:
    """Convert Decimal, datetime, and date to JSON-friendly primitives."""
    if isinstance(val, Decimal):
        return int(val) if val % 1 == 0 else float(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Coerce Pydantic models or dict-like objects to a standard dictionary."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        res = obj.model_dump(mode="python")
        if isinstance(res, dict):
            return res
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        res = obj.dict()
        if isinstance(res, dict):
            return res
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Metric Adapters (type: "metric")
# ──────────────────────────────────────────────────────────────────────────────


def _to_num(val: Any) -> Any:
    val = _to_plain_value(val)
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            f = float(val)
            return int(f) if f.is_integer() else f
        except (ValueError, TypeError):
            pass
    return val


def format_quote_metric(quote_obj: Any) -> List[Dict[str, Any]]:
    """Format quote snapshot into OpenBB metric cards."""
    data = _to_dict(quote_obj)
    ticker = data.get("ticker", "N/A")
    price = _to_num(data.get("price") or data.get("close"))
    change = _to_num(data.get("change"))
    change_pct = _to_num(data.get("change_pct"))
    volume = _to_plain_value(data.get("volume"))
    high = _to_num(data.get("high"))
    low = _to_num(data.get("low"))
    prev_close = _to_num(data.get("previous_close"))

    if isinstance(price, (int, float)):
        price = round(price, 2)
    if isinstance(change, (int, float)):
        change = round(change, 2)
    if isinstance(change_pct, (int, float)):
        change_pct = round(change_pct, 2)
    if isinstance(high, (int, float)):
        high = round(high, 2)
    if isinstance(low, (int, float)):
        low = round(low, 2)
    if isinstance(prev_close, (int, float)):
        prev_close = round(prev_close, 2)

    metrics: List[Dict[str, Any]] = [
        {
            "label": f"{ticker} Price",
            "value": price,
            "delta": change_pct,
        }
    ]

    if change is not None or change_pct is not None:
        metrics.append(
            {
                "label": "Change",
                "value": f"{change:+.2f}" if isinstance(change, (int, float)) else change,
                "delta": f"{change_pct:+.2f}%" if isinstance(change_pct, (int, float)) else change_pct,
            }
        )

    if prev_close is not None:
        metrics.append({"label": "Previous Close", "value": prev_close, "delta": None})
    if high is not None:
        metrics.append({"label": "Day High", "value": high, "delta": None})
    if low is not None:
        metrics.append({"label": "Day Low", "value": low, "delta": None})
    if volume is not None:
        metrics.append({"label": "Volume", "value": volume, "delta": None})

    return metrics


def format_macro_rates_metric(macro_obj: Any) -> List[Dict[str, Any]]:
    """Format FRED macro rates into OpenBB metric cards."""
    data = _to_dict(macro_obj)
    rf_data = data.get("risk_free_rate") or {}
    val = rf_data.get("value")
    unit = rf_data.get("unit") or "%"
    obs_date = rf_data.get("observation_date")

    display_val = f"{val}{'%' if unit == 'percent' else ''}" if val is not None else "N/A"
    label = rf_data.get("label") or "US 10Y Risk-Free Rate (DGS10)"
    if obs_date:
        label = f"{label} ({obs_date})"

    return [
        {
            "label": label,
            "value": display_val,
            "delta": None,
        }
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Chart Adapters (type: "chart" -> Plotly Figure JSON)
# ──────────────────────────────────────────────────────────────────────────────


def format_candlestick_chart(
    ticker: str,
    records: List[Dict[str, Any]],
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Format OHLCV records into a standard Plotly candlestick figure dictionary."""
    dates: List[str] = []
    opens: List[Optional[float]] = []
    highs: List[Optional[float]] = []
    lows: List[Optional[float]] = []
    closes: List[Optional[float]] = []
    volumes: List[Optional[int]] = []

    for r in records:
        d = r.get("Date") or r.get("time") or r.get("timestamp")
        dates.append(_to_plain_value(d))
        opens.append(_to_plain_value(r.get("Open") or r.get("open")))
        highs.append(_to_plain_value(r.get("High") or r.get("high")))
        lows.append(_to_plain_value(r.get("Low") or r.get("low")))
        closes.append(_to_plain_value(r.get("Close") or r.get("close")))
        volumes.append(_to_plain_value(r.get("Volume") or r.get("volume")))

    data_traces: List[Dict[str, Any]] = [
        {
            "type": "candlestick",
            "name": ticker,
            "x": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    ]

    layout: Dict[str, Any] = {
        "title": title or f"{ticker} OHLCV",
        "xaxis": {
            "rangeslider": {"visible": False},
            "type": "date",
        },
        "yaxis": {
            "title": "Price",
        },
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
    }

    return {
        "data": data_traces,
        "layout": layout,
    }


def format_indicator_chart(
    ticker: str,
    indicator_name: str,
    series_list: List[Any],
) -> Dict[str, Any]:
    """Format indicator series into a Plotly line figure dictionary."""
    data_traces: List[Dict[str, Any]] = []

    for s in series_list:
        s_dict = _to_dict(s)
        name = s_dict.get("name", indicator_name)
        values = s_dict.get("values", [])
        xs = []
        ys = []
        for pt in values:
            pt_dict = _to_dict(pt)
            t = pt_dict.get("t")
            v = pt_dict.get("v")
            if v is not None:
                xs.append(_to_plain_value(t))
                ys.append(_to_plain_value(v))

        data_traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": name,
                "x": xs,
                "y": ys,
            }
        )

    return {
        "data": data_traces,
        "layout": {
            "title": f"{ticker} {indicator_name.upper()}",
            "xaxis": {"rangeslider": {"visible": False}},
            "yaxis": {"title": indicator_name.upper()},
            "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        },
    }


def format_technical_multi_chart(
    ticker: str,
    indicators_result: Any,
) -> Dict[str, Any]:
    """Format multi-indicator results into a multi-trace Plotly line chart."""
    result_dict = _to_dict(indicators_result)
    indicators = result_dict.get("indicators", {})

    data_traces: List[Dict[str, Any]] = []

    for ind_name, ind_res in indicators.items():
        ind_dict = _to_dict(ind_res)
        series_list = ind_dict.get("series", [])
        for s in series_list:
            s_dict = _to_dict(s)
            s_name = s_dict.get("name", ind_name)
            values = s_dict.get("values", [])
            xs = []
            ys = []
            for pt in values:
                pt_dict = _to_dict(pt)
                v = pt_dict.get("v")
                t = pt_dict.get("t")
                if v is not None:
                    xs.append(_to_plain_value(t))
                    ys.append(_to_plain_value(v))
            data_traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "name": s_name,
                    "x": xs,
                    "y": ys,
                }
            )

    return {
        "data": data_traces,
        "layout": {
            "title": f"{ticker} Technical Indicators",
            "xaxis": {"rangeslider": {"visible": False}},
            "yaxis": {"title": "Value"},
            "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        },
    }


def format_technical_chart_overlay(
    ticker: str,
    chart_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Format candlesticks with indicator overlays into a Plotly figure."""
    timestamps = chart_payload.get("timestamps", [])
    ohlcv = chart_payload.get("ohlcv", {})
    indicators = chart_payload.get("indicators", {})

    data_traces: List[Dict[str, Any]] = [
        {
            "type": "candlestick",
            "name": ticker,
            "x": timestamps,
            "open": ohlcv.get("open", []),
            "high": ohlcv.get("high", []),
            "low": ohlcv.get("low", []),
            "close": ohlcv.get("close", []),
        }
    ]

    for ind_name, values in indicators.items():
        data_traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": ind_name,
                "x": timestamps,
                "y": values,
            }
        )

    return {
        "data": data_traces,
        "layout": {
            "title": f"{ticker} Technical Chart",
            "xaxis": {"rangeslider": {"visible": False}},
            "yaxis": {"title": "Price"},
            "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Table Adapters (type: "table" -> list[dict])
# ──────────────────────────────────────────────────────────────────────────────


def format_fundamentals_table(eodhd_data: Any) -> List[Dict[str, Any]]:
    """Flatten nested EODHD fundamentals data into AgGrid table rows."""
    data = _to_dict(eodhd_data)
    rows: List[Dict[str, Any]] = []

    priority_sections = [
        "Highlights",
        "Valuation",
        "General",
        "SharesStats",
        "Technicals",
        "SplitsDividends",
        "AnalystRatings",
        "ESGScores",
    ]

    for section in priority_sections:
        section_data = data.get(section)
        if isinstance(section_data, dict):
            for key, val in section_data.items():
                if isinstance(val, dict):
                    for sub_key, sub_val in val.items():
                        if not isinstance(sub_val, (dict, list)):
                            rows.append(
                                {
                                    "category": section,
                                    "metric": f"{key} - {sub_key}",
                                    "value": _to_plain_value(sub_val),
                                }
                            )
                elif not isinstance(val, list):
                    rows.append(
                        {
                            "category": section,
                            "metric": key,
                            "value": _to_plain_value(val),
                        }
                    )

    # If no priority section matched, flatten top level
    if not rows:
        for k, v in data.items():
            if not isinstance(v, (dict, list)):
                rows.append({"category": "Overview", "metric": k, "value": _to_plain_value(v)})

    return rows


def format_fundamentals_deep_table(deep_data: Any) -> List[Dict[str, Any]]:
    """Flatten deep fundamentals sections into table rows."""
    data = _to_dict(deep_data)
    rows: List[Dict[str, Any]] = []

    for section, content in data.items():
        if section in {"meta", "error", "message"}:
            continue
        if isinstance(content, dict):
            for k, v in content.items():
                if isinstance(v, (dict, list)):
                    continue
                rows.append({"section": section, "field": k, "value": _to_plain_value(v)})
        elif isinstance(content, list):
            for idx, item in enumerate(content[:20]):
                if isinstance(item, dict):
                    for k, v in item.items():
                        if not isinstance(v, (dict, list)):
                            rows.append(
                                {
                                    "section": f"{section} [{idx + 1}]",
                                    "field": k,
                                    "value": _to_plain_value(v),
                                }
                            )

    return rows


def format_dcf_table(dcf_data: Any) -> List[Dict[str, Any]]:
    """Flatten DCF valuation outputs into table rows."""
    data = _to_dict(dcf_data)
    ticker = data.get("ticker", "N/A")
    currency = data.get("currency", "USD")
    current_price = _to_plain_value(data.get("current_price"))

    rows: List[Dict[str, Any]] = [
        {"category": "Summary", "metric": "Ticker", "value": ticker},
        {"category": "Summary", "metric": "Currency", "value": currency},
        {"category": "Summary", "metric": "Current Price", "value": current_price},
    ]

    consensus = _to_plain_value(data.get("consensus_value"))
    consensus_upside = _to_plain_value(data.get("consensus_upside_pct"))
    if consensus is not None:
        rows.append({"category": "Summary", "metric": "Consensus Fair Value", "value": consensus})
    if consensus_upside is not None:
        rows.append(
            {
                "category": "Summary",
                "metric": "Consensus Upside (%)",
                "value": f"{consensus_upside:+.2f}%" if isinstance(consensus_upside, (int, float)) else consensus_upside,
            }
        )

    # Models breakdown
    models = data.get("models", {})
    for m_key, m_val in models.items():
        m_dict = _to_dict(m_val)
        m_name = m_dict.get("model_name", m_key.upper())
        val_per_share = _to_plain_value(m_dict.get("intrinsic_value_per_share"))
        upside = _to_plain_value(m_dict.get("upside_pct"))
        term_val = _to_plain_value(m_dict.get("terminal_value"))

        rows.append(
            {"category": "Models", "metric": f"{m_name} — Intrinsic Value", "value": val_per_share}
        )
        if upside is not None:
            rows.append(
                {
                    "category": "Models",
                    "metric": f"{m_name} — Upside (%)",
                    "value": f"{upside:+.2f}%" if isinstance(upside, (int, float)) else upside,
                }
            )
        if term_val is not None:
            rows.append(
                {"category": "Models", "metric": f"{m_name} — Terminal Value", "value": term_val}
            )

    # WACC breakdown
    wacc_data = data.get("wacc") or {}
    if wacc_data:
        w_dict = _to_dict(wacc_data)
        for w_key in ["wacc", "cost_of_equity", "cost_of_debt", "beta_used", "tax_rate"]:
            if w_key in w_dict:
                v = _to_plain_value(w_dict[w_key])
                if w_key != "beta_used" and v is not None:
                    try:
                        v_str = f"{float(v) * 100:.2f}%"
                    except (ValueError, TypeError):
                        v_str = str(v)
                else:
                    v_str = str(v)
                rows.append({"category": "WACC", "metric": w_key.replace("_", " ").title(), "value": v_str})

    return rows


def format_dcf_compare_table(dcf_data: Any) -> List[Dict[str, Any]]:
    """Format DCF model comparisons side-by-side into table rows."""
    data = _to_dict(dcf_data)
    models = data.get("models", {})
    currency = data.get("currency", "USD")
    current_price = _to_plain_value(data.get("current_price"))

    rows: List[Dict[str, Any]] = []
    for m_key, m_val in models.items():
        m_dict = _to_dict(m_val)
        intrinsic = _to_plain_value(m_dict.get("intrinsic_value_per_share"))
        upside = _to_plain_value(m_dict.get("upside_pct"))
        try:
            up_float = round(float(upside), 2) if upside is not None else None
        except (ValueError, TypeError):
            up_float = upside
        try:
            iv_float = round(float(intrinsic), 2) if intrinsic is not None else None
        except (ValueError, TypeError):
            iv_float = intrinsic
        rows.append(
            {
                "model_key": m_key,
                "model_name": m_dict.get("model_name", m_key),
                "current_price": current_price,
                "intrinsic_value": iv_float,
                "upside_pct": up_float,
                "currency": currency,
            }
        )
    return rows


def format_dcf_sensitivity_table(sensitivity_data: Any) -> List[Dict[str, Any]]:
    """Flatten DCF sensitivity matrix into a list of row cells."""
    data = _to_dict(sensitivity_data)
    matrix = data.get("matrix", [])
    model = data.get("model", "fcf")

    rows: List[Dict[str, Any]] = []
    for row in matrix:
        for cell in row:
            c_dict = _to_dict(cell)
            w = _to_plain_value(c_dict.get("wacc"))
            g = _to_plain_value(c_dict.get("terminal_growth"))
            iv = _to_plain_value(c_dict.get("intrinsic_value"))
            up = _to_plain_value(c_dict.get("upside_pct"))

            try:
                w_str = f"{float(w) * 100:.1f}%"
            except (ValueError, TypeError):
                w_str = str(w)

            try:
                g_str = f"{float(g) * 100:.1f}%"
            except (ValueError, TypeError):
                g_str = str(g)

            try:
                iv_float = round(float(iv), 2) if iv is not None else None
            except (ValueError, TypeError):
                iv_float = iv

            try:
                up_float = round(float(up), 2) if up is not None else None
            except (ValueError, TypeError):
                up_float = up

            rows.append(
                {
                    "model": model,
                    "wacc": w_str,
                    "terminal_growth": g_str,
                    "intrinsic_value": iv_float,
                    "upside_pct": up_float,
                }
            )
    return rows


def format_batch_quotes_table(quotes_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert batch quotes dictionary into AgGrid rows."""
    rows: List[Dict[str, Any]] = []
    for ticker, payload in quotes_dict.items():
        if not payload or not isinstance(payload, dict):
            rows.append(
                {
                    "ticker": ticker,
                    "price": None,
                    "change": None,
                    "change_pct": None,
                    "volume": None,
                    "is_realtime": False,
                    "source": "unavailable",
                }
            )
            continue

        qdata = payload.get("data") or {}
        price = _to_plain_value(qdata.get("close") or qdata.get("price"))
        change = _to_plain_value(qdata.get("change"))
        change_pct = _to_plain_value(qdata.get("change_pct"))
        volume = _to_plain_value(qdata.get("volume"))

        rows.append(
            {
                "ticker": ticker,
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "is_realtime": payload.get("is_realtime", False),
                "source": payload.get("source", "unknown"),
            }
        )
    return rows


def format_etf_details_table(etf_data: Any) -> List[Dict[str, Any]]:
    """Format ETF details into table rows."""
    data = _to_dict(etf_data)
    fields = [
        ("isin", "ISIN"),
        ("name", "Name"),
        ("ticker", "Ticker"),
        ("net_expense_ratio", "TER (Total Expense Ratio)"),
        ("total_net_assets", "AUM (Total Net Assets)"),
        ("domicile", "Domicile"),
        ("replication_method", "Replication Method"),
        ("distribution_policy", "Distribution Policy"),
        ("index_tracked", "Index Tracked"),
        ("inception_date", "Inception Date"),
        ("nb_holdings", "Number of Holdings"),
    ]

    rows: List[Dict[str, Any]] = []
    for field_key, label in fields:
        val = data.get(field_key)
        if val is not None:
            rows.append({"metric": label, "value": _to_plain_value(val)})

    return rows
