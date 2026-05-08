#!/usr/bin/env python3
"""
股票分析報告產生器
用法: python stock_analyst.py 2330
      python stock_analyst.py        ← 互動輸入
輸出: {股票代號}_report_{日期}.html（自動開啟瀏覽器）
"""

import sys
import os
import json
import time
import webbrowser
from datetime import datetime, timedelta, date
from pathlib import Path

# 自動載入同目錄的 .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import requests
import pandas as pd
import numpy as np

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# ── 1. K 線資料（TWSE STOCK_DAY） ────────────────────────

def fetch_kline(stock_code: str) -> tuple[pd.DataFrame, str]:
    """抓最近 60 個交易日 K 線，回傳 (DataFrame, 股票名稱)"""
    rows = []
    stock_name = stock_code

    # 抓最近 4 個月（確保涵蓋 60 個交易日）
    today = date.today()
    months = []
    d = today.replace(day=1)
    for _ in range(4):
        months.append(d)
        if d.month == 1:
            d = d.replace(year=d.year - 1, month=12)
        else:
            d = d.replace(month=d.month - 1)

    for m in months:
        date_str = m.strftime("%Y%m01")
        url = (
            f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
            f"?response=json&date={date_str}&stockNo={stock_code}"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
            data = resp.json()
            if data.get("stat") != "OK" or not data.get("data"):
                time.sleep(0.3)
                continue
            # 從 title 解析股票名稱（格式：「113年03月 2330 台積電 各日成交資訊」）
            if len(rows) == 0:
                parts = data.get("title", "").split()
                if len(parts) >= 3:
                    stock_name = parts[2]
            rows.extend(data["data"])
        except Exception as e:
            print(f"  警告：{m.strftime('%Y-%m')} K 線抓取失敗（{e}）")
        time.sleep(0.3)

    if not rows:
        raise ValueError(f"無法取得 {stock_code} 的 K 線資料，請確認股票代號是否正確。")

    df = pd.DataFrame(
        rows,
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數", "註記"],
    )

    for col in ["開盤價", "最高價", "最低價", "收盤價"]:
        df[col] = df[col].str.replace(",", "").astype(float)
    df["成交股數"] = df["成交股數"].str.replace(",", "").astype(float) / 1000  # 轉成張

    def roc_to_date(s):
        p = s.split("/")
        return date(int(p[0]) + 1911, int(p[1]), int(p[2]))

    df["date"] = df["日期"].apply(roc_to_date)
    df = df.sort_values("date").tail(60).reset_index(drop=True)
    return df, stock_name


# ── 2. 三大法人（TWSE T86，共 60 個交易日） ────────────────

def fetch_institutional(stock_code: str, dates: list) -> pd.DataFrame:
    """逐日抓 T86 三大法人，回傳 DataFrame"""
    records = []
    total = len(dates)

    for i, d in enumerate(dates):
        print(f"\r  法人資料 {i+1}/{total}...", end="", flush=True)
        date_str = d.strftime("%Y%m%d")
        url = (
            f"https://www.twse.com.tw/rwd/zh/fund/T86"
            f"?date={date_str}&selectType=ALLBUT0999&response=json"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
            data = resp.json()
            if data.get("stat") != "OK" or not data.get("data"):
                time.sleep(0.4)
                continue
            for row in data["data"]:
                if str(row[0]).strip() == stock_code:
                    def clean(x):
                        try:
                            return float(str(x).replace(",", ""))
                        except Exception:
                            return 0.0
                    records.append({
                        "date": d,
                        "外資買賣超": clean(row[4]),
                        "投信買賣超": clean(row[7]),
                        "自營商買賣超": clean(row[8]),
                        "三大法人合計": clean(row[9]) if len(row) > 9 else 0.0,
                    })
                    break
        except Exception:
            pass
        time.sleep(0.4)

    print()
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


# ── 3. 技術指標計算 ──────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["收盤價"]
    h = df["最高價"]
    lo = df["最低價"]

    # MA
    df["MA5"]  = c.rolling(5).mean().round(2)
    df["MA10"] = c.rolling(10).mean().round(2)
    df["MA20"] = c.rolling(20).mean().round(2)

    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = (100 - 100 / (1 + rs)).round(2)

    # KD(9)
    low9  = lo.rolling(9).min()
    high9 = h.rolling(9).max()
    rsv = (c - low9) / (high9 - low9).replace(0, float("nan")) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    df["K"] = k.round(2)
    df["D"] = d.round(2)

    # MACD(12,26,9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["DIF"]  = (ema12 - ema26).round(3)
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean().round(3)
    df["OSC"]  = (df["DIF"] - df["MACD"]).round(3)

    # Bollinger(20, 2σ)
    ma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["BB_upper"] = (ma20 + 2 * std20).round(2)
    df["BB_lower"] = (ma20 - 2 * std20).round(2)
    df["BB_mid"]   = ma20.round(2)

    return df


# ── 4. 技術警示 ──────────────────────────────────────────

def generate_alerts(df: pd.DataFrame) -> list:
    alerts = []
    if len(df) < 2:
        return alerts
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = last["收盤價"]

    def add(priority, type_, msg):
        alerts.append({"priority": priority, "type": type_, "msg": msg})

    # P1 強訊號
    if last["RSI"] > 80:
        add(1, "RSI超買", f"RSI {last['RSI']:.1f} 超過 80，超買警示")
    if last["RSI"] < 20:
        add(1, "RSI超賣", f"RSI {last['RSI']:.1f} 低於 20，超賣訊號")
    if prev["K"] <= prev["D"] and last["K"] > last["D"]:
        add(1, "KD黃金交叉", f"KD 黃金交叉（K={last['K']:.1f} 穿越 D={last['D']:.1f}）")
    if prev["K"] >= prev["D"] and last["K"] < last["D"]:
        add(1, "KD死亡交叉", f"KD 死亡交叉（K={last['K']:.1f} 跌破 D={last['D']:.1f}）")
    if prev["DIF"] <= prev["MACD"] and last["DIF"] > last["MACD"]:
        add(1, "MACD黃金交叉", "MACD DIF 向上穿越 Signal，買進訊號")
    if prev["DIF"] >= prev["MACD"] and last["DIF"] < last["MACD"]:
        add(1, "MACD死亡交叉", "MACD DIF 向下跌破 Signal，賣出訊號")

    # P2 中等
    if last["K"] > 80:
        add(2, "KD超買", f"K 值 {last['K']:.1f} 進入超買區（>80）")
    if last["K"] < 20:
        add(2, "KD超賣", f"K 值 {last['K']:.1f} 進入超賣區（<20）")
    if not pd.isna(last["BB_upper"]) and close >= last["BB_upper"]:
        add(2, "觸布林上軌", f"收盤 {close} 觸及布林上軌 {last['BB_upper']}")
    if not pd.isna(last["BB_lower"]) and close <= last["BB_lower"]:
        add(2, "觸布林下軌", f"收盤 {close} 觸及布林下軌 {last['BB_lower']}")

    # P3 提示
    if not pd.isna(last["MA5"]) and close < last["MA5"]:
        add(3, "跌破MA5", f"收盤 {close} 跌破 MA5 {last['MA5']}")
    if not pd.isna(last["MA20"]) and close < last["MA20"]:
        add(3, "跌破MA20", f"收盤 {close} 跌破 MA20 {last['MA20']}")
    if not pd.isna(last["OSC"]) and not pd.isna(prev["OSC"]) and last["OSC"] < 0 and prev["OSC"] >= 0:
        add(3, "MACD柱轉負", "MACD 能量柱由正轉負，動能減弱")

    return sorted(alerts, key=lambda x: x["priority"])


# ── 5. Firecrawl 新聞 ────────────────────────────────────

def fetch_news(stock_code: str, stock_name: str) -> list:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return []
    query = f"{stock_code} {stock_name} 股票 最新消息"
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "limit": 5},
            timeout=15,
        )
        data = resp.json()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in data.get("data", [])
        ]
    except Exception as e:
        print(f"  新聞抓取失敗：{e}")
        return []


# ── 6. Groq AI 分析 ─────────────────────────────────────────

def ai_analysis(stock_code, stock_name, df, inst_df, alerts, news) -> dict:
    from groq import Groq
    last = df.iloc[-1]

    # 近 5 日走勢
    price_rows = []
    for _, row in df.tail(5).iterrows():
        price_rows.append(
            f"  {row['date']} 收:{row['收盤價']} 開:{row['開盤價']} 高:{row['最高價']} 低:{row['最低價']}"
        )

    # 近 5 日法人
    inst_rows = []
    if not inst_df.empty:
        for _, row in inst_df.tail(5).iterrows():
            inst_rows.append(
                f"  {row['date'].strftime('%m/%d')} "
                f"外資:{row['外資買賣超']:+,.0f} "
                f"投信:{row['投信買賣超']:+,.0f} "
                f"自營:{row['自營商買賣超']:+,.0f}（千股）"
            )

    alert_text = "\n".join(f"  [P{a['priority']}] {a['type']}: {a['msg']}" for a in alerts) or "  無明顯警示"
    news_text = "\n".join(f"  - {n['title']}\n    {n['snippet']}" for n in news[:3]) or "  無法取得"

    prompt = f"""你是一位專業的台股技術分析師，請根據以下數據產出繁體中文分析報告。

## 股票資訊
代號：{stock_code}  名稱：{stock_name}
報告日期：{datetime.now().strftime('%Y-%m-%d')}

## 最新技術指標
收盤：{last['收盤價']}  開盤：{last['開盤價']}  高：{last['最高價']}  低：{last['最低價']}
MA5：{last['MA5']}  MA10：{last['MA10']}  MA20：{last['MA20']}
RSI(14)：{last['RSI']}
K：{last['K']}  D：{last['D']}
DIF：{last['DIF']}  MACD：{last['MACD']}  OSC：{last['OSC']}
布林上軌：{last['BB_upper']}  中軌：{last['BB_mid']}  下軌：{last['BB_lower']}

## 近 5 日走勢
{chr(10).join(price_rows)}

## 近 5 日三大法人
{chr(10).join(inst_rows) if inst_rows else '  無法取得'}

## 技術警示
{alert_text}

## 最新新聞
{news_text}

請以 JSON 格式回覆，欄位如下（所有文字用繁體中文）：
{{
  "stance": "多方 / 空方 / 觀望",
  "short_term": "3-5 個交易日短線預測（1-2 句）",
  "key_levels": {{"support": [數字, 數字], "resistance": [數字, 數字]}},
  "suggestion": "具體操作建議（1-3 句）",
  "risk": "風險提醒（1-2 句）",
  "summary": "給散戶的白話整體分析（3-5 句）"
}}"""

    client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1024,
    )

    text = result.stdout.strip()
    text = resp.choices[0].message.content.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    return json.loads(text)


# ── 7. Plotly 圖表 HTML ──────────────────────────────────

def build_chart_html(df: pd.DataFrame, inst_df: pd.DataFrame) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    dates = [str(d) for d in df["date"]]
    colors = ["#ef5350" if r["收盤價"] >= r["開盤價"] else "#26a69a" for _, r in df.iterrows()]

    # ── 圖一：K 線 + MA + 布林 + 成交量 ──────────────────
    fig1 = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.03,
    )

    fig1.add_trace(go.Candlestick(
        x=dates, open=df["開盤價"], high=df["最高價"],
        low=df["最低價"], close=df["收盤價"],
        increasing_line_color="#ef5350", decreasing_line_color="#26a69a",
        name="K 線",
    ), row=1, col=1)

    for col, color, width in [("MA5", "#ffd700", 1.5), ("MA10", "#ff9800", 1.5), ("MA20", "#7c4dff", 1.5)]:
        fig1.add_trace(go.Scatter(
            x=dates, y=df[col], mode="lines",
            name=col, line=dict(color=color, width=width),
        ), row=1, col=1)

    fig1.add_trace(go.Scatter(
        x=dates + dates[::-1],
        y=list(df["BB_upper"]) + list(df["BB_lower"][::-1]),
        fill="toself", fillcolor="rgba(144,202,249,0.1)",
        line=dict(color="rgba(144,202,249,0.4)"), name="布林通道",
        hoverinfo="skip",
    ), row=1, col=1)

    fig1.add_trace(go.Bar(
        x=dates, y=df["成交股數"], marker_color=colors,
        name="成交量（張）", opacity=0.8,
    ), row=2, col=1)

    fig1.update_layout(
        title="K 線 + 均線 + 布林通道",
        xaxis_rangeslider_visible=False,
        **_chart_layout(),
    )

    # ── 圖二：KD ─────────────────────────────────────────
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=dates, y=df["K"], mode="lines", name="K", line=dict(color="#ffd700", width=2)))
    fig2.add_trace(go.Scatter(x=dates, y=df["D"], mode="lines", name="D", line=dict(color="#ef5350", width=2)))
    fig2.add_hline(y=80, line_dash="dash", line_color="rgba(239,83,80,0.5)", annotation_text="超買 80")
    fig2.add_hline(y=20, line_dash="dash", line_color="rgba(38,166,154,0.5)", annotation_text="超賣 20")
    fig2.update_layout(title="KD 指標 (9)", **_chart_layout())

    # ── 圖三：MACD ───────────────────────────────────────
    fig3 = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.5, 0.5], vertical_spacing=0.05)
    fig3.add_trace(go.Scatter(x=dates, y=df["DIF"], mode="lines", name="DIF", line=dict(color="#ffd700", width=2)), row=1, col=1)
    fig3.add_trace(go.Scatter(x=dates, y=df["MACD"], mode="lines", name="Signal", line=dict(color="#ef5350", width=2)), row=1, col=1)

    osc_colors = ["#ef5350" if v >= 0 else "#26a69a" for v in df["OSC"]]
    fig3.add_trace(go.Bar(x=dates, y=df["OSC"], name="OSC", marker_color=osc_colors), row=2, col=1)
    fig3.add_hline(y=0, line_dash="solid", line_color="rgba(255,255,255,0.3)", row=2, col=1)

    # 交叉標記
    for i in range(1, len(df)):
        prev_dif, cur_dif = df["DIF"].iloc[i-1], df["DIF"].iloc[i]
        prev_sig, cur_sig = df["MACD"].iloc[i-1], df["MACD"].iloc[i]
        if prev_dif <= prev_sig and cur_dif > cur_sig:
            fig3.add_trace(go.Scatter(
                x=[dates[i]], y=[df["DIF"].iloc[i]],
                mode="markers", marker=dict(color="#ffd700", size=12, symbol="triangle-up"),
                name="黃金交叉", showlegend=False,
            ), row=1, col=1)
        elif prev_dif >= prev_sig and cur_dif < cur_sig:
            fig3.add_trace(go.Scatter(
                x=[dates[i]], y=[df["DIF"].iloc[i]],
                mode="markers", marker=dict(color="#ef5350", size=12, symbol="triangle-down"),
                name="死亡交叉", showlegend=False,
            ), row=1, col=1)

    fig3.update_layout(title="MACD (12,26,9)", **_chart_layout())

    # ── 圖四：三大法人 ────────────────────────────────────
    inst_html = ""
    if not inst_df.empty:
        inst_dates = [str(d) for d in inst_df["date"]]
        fig4 = go.Figure()
        for col, color in [("外資買賣超", "#42a5f5"), ("投信買賣超", "#ffd700"), ("自營商買賣超", "#ab47bc")]:
            fig4.add_trace(go.Bar(x=inst_dates, y=inst_df[col] / 1000, name=col))
        fig4.update_layout(title="三大法人買賣超（張，正=買超，負=賣超）", barmode="group", **_chart_layout())
        inst_html = fig4.to_html(full_html=False, include_plotlyjs=False)

    chart1 = fig1.to_html(full_html=False, include_plotlyjs=False)
    chart2 = fig2.to_html(full_html=False, include_plotlyjs=False)
    chart3 = fig3.to_html(full_html=False, include_plotlyjs=False)

    return chart1, chart2, chart3, inst_html


def _chart_layout():
    return dict(
        template="plotly_dark",
        height=400,
        margin=dict(l=60, r=20, t=50, b=40),
        legend=dict(orientation="h", y=1.02, x=0),
        paper_bgcolor="#1e1e2e",
        plot_bgcolor="#1e1e2e",
    )


# ── 8. HTML 報告生成 ─────────────────────────────────────

def generate_html(stock_code, stock_name, df, inst_df, alerts, analysis, news) -> str:
    last = df.iloc[-1]
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    stance_color = {"多方": "#26a69a", "空方": "#ef5350", "觀望": "#ffd700"}.get(analysis.get("stance", "觀望"), "#ffd700")

    # Charts
    c1, c2, c3, c4 = build_chart_html(df, inst_df)

    # Alerts HTML
    priority_colors = {1: "#ef5350", 2: "#ffd700", 3: "#90caf9"}
    alerts_html = ""
    if alerts:
        for a in alerts:
            color = priority_colors.get(a["priority"], "#fff")
            alerts_html += f'<div class="alert-item" style="border-left:4px solid {color}"><span class="badge" style="background:{color}">P{a["priority"]}</span> <strong>{a["type"]}</strong>：{a["msg"]}</div>\n'
    else:
        alerts_html = '<div class="alert-item" style="border-left:4px solid #26a69a">無明顯技術警示</div>'

    # News HTML
    news_html = ""
    for n in news:
        news_html += f'<div class="news-item"><a href="{n["url"]}" target="_blank">{n["title"]}</a><p>{n["snippet"]}</p></div>\n'
    if not news_html:
        news_html = "<p>無法取得新聞資料</p>"

    # Key levels
    support = analysis.get("key_levels", {}).get("support", [])
    resistance = analysis.get("key_levels", {}).get("resistance", [])
    support_str = " / ".join(str(s) for s in support) if support else "—"
    resistance_str = " / ".join(str(r) for r in resistance) if resistance else "—"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{stock_code} {stock_name} 技術分析報告</title>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #13131f; color: #e0e0e0; font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }}
    .header {{ background: #1e1e2e; padding: 24px 32px; border-bottom: 1px solid #2a2a3e; }}
    .header h1 {{ font-size: 28px; font-weight: 700; }}
    .header .meta {{ color: #888; margin-top: 6px; font-size: 14px; }}
    .stance-badge {{ display: inline-block; padding: 4px 16px; border-radius: 20px; font-weight: 700; font-size: 18px; margin-left: 12px; background: {stance_color}; color: #fff; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 24px 32px; }}
    .section {{ margin-bottom: 32px; }}
    .section h2 {{ font-size: 18px; font-weight: 600; color: #90caf9; border-bottom: 1px solid #2a2a3e; padding-bottom: 8px; margin-bottom: 16px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }}
    .card {{ background: #1e1e2e; border-radius: 8px; padding: 16px; }}
    .card .label {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
    .card .value {{ font-size: 20px; font-weight: 700; }}
    .summary-box {{ background: #1e1e2e; border-radius: 8px; padding: 20px; line-height: 1.8; }}
    .suggestion-box {{ background: #1a2a3a; border-left: 4px solid #42a5f5; border-radius: 0 8px 8px 0; padding: 16px; margin-top: 12px; }}
    .risk-box {{ background: #2a1a1a; border-left: 4px solid #ef5350; border-radius: 0 8px 8px 0; padding: 16px; margin-top: 12px; }}
    .level-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }}
    .level-card {{ background: #1e1e2e; border-radius: 8px; padding: 12px 16px; }}
    .level-card .label {{ font-size: 12px; color: #888; }}
    .level-card .value {{ font-size: 16px; font-weight: 700; margin-top: 4px; }}
    .alert-item {{ background: #1e1e2e; border-radius: 4px; padding: 10px 14px; margin-bottom: 8px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 700; color: #fff; margin-right: 8px; }}
    .chart-wrap {{ background: #1e1e2e; border-radius: 8px; padding: 8px; margin-bottom: 16px; overflow: hidden; }}
    .news-item {{ border-bottom: 1px solid #2a2a3e; padding: 12px 0; }}
    .news-item a {{ color: #90caf9; text-decoration: none; font-weight: 600; }}
    .news-item a:hover {{ text-decoration: underline; }}
    .news-item p {{ color: #888; font-size: 13px; margin-top: 4px; line-height: 1.5; }}
    .indicators-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 12px; }}
    .indicator {{ background: #1e1e2e; border-radius: 8px; padding: 12px; text-align: center; }}
    .indicator .name {{ font-size: 12px; color: #888; }}
    .indicator .val {{ font-size: 18px; font-weight: 700; margin-top: 4px; }}
  </style>
</head>
<body>

<div class="header">
  <h1>{stock_code} {stock_name} <span class="stance-badge">{analysis.get('stance', '觀望')}</span></h1>
  <div class="meta">報告產生時間：{report_date} ｜ 資料截至：{last['date']} ｜ 收盤：<strong>{last['收盤價']}</strong></div>
</div>

<div class="container">

  <!-- AI 綜合研判 -->
  <div class="section">
    <h2>AI 綜合研判</h2>
    <div class="summary-box">{analysis.get('summary', '')}</div>
    <div class="suggestion-box">📌 <strong>操作建議：</strong>{analysis.get('suggestion', '')}</div>
    <div class="risk-box">⚠️ <strong>風險提醒：</strong>{analysis.get('risk', '')}</div>
    <div class="level-grid">
      <div class="level-card">
        <div class="label">支撐區</div>
        <div class="value" style="color:#26a69a">{support_str}</div>
      </div>
      <div class="level-card">
        <div class="label">壓力區</div>
        <div class="value" style="color:#ef5350">{resistance_str}</div>
      </div>
    </div>
    <div style="margin-top:12px; background:#1e1e2e; border-radius:8px; padding:16px;">
      <div style="color:#888;font-size:12px;margin-bottom:8px;">短線預測（3-5 個交易日）</div>
      <div>{analysis.get('short_term', '')}</div>
    </div>
  </div>

  <!-- 技術指標快覽 -->
  <div class="section">
    <h2>技術指標快覽</h2>
    <div class="indicators-grid">
      <div class="indicator"><div class="name">收盤價</div><div class="val">{last['收盤價']}</div></div>
      <div class="indicator"><div class="name">MA5</div><div class="val">{last['MA5']}</div></div>
      <div class="indicator"><div class="name">MA10</div><div class="val">{last['MA10']}</div></div>
      <div class="indicator"><div class="name">MA20</div><div class="val">{last['MA20']}</div></div>
      <div class="indicator"><div class="name">RSI(14)</div><div class="val" style="color:{'#ef5350' if last['RSI']>80 else '#26a69a' if last['RSI']<20 else '#e0e0e0'}">{last['RSI']}</div></div>
      <div class="indicator"><div class="name">K</div><div class="val">{last['K']}</div></div>
      <div class="indicator"><div class="name">D</div><div class="val">{last['D']}</div></div>
      <div class="indicator"><div class="name">DIF</div><div class="val">{last['DIF']}</div></div>
      <div class="indicator"><div class="name">MACD</div><div class="val">{last['MACD']}</div></div>
      <div class="indicator"><div class="name">OSC</div><div class="val" style="color:{'#ef5350' if last['OSC']>=0 else '#26a69a'}">{last['OSC']}</div></div>
      <div class="indicator"><div class="name">布林上軌</div><div class="val">{last['BB_upper']}</div></div>
      <div class="indicator"><div class="name">布林下軌</div><div class="val">{last['BB_lower']}</div></div>
    </div>
  </div>

  <!-- 技術警示 -->
  <div class="section">
    <h2>技術警示</h2>
    {alerts_html}
  </div>

  <!-- K 線圖 -->
  <div class="section">
    <h2>K 線圖（60 日）</h2>
    <div class="chart-wrap">{c1}</div>
  </div>

  <!-- KD 圖 -->
  <div class="section">
    <h2>KD 指標</h2>
    <div class="chart-wrap">{c2}</div>
  </div>

  <!-- MACD 圖 -->
  <div class="section">
    <h2>MACD</h2>
    <div class="chart-wrap">{c3}</div>
  </div>

  <!-- 三大法人 -->
  {"<div class='section'><h2>三大法人（60 日）</h2><div class='chart-wrap'>" + c4 + "</div></div>" if c4 else ""}

  <!-- 新聞 -->
  <div class="section">
    <h2>最新相關新聞</h2>
    {news_html}
  </div>

</div>
</body>
</html>"""

    return html


# ── 主程式 ──────────────────────────────────────────────

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if len(sys.argv) > 1:
        stock_code = sys.argv[1].strip()
    else:
        stock_code = input("請輸入股票代號（例如 2330）：").strip()

    if not stock_code:
        print("未輸入股票代號，結束。")
        sys.exit(1)

    print(f"\n=== 開始分析 {stock_code} ===")

    print("1/5 抓取 K 線資料...")
    df, stock_name = fetch_kline(stock_code)
    print(f"   取得 {len(df)} 個交易日（{df['date'].iloc[0]} ～ {df['date'].iloc[-1]}）")
    print(f"   股票名稱：{stock_name}")

    print("2/5 計算技術指標...")
    df = calc_indicators(df)

    print("3/5 抓取三大法人（60 日，約需 30-60 秒）...")
    trading_dates = list(df["date"])
    inst_df = fetch_institutional(stock_code, trading_dates)
    if not inst_df.empty:
        print(f"   取得法人資料 {len(inst_df)} 日")
    else:
        print("   法人資料無法取得，跳過。")

    print("4/5 抓取新聞...")
    news = fetch_news(stock_code, stock_name)
    print(f"   取得 {len(news)} 則新聞")

    alerts = generate_alerts(df)
    print(f"   技術警示：{len(alerts)} 項")

    print("5/5 AI 綜合分析（Claude Sonnet）...")
    analysis = ai_analysis(stock_code, stock_name, df, inst_df, alerts, news)
    print(f"   多空立場：{analysis.get('stance', 'N/A')}")

    print("\n生成 HTML 報告...")
    html = generate_html(stock_code, stock_name, df, inst_df, alerts, analysis, news)

    today_str = datetime.now().strftime("%Y%m%d")
    output_path = Path(__file__).parent / f"{stock_code}_report_{today_str}.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"報告已儲存：{output_path}")

    webbrowser.open(f"file://{output_path.resolve()}")
    print("已在瀏覽器開啟。\n")


if __name__ == "__main__":
    main()
