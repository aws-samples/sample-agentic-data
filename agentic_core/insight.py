"""
Insight Engine — 确定性数据分析工具集。
所有计算纯 Python (numpy/scipy)，零 LLM 成本，毫秒级。

四个工具:
1. detect_anomaly — 异常检测 (Z-Score + IQR)
2. analyze_trend — 趋势分析 (线性回归 + 移动平均 + 环比同比)
3. attribution — 维度归因 (方差贡献度)
4. forecast — 预测 (指数平滑 / 线性外推)
"""

import json
import numpy as np
from strands import tool


def _parse_data(data_str: str) -> list:
    """Parse JSON string or already-parsed list."""
    if isinstance(data_str, list):
        return data_str
    try:
        return json.loads(data_str)
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_series(data: list, metric: str, time_col: str = "") -> tuple:
    """Extract numeric series and optional time labels from data."""
    values = []
    labels = []
    for row in data:
        if not isinstance(row, dict):
            continue
        val = row.get(metric)
        if val is None:
            continue
        try:
            values.append(float(val))
        except (ValueError, TypeError):
            continue
        if time_col and time_col in row:
            labels.append(str(row[time_col]))
        else:
            labels.append(str(len(labels)))
    return np.array(values), labels


@tool
def detect_anomaly(data: str, metric: str, time_col: str = "") -> str:
    """检测时间序列中的异常数据点。
    输入: data=JSON数组, metric=要检测的数值字段名, time_col=时间字段名(可选)
    算法: Z-Score (>2σ) + IQR 双重验证
    输出: 异常点列表，含位置、值、严重程度、方向"""

    rows = _parse_data(data)
    if not rows:
        return json.dumps({"anomalies": [], "error": "无法解析数据"}, ensure_ascii=False)

    values, labels = _extract_series(rows, metric, time_col)
    if len(values) < 5:
        return json.dumps({"anomalies": [], "summary": f"数据点不足({len(values)}条)，需至少5条"}, ensure_ascii=False)

    # Z-Score method
    mean = np.mean(values)
    std = np.std(values)
    z_scores = (values - mean) / std if std > 0 else np.zeros_like(values)

    # IQR method
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr

    anomalies = []
    for i, (v, z) in enumerate(zip(values, z_scores)):
        is_zscore = abs(z) > 2.0
        is_iqr = v < lower_bound or v > upper_bound

        if is_zscore or is_iqr:
            severity = "高" if (abs(z) > 3.0 or (is_zscore and is_iqr)) else "中"
            direction = "偏高" if v > mean else "偏低"
            anomalies.append({
                "index": i,
                "label": labels[i] if i < len(labels) else str(i),
                "value": round(float(v), 4),
                "z_score": round(float(z), 2),
                "severity": severity,
                "direction": direction,
                "methods": ("Z-Score+IQR" if (is_zscore and is_iqr) else "Z-Score" if is_zscore else "IQR"),
            })

    result = {
        "anomalies": anomalies,
        "stats": {
            "mean": round(float(mean), 4),
            "std": round(float(std), 4),
            "normal_range": [round(float(mean - 2*std), 4), round(float(mean + 2*std), 4)],
            "iqr_range": [round(float(lower_bound), 4), round(float(upper_bound), 4)],
        },
        "total_points": len(values),
        "anomaly_count": len(anomalies),
        "summary": f"在{len(values)}个数据点中发现{len(anomalies)}个异常" if anomalies else f"在{len(values)}个数据点中未发现异常",
    }
    return json.dumps(result, ensure_ascii=False)


@tool
def analyze_trend(data: str, metric: str, time_col: str = "") -> str:
    """分析时间序列的趋势、变化率和拐点。
    输入: data=JSON数组, metric=数值字段名, time_col=时间字段名(可选)
    算法: 线性回归 + 移动平均 + 环比分析
    输出: 趋势方向、变化率、拐点位置、环比"""

    from scipy import stats as sp_stats

    rows = _parse_data(data)
    values, labels = _extract_series(rows, metric, time_col)
    if len(values) < 3:
        return json.dumps({"error": f"数据点不足({len(values)}条)，需至少3条"}, ensure_ascii=False)

    n = len(values)
    x = np.arange(n, dtype=float)

    # Linear regression
    slope, intercept, r_value, p_value, std_err = sp_stats.linregress(x, values)
    r_squared = r_value ** 2

    # Trend direction
    if abs(slope) < std_err * 0.5:
        direction = "平稳"
        direction_symbol = "→"
    elif slope > 0:
        direction = "上升"
        direction_symbol = "↑"
    else:
        direction = "下降"
        direction_symbol = "↓"

    # Change rate (first vs last period)
    if values[0] != 0:
        total_change_pct = round(float((values[-1] - values[0]) / abs(values[0]) * 100), 2)
    else:
        total_change_pct = 0.0

    # Period-over-period (环比)
    pops = []
    for i in range(1, n):
        if values[i-1] != 0:
            pops.append(round(float((values[i] - values[i-1]) / abs(values[i-1]) * 100), 2))
        else:
            pops.append(0.0)

    # Moving average (window = min(5, n//2))
    window = max(2, min(5, n // 2))
    ma = np.convolve(values, np.ones(window)/window, mode='valid')

    # Detect turning points (where MA direction changes)
    turning_points = []
    if len(ma) >= 3:
        ma_diff = np.diff(ma)
        for i in range(1, len(ma_diff)):
            if ma_diff[i-1] * ma_diff[i] < 0:  # Sign change
                tp_idx = i + window // 2
                if tp_idx < n:
                    turning_points.append({
                        "index": int(tp_idx),
                        "label": labels[tp_idx] if tp_idx < len(labels) else str(tp_idx),
                        "value": round(float(values[tp_idx]), 4),
                        "type": "峰值" if ma_diff[i-1] > 0 else "谷值",
                    })

    # Recent trend (last 30% of data)
    recent_n = max(3, n // 3)
    recent_vals = values[-recent_n:]
    recent_x = np.arange(recent_n, dtype=float)
    if len(recent_vals) >= 3:
        r_slope, _, _, _, _ = sp_stats.linregress(recent_x, recent_vals)
        if abs(r_slope) < std_err * 0.5:
            recent_direction = "平稳"
        elif r_slope > 0:
            recent_direction = "上升"
        else:
            recent_direction = "下降"
    else:
        recent_direction = direction

    result = {
        "direction": direction,
        "direction_symbol": direction_symbol,
        "slope_per_period": round(float(slope), 4),
        "r_squared": round(float(r_squared), 4),
        "p_value": round(float(p_value), 6),
        "significant": bool(p_value < 0.05),
        "total_change_pct": total_change_pct,
        "recent_direction": recent_direction,
        "period_over_period": pops[-5:] if len(pops) > 5 else pops,
        "turning_points": turning_points[:5],
        "moving_average": [round(float(v), 4) for v in ma.tolist()],
        "summary": f"整体{direction}趋势{direction_symbol}，变化率{total_change_pct}%，近期{recent_direction}",
    }
    return json.dumps(result, ensure_ascii=False)


@tool
def attribution(data: str, metric: str, dimensions: str) -> str:
    """分析各维度对指标变化的贡献度。
    输入: data=JSON数组, metric=数值字段名, dimensions=逗号分隔的维度字段名
    算法: 组间方差 / 总方差 = 解释力占比
    输出: 各维度贡献度排名 + 各维度值的具体表现"""

    rows = _parse_data(data)
    if not rows:
        return json.dumps({"error": "无法解析数据"}, ensure_ascii=False)

    dim_list = [d.strip() for d in dimensions.split(",") if d.strip()]
    if not dim_list:
        return json.dumps({"error": "未指定维度"}, ensure_ascii=False)

    # Extract metric values
    all_values = []
    for row in rows:
        try:
            all_values.append(float(row.get(metric, 0)))
        except (ValueError, TypeError):
            all_values.append(0.0)

    if not all_values:
        return json.dumps({"error": f"字段 {metric} 无有效数值"}, ensure_ascii=False)

    total_var = np.var(all_values)
    if total_var == 0:
        return json.dumps({"contributions": [], "summary": "指标无波动，无法进行归因分析"}, ensure_ascii=False)

    contributions = []
    for dim in dim_list:
        # Group by dimension
        groups = {}
        for i, row in enumerate(rows):
            key = str(row.get(dim, "未知"))
            if key not in groups:
                groups[key] = []
            if i < len(all_values):
                groups[key].append(all_values[i])

        if len(groups) < 2:
            continue

        # Between-group variance (SSB / SST)
        grand_mean = np.mean(all_values)
        ssb = sum(len(g) * (np.mean(g) - grand_mean) ** 2 for g in groups.values())
        sst = total_var * len(all_values)
        explained_ratio = float(ssb / sst) if sst > 0 else 0

        # Per-group stats
        group_stats = []
        for key, vals in sorted(groups.items(), key=lambda x: -np.mean(x[1])):
            group_stats.append({
                "value": key,
                "count": len(vals),
                "mean": round(float(np.mean(vals)), 4),
                "std": round(float(np.std(vals)), 4),
                "min": round(float(np.min(vals)), 4),
                "max": round(float(np.max(vals)), 4),
            })

        contributions.append({
            "dimension": dim,
            "explained_ratio": round(explained_ratio, 4),
            "explained_pct": round(explained_ratio * 100, 1),
            "groups": len(groups),
            "top_groups": group_stats[:10],
        })

    # Sort by contribution
    contributions.sort(key=lambda x: -x["explained_ratio"])

    # Normalize to 100%
    total_explained = sum(c["explained_ratio"] for c in contributions)
    if total_explained > 0:
        for c in contributions:
            c["normalized_pct"] = round(c["explained_ratio"] / total_explained * 100, 1)

    summary_parts = []
    for c in contributions[:3]:
        summary_parts.append(f"{c['dimension']}({c.get('normalized_pct', c['explained_pct'])}%)")

    result = {
        "contributions": contributions,
        "total_explained_pct": round(total_explained * 100, 1),
        "metric_stats": {
            "mean": round(float(np.mean(all_values)), 4),
            "std": round(float(np.std(all_values)), 4),
            "total_points": len(all_values),
        },
        "summary": f"主要影响因素: {', '.join(summary_parts)}" if summary_parts else "未找到显著影响因素",
    }
    return json.dumps(result, ensure_ascii=False)


@tool
def forecast(data: str, metric: str, periods: int = 7, time_col: str = "") -> str:
    """基于历史数据预测未来趋势。
    输入: data=JSON数组, metric=数值字段名, periods=预测期数, time_col=时间字段名(可选)
    算法: 双指数平滑(Holt) + 线性回归，取加权平均
    输出: 预测值 + 置信区间 + 趋势描述"""

    from scipy import stats as sp_stats

    rows = _parse_data(data)
    values, labels = _extract_series(rows, metric, time_col)
    n = len(values)
    if n < 5:
        return json.dumps({"error": f"数据点不足({n}条)，需至少5条才能预测"}, ensure_ascii=False)

    periods = min(periods, n)  # Don't predict more than data length

    # --- Method 1: Holt's double exponential smoothing ---
    alpha = 0.3  # Level smoothing
    beta = 0.1   # Trend smoothing

    level = values[0]
    trend = np.mean(np.diff(values[:min(5, n)]))  # Initial trend from first few points

    levels = [level]
    trends = [trend]

    for i in range(1, n):
        new_level = alpha * values[i] + (1 - alpha) * (level + trend)
        new_trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
        trend = new_trend
        levels.append(level)
        trends.append(trend)

    holt_forecast = [level + trend * (i + 1) for i in range(periods)]

    # --- Method 2: Linear regression ---
    x = np.arange(n, dtype=float)
    slope, intercept, r_value, p_value, std_err = sp_stats.linregress(x, values)
    lr_forecast = [intercept + slope * (n + i) for i in range(periods)]

    # --- Weighted average (Holt 60%, LR 40%) ---
    # Holt adapts faster to recent changes, LR is more stable
    forecasted = [0.6 * h + 0.4 * l for h, l in zip(holt_forecast, lr_forecast)]

    # --- Confidence interval ---
    residuals = values - (intercept + slope * x)
    residual_std = float(np.std(residuals))

    confidence = []
    for i, f_val in enumerate(forecasted):
        # Wider confidence as we predict further
        margin = residual_std * 1.96 * np.sqrt(1 + (i + 1) / n)
        confidence.append({
            "period": i + 1,
            "predicted": round(float(f_val), 4),
            "lower": round(float(f_val - margin), 4),
            "upper": round(float(f_val + margin), 4),
        })

    # Forecast direction
    if len(forecasted) >= 2:
        if forecasted[-1] > forecasted[0] * 1.01:
            forecast_direction = "上升"
        elif forecasted[-1] < forecasted[0] * 0.99:
            forecast_direction = "下降"
        else:
            forecast_direction = "平稳"
    else:
        forecast_direction = "平稳"

    # Change vs current
    current = float(values[-1])
    predicted_end = float(forecasted[-1])
    if current != 0:
        change_pct = round((predicted_end - current) / abs(current) * 100, 2)
    else:
        change_pct = 0.0

    result = {
        "current_value": round(current, 4),
        "predictions": confidence,
        "forecast_direction": forecast_direction,
        "change_pct": change_pct,
        "method": "Holt双指数平滑(60%) + 线性回归(40%)",
        "model_quality": {
            "r_squared": round(float(r_value ** 2), 4),
            "residual_std": round(residual_std, 4),
        },
        "historical_points": n,
        "forecast_periods": periods,
        "summary": f"预测{forecast_direction}趋势，预计从{round(current,2)}变化到{round(predicted_end,2)}（{'+' if change_pct>0 else ''}{change_pct}%）",
    }
    return json.dumps(result, ensure_ascii=False)
