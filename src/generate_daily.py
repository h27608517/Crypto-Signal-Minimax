from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PUBLIC = ROOT / "public"
REPORTS = PUBLIC / "reports"
SOURCES_FILE = SRC / "sources.json"


@dataclass
class NewsItem:
    title: str
    source: str
    link: str
    published: str
    summary: str


def clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:800]


def parse_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return value


def fetch_text(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "crypto-daily-bot/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def first_link(node: ET.Element) -> str:
    for child in node.iter():
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def load_config() -> dict[str, Any]:
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def parse_feed(xml_text: str, source_name: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    nodes = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    items: list[NewsItem] = []

    for node in nodes:
        title = clean_html(first_text(node, {"title"}))
        if not title:
            continue
        items.append(
            NewsItem(
                title=title,
                source=source_name,
                link=first_link(node),
                published=parse_date(first_text(node, {"published", "pubdate", "updated"})),
                summary=clean_html(first_text(node, {"description", "summary", "content", "encoded"})),
            )
        )
    return items


def fetch_news(config: dict[str, Any]) -> list[NewsItem]:
    max_items = int(os.getenv("MAX_ITEMS_PER_FEED", "8"))
    items: list[NewsItem] = []

    for feed in config["feeds"]:
        try:
            items.extend(parse_feed(fetch_text(feed["url"]), feed["name"])[:max_items])
        except Exception as exc:
            items.append(
                NewsItem(
                    title=f"{feed['name']} feed unavailable",
                    source=feed["name"],
                    link=feed["url"],
                    published="",
                    summary=str(exc),
                )
            )

    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        key = re.sub(r"\W+", "", item.title.lower())
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:40]


def fetch_market(config: dict[str, Any]) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "vs_currency": "usd",
            "ids": ",".join(config["coins"]),
            "order": "market_cap_desc",
            "price_change_percentage": "24h,7d",
        }
    )
    url = f"https://api.coingecko.com/api/v3/coins/markets?{query}"
    try:
        return json.loads(fetch_text(url))
    except Exception as exc:
        return [{"name": "Market data unavailable", "symbol": "-", "error": str(exc)}]


def build_prompt(report_date: str, market: list[dict[str, Any]], news: list[NewsItem]) -> str:
    news_lines = "\n".join(
        f"- [{item.source}] {item.title} ({item.published})\n  {item.summary}\n  Link: {item.link}"
        for item in news
    )
    market_json = json.dumps(market, ensure_ascii=False, indent=2)

    return f"""
You are a professional crypto market editor writing for Chinese readers.
Create a Chinese crypto daily report for {report_date} from the market data and news below.

Rules:
1. Return strict JSON only. Do not return Markdown or code fences.
2. The JSON object must include:
   - title: string
   - brief: string, under 80 Chinese characters
   - market_summary: 3-5 strings
   - key_events: 5-8 objects with category, title, summary, impact, source_url
   - watchlist: 3-5 strings
   - risk_notes: 3-5 strings
3. Do not invent facts. If something is uncertain, say it needs further observation.
4. Tone: clear, calm, concise, useful for a morning briefing.

Market data:
{market_json}

News:
{news_lines}
""".strip()


def fallback_analysis(report_date: str, market: list[dict[str, Any]], news: list[NewsItem]) -> dict[str, Any]:
    key_events = [
        {
            "category": item.source,
            "title": item.title,
            "summary": item.summary or "The source did not provide a summary. Open the source link for the full article.",
            "impact": "Needs further observation alongside price action and official updates.",
            "source_url": item.link,
        }
        for item in news[:8]
    ]
    movers = []
    for coin in market:
        if "error" in coin:
            continue
        movers.append(
            f"{coin.get('name')} is around ${coin.get('current_price'):,}, 24h change {coin.get('price_change_percentage_24h', 0):.2f}%."
        )

    return {
        "title": f"{report_date} Crypto Daily",
        "brief": "Data fetched successfully. MiniMax is not configured, so this is a basic fallback report.",
        "market_summary": movers[:5] or ["Market data is temporarily unavailable."],
        "key_events": key_events,
        "watchlist": ["Key BTC and ETH levels", "ETF, regulation, and macro rate headlines", "Capital flows across major chains"],
        "risk_notes": ["Crypto assets are highly volatile", "News feeds can be delayed", "This report is informational and is not investment advice"],
    }


def call_minimax(prompt: str) -> dict[str, Any] | None:
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key or OpenAI is None:
        return None

    model = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": "You are a careful financial news editor. Return only valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    text = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("MiniMax response did not contain JSON")
    return json.loads(match.group(0))


def money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "-"


def pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def render_report(report_date: str, analysis: dict[str, Any], market: list[dict[str, Any]]) -> str:
    market_cards = []
    for coin in market:
        if coin.get("error"):
            market_cards.append(
                f"""
          <div class="coin">
            <b>{esc(coin.get("name", "Market data unavailable"))}</b>
            <div class="price">-</div>
            <div class="change">{esc(coin.get("error"))}</div>
          </div>"""
            )
            continue
        change = coin.get("price_change_percentage_24h") or 0
        direction = "up" if change >= 0 else "down"
        market_cards.append(
            f"""
          <div class="coin">
            <b>{esc(coin.get("name"))} · {esc(str(coin.get("symbol", "")).upper())}</b>
            <div class="price">{money(coin.get("current_price"))}</div>
            <div class="change {direction}">24h {pct(change)}</div>
          </div>"""
        )

    summary_items = "".join(f"<li>{esc(item)}</li>" for item in analysis.get("market_summary", []))
    event_cards = []
    for item in analysis.get("key_events", []):
        source_url = item.get("source_url")
        source = f'<p><a href="{esc(source_url)}" target="_blank" rel="noreferrer">Source</a></p>' if source_url else ""
        event_cards.append(
            f"""
        <article class="event">
          <span class="tag">{esc(item.get("category"))}</span>
          <h3>{esc(item.get("title"))}</h3>
          <p>{esc(item.get("summary"))}</p>
          <div class="impact">{esc(item.get("impact"))}</div>
          {source}
        </article>"""
        )

    watchlist = "".join(f"<li>{esc(item)}</li>" for item in analysis.get("watchlist", []))
    risk_notes = "".join(f"<li>{esc(item)}</li>" for item in analysis.get("risk_notes", []))
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(analysis.get("title"))}</title>
  <style>
    :root {{ color-scheme: light; --ink: #171717; --muted: #62615d; --paper: #f6f0e6; --panel: #fffaf2; --line: #ded5c8; --accent: #0f766e; --hot: #c2410c; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    a {{ color: inherit; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 34px 20px 56px; }}
    header {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, .7fr); gap: 28px; align-items: end; padding: 34px 0 28px; border-bottom: 1px solid var(--line); }}
    .date {{ color: var(--accent); font-weight: 750; margin-bottom: 14px; }}
    h1 {{ font-size: clamp(34px, 6vw, 68px); line-height: .95; letter-spacing: 0; margin: 0; max-width: 820px; }}
    .brief {{ font-size: 20px; line-height: 1.65; color: var(--muted); margin: 0; }}
    section {{ padding: 28px 0; border-bottom: 1px solid var(--line); }}
    h2 {{ font-size: 22px; margin: 0 0 18px; }}
    .market {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; }}
    .coin, .event {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .coin b {{ display: block; font-size: 15px; margin-bottom: 8px; }}
    .price {{ font-size: 22px; font-weight: 780; }}
    .change {{ margin-top: 8px; color: var(--muted); font-size: 14px; }}
    .up {{ color: var(--accent); }}
    .down {{ color: var(--hot); }}
    .summary-list {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; padding: 0; margin: 0; list-style: none; }}
    .summary-list li {{ background: rgba(255, 250, 242, .72); border-left: 3px solid var(--accent); padding: 12px 14px; line-height: 1.6; }}
    .events {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
    .tag {{ display: inline-block; color: var(--accent); font-size: 13px; font-weight: 760; margin-bottom: 10px; }}
    .event h3 {{ font-size: 18px; line-height: 1.35; margin: 0 0 10px; }}
    .event p {{ color: var(--muted); line-height: 1.65; margin: 0 0 12px; }}
    .impact {{ font-size: 14px; color: var(--ink); }}
    .columns {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px; }}
    .plain-list {{ margin: 0; padding-left: 18px; color: var(--muted); line-height: 1.8; }}
    footer {{ padding-top: 24px; color: var(--muted); font-size: 13px; line-height: 1.6; }}
    @media (max-width: 760px) {{ header {{ grid-template-columns: 1fr; }} .brief {{ font-size: 18px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <div class="date">{esc(report_date)} · Crypto Daily</div>
        <h1>{esc(analysis.get("title"))}</h1>
      </div>
      <p class="brief">{esc(analysis.get("brief"))}</p>
    </header>
    <section><h2>Market Snapshot</h2><div class="market">{"".join(market_cards)}</div></section>
    <section><h2>Market in Brief</h2><ul class="summary-list">{summary_items}</ul></section>
    <section><h2>Key Events</h2><div class="events">{"".join(event_cards)}</div></section>
    <section class="columns">
      <div><h2>Watch Next</h2><ul class="plain-list">{watchlist}</ul></div>
      <div><h2>Risk Notes</h2><ul class="plain-list">{risk_notes}</ul></div>
    </section>
    <footer>Generated at {generated_at}. This automated report summarizes public information only and is not investment advice.</footer>
  </div>
</body>
</html>"""


def render_index(latest_report: str, analysis: dict[str, Any]) -> str:
    reports = sorted(REPORTS.glob("crypto-*.html"), reverse=True)
    links = "\n".join(
        f'<a class="report-link" href="reports/{path.name}">{path.stem.replace("crypto-", "")}</a>'
        for path in reports[:30]
    )
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Daily</title>
  <style>
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f3ec; color: #191919; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 64px 22px; }}
    h1 {{ font-size: clamp(36px, 7vw, 72px); line-height: .95; margin: 0 0 18px; letter-spacing: 0; }}
    p {{ color: #555; font-size: 18px; line-height: 1.7; max-width: 680px; }}
    .latest {{ display: inline-flex; align-items: center; gap: 10px; margin: 22px 0 34px; padding: 13px 18px; background: #111; color: white; text-decoration: none; border-radius: 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 18px; }}
    .report-link {{ display: block; padding: 14px 16px; color: #111; border: 1px solid #d8d0c4; border-radius: 8px; text-decoration: none; background: #fffaf2; }}
  </style>
</head>
<body>
  <main>
    <h1>Crypto Daily</h1>
    <p>{esc(analysis.get("brief", "Daily automated crypto market briefing."))}</p>
    <a class="latest" href="reports/{esc(latest_report)}">Latest report</a>
    <h2>Archive</h2>
    <div class="grid">{links}</div>
  </main>
</body>
</html>"""


def render(report_date: str, analysis: dict[str, Any], market: list[dict[str, Any]]) -> None:
    PUBLIC.mkdir(exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_name = f"crypto-{report_date}.html"
    (REPORTS / report_name).write_text(render_report(report_date, analysis, market), encoding="utf-8")
    (PUBLIC / "index.html").write_text(render_index(report_name, analysis), encoding="utf-8")


def main() -> None:
    report_date = os.getenv("REPORT_DATE") or datetime.now().strftime("%Y-%m-%d")
    config = load_config()
    news = fetch_news(config)
    market = fetch_market(config)
    prompt = build_prompt(report_date, market, news)
    analysis = call_minimax(prompt) or fallback_analysis(report_date, market, news)
    render(report_date, analysis, market)
    print(f"Generated public/reports/crypto-{report_date}.html")


if __name__ == "__main__":
    main()
