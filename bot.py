#!/usr/bin/env python3
"""
股票分析 Discord Bot
觸發：直接輸入股票代號（4-6 位數字）
      @台股分西施 任何問題       → 新對話（帶上次分析背景）
      回覆 Bot 的訊息            → 帶歷史記錄繼續對話
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

# 每個頻道最後一次股票分析的文字摘要（給 Groq 閱讀用）
channel_context: dict[int, str] = {}
# 每個頻道最後一次股票分析的結構化數字（權威數據，禁止 Groq 自行替換）
channel_data: dict[int, dict] = {}

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

    embed.add_field(
        name="🔢 技術指標",
        value=(
            f"RSI `{last['RSI']}`　"
            f"K `{last['K']}`　D `{last['D']}`　"
            f"DIF `{last['DIF']}`　OSC `{last['OSC']}`"
        ),
        inline=False,
    )

    kl = analysis.get("key_levels", {})
    support_str = " / ".join(str(s) for s in kl.get("support", [])) or "—"
    resist_str  = " / ".join(str(r) for r in kl.get("resistance", [])) or "—"
    embed.add_field(
        name="🗺️ 支撐 ／ 壓力",
        value=f"🔵 支撐：**{support_str}**　🔴 壓力：**{resist_str}**",
        inline=False,
    )

    embed.add_field(
        name="🔮 短線預測（3-5 日）",
        value=analysis.get("short_term", "—"),
        inline=False,
    )

    entry      = analysis.get("entry")
    stop_loss  = analysis.get("stop_loss")
    target     = analysis.get("target")
    if entry and stop_loss and target:
        try:
            e, s, t = float(entry), float(stop_loss), float(target)
            if s < e < t:
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
    embed.add_field(name="📌 操作建議", value=suggestion_val, inline=False)

    embed.add_field(name="⚠️ 風險提醒", value=analysis.get("risk", "—"), inline=False)

    if alerts:
        priority_icon = {1: "🔴", 2: "🟡", 3: "🔵"}
        alert_lines = [
            f"{priority_icon.get(a['priority'], '▪')} **{a['type']}**：{a['msg']}"
            for a in alerts[:5]
        ]
        embed.add_field(name="🚨 技術警示", value="\n".join(alert_lines), inline=False)

    if news:
        news_lines = [f"• [{n['title']}]({n['url']})" for n in news[:3] if n.get("title")]
        if news_lines:
            embed.add_field(name="📰 近期新聞", value="\n".join(news_lines), inline=False)

    embed.set_footer(text=f"資料截至 {last['date']}　法人資料 60 日　by stock-analyst bot")
    return embed


# ── 分析核心 ──────────────────────────────────────────────

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

        # 儲存分析結果供後續追問使用
        analysis = data["analysis"]
        last = data["last"]
        kl = analysis.get("key_levels", {})

        channel_data[message.channel.id] = {
            "code": data["stock_code"],
            "name": data["stock_name"],
            "close": last["收盤價"],
            "ma5": last["MA5"],
            "ma10": last["MA10"],
            "ma20": last["MA20"],
            "rsi": last["RSI"],
            "k": last["K"],
            "d": last["D"],
            "dif": last["DIF"],
            "osc": last["OSC"],
            "support": kl.get("support", []),
            "resistance": kl.get("resistance", []),
            "entry": analysis.get("entry", "—"),
            "stop_loss": analysis.get("stop_loss", "—"),
            "target": analysis.get("target", "—"),
            "stance": analysis.get("stance", "觀望"),
            "summary": analysis.get("summary", ""),
            "short_term": analysis.get("short_term", ""),
            "risk": analysis.get("risk", ""),
        }

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


# ── 建立回覆串的對話歷史 ─────────────────────────────────

async def build_reply_history(message: discord.Message, max_depth: int = 8) -> list[dict]:
    """往上追蹤回覆鏈，組成 Groq messages 格式的對話歷史。"""
    history = []
    current = message

    for _ in range(max_depth):
        if not current.reference:
            break
        try:
            ref = current.reference.resolved
            if ref is None:
                ref = await current.channel.fetch_message(current.reference.message_id)
        except Exception:
            break

        role = "assistant" if ref.author.bot else "user"
        text = re.sub(r"<@!?\d+>", "", ref.content).strip()
        if text:
            history.insert(0, {"role": role, "content": text})
        current = ref

    return history


# ── Firecrawl 網路搜尋 ───────────────────────────────────

def web_search(query: str, limit: int = 5) -> list[dict]:
    """用 Firecrawl 搜尋，回傳 [{title, url, snippet}]。"""
    import requests
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "limit": limit},
            timeout=15,
        )
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in resp.json().get("data", [])
        ]
    except Exception as e:
        print(f"  網路搜尋失敗：{e}")
        return []


def scrape_url(url: str, max_chars: int = 1000) -> str:
    """用 Firecrawl scrape 抓網頁全文，回傳截斷後的 markdown。"""
    import requests
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"]},
            timeout=20,
        )
        content = resp.json().get("data", {}).get("markdown", "")
        return content[:max_chars] if content else ""
    except Exception as e:
        print(f"  Scrape 失敗 {url}：{e}")
        return ""


# ── 對話問答（@mention 或回覆串） ────────────────────────

def build_data_block(cd: dict) -> str:
    """把 channel_data 組成給 Groq 的鎖定數字區塊。"""
    support_str = " / ".join(str(s) for s in cd["support"]) or "—"
    resist_str  = " / ".join(str(r) for r in cd["resistance"]) or "—"
    return (
        f"=== 真實技術數據（權威，禁止更動或替換） ===\n"
        f"股票：{cd['code']} {cd['name']}\n"
        f"收盤價：{cd['close']}（唯一正確現價）\n"
        f"MA5：{cd['ma5']}　MA10：{cd['ma10']}　MA20：{cd['ma20']}\n"
        f"RSI：{cd['rsi']}　K：{cd['k']}　D：{cd['d']}\n"
        f"DIF：{cd['dif']}　OSC：{cd['osc']}\n"
        f"支撐：{support_str}\n"
        f"壓力：{resist_str}\n"
        f"建議進場：{cd['entry']}　停損：{cd['stop_loss']}　目標：{cd['target']}\n"
        f"研判立場：{cd['stance']}\n"
        f"AI 摘要：{cd['summary']}\n"
        f"短線預測：{cd['short_term']}\n"
        f"風險提醒：{cd['risk']}\n"
        f"=== 以上數字為唯一依據，不可自行推算或替換 ==="
    )


async def do_chat(message: discord.Message, question: str, history: list[dict] = None):
    cd = channel_data.get(message.channel.id)

    thinking_msg = await message.reply("🔍 搜尋資料中...")

    loop = asyncio.get_event_loop()
    search_query = f"{cd['code']} {cd['name']} {question}" if cd else question
    search_results = await loop.run_in_executor(executor, lambda: web_search(search_query))

    # 並行抓前 2 篇全文
    await thinking_msg.edit(content="📖 閱讀文章中...")
    top_urls = [r["url"] for r in search_results[:2] if r.get("url")]
    scraped = await asyncio.gather(
        *[loop.run_in_executor(executor, scrape_url, url) for url in top_urls]
    )
    for i, content in enumerate(scraped):
        if content:
            search_results[i]["full_content"] = content

    await thinking_msg.edit(content="🤔 分析中...")

    if cd:
        system_prompt = (
            "你是台股分西施，作風積極、直接的台股投資分析師，不說廢話。\n"
            "用繁體中文回答，語氣像老手交流，簡潔有力。\n\n"
            "回答規則：\n"
            "1. 直接給結論，禁止說「你可以考慮」「視情況而定」「根據個人風險」等模糊廢話。\n"
            "2. 所有股價、技術指標數字必須完全來自下方【真實技術數據】，禁止自行推算或替換。\n"
            "3. 財報、基本面等非價格資訊可從【網路搜尋結果】取用，不可捏造。\n"
            "4. 不要列出網站或來源連結，來源自動附在後面。\n\n"
            "回覆時必須嚴格遵守以下輸出格式（每個區塊各佔一行）：\n"
            "【立場】多方 / 空方 / 觀望\n"
            "【操作】加碼 $X ｜ 停損 $X ｜ 目標 $X（視問題情境調整，被套則優先給停損/攤平建議）\n"
            "【技術】RSI X，KD X/X，站上或跌破 MAxx（$X）\n"
            "【說明】一到兩句話點出關鍵判斷依據\n\n"
            + build_data_block(cd)
        )
    else:
        system_prompt = (
            "你是台股分西施，專業的台股投資分析助理。\n"
            "用繁體中文回答，語氣自然、簡潔，像朋友聊天。\n"
            "直接回答問題，不要說「根據搜尋結果」等前言。\n"
            "不要列出網站或來源連結，來源自動附在後面。\n"
            "若搜尋結果不足以回答，直接說「目前找不到相關資料」。\n"
        )

    if search_results:
        lines = []
        for i, r in enumerate(search_results):
            lines.append(f"{i+1}. {r['title']}\n   摘要：{r['snippet']}\n   來源：{r['url']}")
            if r.get("full_content"):
                lines.append(f"   全文節錄：\n{r['full_content']}")
        system_prompt += f"\n\n【網路搜尋結果】\n" + "\n".join(lines)
    else:
        system_prompt += "\n\n（本次未取得網路搜尋結果，請如實告知用戶。）"

    # 沒有 channel_data 但有文字摘要時，補充給 else 分支
    if not cd and not history:
        ctx_text = channel_context.get(message.channel.id, "")
        if ctx_text:
            system_prompt += f"\n\n【頻道最近分析摘要】\n{ctx_text}"


    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    def call_groq():
        from groq import Groq
        client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()

    try:
        answer = await asyncio.wait_for(
            loop.run_in_executor(executor, call_groq),
            timeout=60,
        )
        if search_results:
            sources = "\n".join(
                f"{i+1}. [{r['title'] or r['url']}](<{r['url']}>)"
                for i, r in enumerate(search_results)
                if r.get("url")
            )
            sources_block = f"\n\n📎 **資料來源**\n{sources}"
            max_len = 2000 - len(sources_block)
            if len(answer) > max_len:
                answer = answer[:max_len - 3] + "..."
            answer += sources_block
        elif len(answer) > 2000:
            answer = answer[:1997] + "..."
        await thinking_msg.edit(content=answer, suppress=True)
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

    # 回覆 Bot 的訊息 → 帶歷史記錄繼續對話
    if message.reference:
        try:
            ref = message.reference.resolved or await message.channel.fetch_message(
                message.reference.message_id
            )
            if ref.author == bot.user:
                history = await build_reply_history(message)
                question = re.sub(r"<@!?\d+>", "", content).strip()
                if question:
                    await do_chat(message, question, history)
                return
        except Exception:
            pass

    # @mention → 新對話（帶分析背景）
    if bot.user in message.mentions and not message.mention_everyone:
        question = re.sub(r"<@!?\d+>", "", content).strip()
        if question:
            await do_chat(message, question)
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
    embed = discord.Embed(title="📖 股票分析 Bot 使用說明", color=0x90caf9)
    embed.add_field(name="股票分析", value="直接輸入股票代號，例如：`2330`", inline=False)
    embed.add_field(
        name="問問題 / 追問",
        value=(
            "`@台股分西施 被套了怎辦？` → 新對話\n"
            "直接**回覆** Bot 的訊息 → 繼續上下文對話"
        ),
        inline=False,
    )
    embed.add_field(name="舊指令（仍可用）", value="`!analyze 2330`　或　`!a 2330`", inline=False)
    embed.add_field(
        name="分析內容",
        value="K 線 60 日、三大法人 60 日、Firecrawl 新聞、Groq AI 綜合研判",
        inline=False,
    )
    await ctx.reply(embed=embed)


# ── 啟動 ─────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot 上線：{bot.user}（{bot.user.id}）")
    print("   股票代號直接輸入；@mention 或回覆 Bot 觸發對話")


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("❌ 請在 .env 填入 DISCORD_BOT_TOKEN")
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    main()
