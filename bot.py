#!/usr/bin/env python3
"""
股票分析 Discord Bot
觸發：直接輸入股票代號（4-6 位數字）
      @台股分西施 任何問題  → 一般問答（帶上次分析背景）
"""

import asyncio
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import discord
from discord.ext import commands

# 載入 .env（同目錄）
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# 把同目錄加入 import path，才能 import stock_analyst
sys.path.insert(0, str(Path(__file__).parent))
from stock_analyst import (
    fetch_kline, fetch_institutional, fetch_news,
    calc_indicators, generate_alerts, ai_analysis,
)

# ── Discord Bot 設定 ─────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
executor = ThreadPoolExecutor(max_workers=2)

STANCE_COLOR = {"多方": 0x26a69a, "空方": 0xef5350, "觀望": 0xffd700}
STANCE_ICON  = {"多方": "🟢", "空方": "🔴", "觀望": "🟡"}

# 每個頻道最後一次股票分析的文字摘要，供追問時作為背景
channel_context: dict[int, str] = {}

STOCK_CODE_RE = re.compile(r"^\d{4,6}$")


# ── 分析主流程（在 thread 執行，避免阻塞 event loop） ────────

def run_analysis(stock_code: str) -> dict:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    df, stock_name = fetch_kline(stock_code)
    df = calc_indicators(df)
    inst_df = fetch_institutional(stock_code, list(df["date"]))
    news = fetch_news(stock_code, stock_name)
    alerts = generate_alerts(df)
    analysis = ai_analysis(stock_code, stock_name, df, inst_df, alerts, news)

    last = df.iloc[-1]
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "last": last,
        "inst_df": inst_df,
        "alerts": alerts,
        "analysis": analysis,
        "news": news,
    }


# ── 組 Discord Embed ──────────────────────────────────────

def build_embed(data: dict) -> discord.Embed:
    analysis = data["analysis"]
    last = data["last"]
    alerts = data["alerts"]
    news = data["news"]
    code = data["stock_code"]
    name = data["stock_name"]

    stance = analysis.get("stance", "觀望")
    icon = STANCE_ICON.get(stance, "🟡")
    color = STANCE_COLOR.get(stance, 0xffd700)

    embed = discord.Embed(
        title=f"📊 {code} {name}　{icon} {stance}",
        description=analysis.get("summary", ""),
        color=color,
    )

    # 收盤 + 均線
    embed.add_field(
        name="📈 收盤資訊",
        value=(
            f"收盤 **{last['收盤價']}**　"
            f"MA5 `{last['MA5']}`　"
            f"MA10 `{last['MA10']}`　"
            f"MA20 `{last['MA20']}`"
        ),
        inline=False,
    )

    # 技術指標
    embed.add_field(
        name="🔢 技術指標",
        value=(
            f"RSI `{last['RSI']}`　"
            f"K `{last['K']}`　D `{last['D']}`　"
            f"DIF `{last['DIF']}`　OSC `{last['OSC']}`"
        ),
        inline=False,
    )

    # 支撐壓力
    kl = analysis.get("key_levels", {})
    support_str = " / ".join(str(s) for s in kl.get("support", [])) or "—"
    resist_str  = " / ".join(str(r) for r in kl.get("resistance", [])) or "—"
    embed.add_field(
        name="🗺️ 支撐 ／ 壓力",
        value=f"🔵 支撐：**{support_str}**　🔴 壓力：**{resist_str}**",
        inline=False,
    )

    # 短線預測
    embed.add_field(
        name="🔮 短線預測（3-5 日）",
        value=analysis.get("short_term", "—"),
        inline=False,
    )

    # 操作建議（結構化進場/停損/目標）
    entry      = analysis.get("entry")
    stop_loss  = analysis.get("stop_loss")
    target     = analysis.get("target")
    if entry and stop_loss and target:
        try:
            e, s, t = float(entry), float(stop_loss), float(target)
            if s < e < t:  # 合法做多：停損 < 進場 < 目標
                ratio = round((t - e) / (e - s), 1)
                suggestion_val = (
                    f"進場 **${entry}** ｜ 停損 **${stop_loss}** ｜ 目標 **${target}**　"
                    f"風報 1:{ratio}"
                )
            else:
                suggestion_val = "—"
        except Exception:
            suggestion_val = "—"
    else:
        suggestion_val = "—"
    embed.add_field(
        name="📌 操作建議",
        value=suggestion_val,
        inline=False,
    )

    # 風險提醒
    embed.add_field(
        name="⚠️ 風險提醒",
        value=analysis.get("risk", "—"),
        inline=False,
    )

    # 技術警示
    if alerts:
        priority_icon = {1: "🔴", 2: "🟡", 3: "🔵"}
        alert_lines = [f"{priority_icon.get(a['priority'], '▪')} **{a['type']}**：{a['msg']}" for a in alerts[:5]]
        embed.add_field(
            name="🚨 技術警示",
            value="\n".join(alert_lines),
            inline=False,
        )

    # 近期新聞
    if news:
        news_lines = [f"• [{n['title']}]({n['url']})" for n in news[:3] if n.get("title")]
        if news_lines:
            embed.add_field(
                name="📰 近期新聞",
                value="\n".join(news_lines),
                inline=False,
            )

    embed.set_footer(text=f"資料截至 {last['date']}　法人資料 60 日　by stock-analyst bot")
    return embed


# ── 分析核心（共用） ──────────────────────────────────────

async def do_analyze(message, stock_code: str):
    stock_code = stock_code.strip().upper()
    waiting = await message.reply(f"🔍 **{stock_code}** 分析中，約需 60 秒，請稍候...")

    loop = asyncio.get_event_loop()
    try:
        data = await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: run_analysis(stock_code)),
            timeout=300,
        )
        embed = build_embed(data)
        await waiting.edit(content="", embed=embed)

        # 儲存分析摘要供後續 @mention 追問使用
        analysis = data["analysis"]
        last = data["last"]
        channel_context[message.channel.id] = (
            f"股票：{data['stock_code']} {data['stock_name']}\n"
            f"收盤：{last['收盤價']}　MA5：{last['MA5']}　MA20：{last['MA20']}\n"
            f"RSI：{last['RSI']}　KD：{last['K']}/{last['D']}\n"
            f"研判立場：{analysis.get('stance', '—')}\n"
            f"摘要：{analysis.get('summary', '—')}\n"
            f"短線預測：{analysis.get('short_term', '—')}\n"
            f"操作建議 — 進場：{analysis.get('entry', '—')}　"
            f"停損：{analysis.get('stop_loss', '—')}　目標：{analysis.get('target', '—')}\n"
            f"風險提醒：{analysis.get('risk', '—')}"
        )

    except asyncio.TimeoutError:
        await waiting.edit(content="❌ 分析逾時（超過 5 分鐘），請稍後再試")
    except ValueError as e:
        await waiting.edit(content=f"❌ {e}")
    except Exception as e:
        await waiting.edit(content=f"❌ 分析失敗：{e}")


# ── 一般問答（@mention 觸發） ─────────────────────────────

async def do_general_qa(message: discord.Message, question: str):
    ctx_text = channel_context.get(message.channel.id, "")

    system_prompt = (
        "你是台股分西施，專業的台股投資分析助理。"
        "用繁體中文回答，語氣自然、簡潔，像朋友聊天。"
        "若問題與股票投資無關，也可正常回答。"
    )
    user_content = (
        f"參考背景（頻道最近分析的股票）：\n{ctx_text}\n\n用戶問題：{question}"
        if ctx_text else question
    )

    thinking_msg = await message.reply("🤔 思考中...")

    def call_groq():
        from groq import Groq
        client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.7,
            max_tokens=800,
        )
        return resp.choices[0].message.content.strip()

    loop = asyncio.get_event_loop()
    try:
        answer = await asyncio.wait_for(
            loop.run_in_executor(executor, call_groq),
            timeout=60,
        )
        await thinking_msg.edit(content=answer)
    except asyncio.TimeoutError:
        await thinking_msg.edit(content="❌ 回應逾時，請再試一次")
    except Exception as e:
        await thinking_msg.edit(content=f"❌ 發生錯誤：{e}")


# ── 監聽所有訊息 ──────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    content = message.content.strip()

    # @mention → 一般問答（帶分析背景）
    if bot.user in message.mentions and not message.mention_everyone:
        question = re.sub(r"<@!?\d+>", "", content).strip()
        if question:
            await do_general_qa(message, question)
        else:
            await message.reply("有什麼問題都可以問我！股票代號直接輸入，其他問題 @我就好 😊")
        return

    # 純數字 → 股票分析
    if STOCK_CODE_RE.match(content):
        await do_analyze(message, content)
        return

    await bot.process_commands(message)


# ── 指令：!analyze（保留相容） ────────────────────────────

@bot.command(name="analyze", aliases=["a", "分析"])
async def analyze(ctx, stock_code: str = None):
    if not stock_code:
        await ctx.reply("請直接輸入股票代號，例如：`2330`")
        return
    await do_analyze(ctx.message, stock_code)


@bot.command(name="help_analyze", aliases=["ah"])
async def help_analyze(ctx):
    embed = discord.Embed(
        title="📖 股票分析 Bot 使用說明",
        color=0x90caf9,
    )
    embed.add_field(name="股票分析", value="直接輸入股票代號，例如：`2330`", inline=False)
    embed.add_field(name="追問 / 一般問答", value="`@台股分西施 被套了怎辦？`\n分析後可追問，或問任何投資問題", inline=False)
    embed.add_field(name="舊指令（仍可用）", value="`!analyze 2330`　或　`!a 2330`", inline=False)
    embed.add_field(name="分析內容", value="K 線 60 日、三大法人 60 日、Firecrawl 新聞、Groq AI 綜合研判", inline=False)
    await ctx.reply(embed=embed)


# ── 啟動 ─────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot 上線：{bot.user}（{bot.user.id}）")
    print("   股票代號直接輸入，@mention 觸發一般問答")


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("❌ 請在 .env 填入 DISCORD_BOT_TOKEN")
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    main()
