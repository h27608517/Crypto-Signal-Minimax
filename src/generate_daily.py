from __future__ import annotations

import html
import base64
import hashlib
import hmac
import json
import math
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
PERSONAL = PUBLIC / "personal"
PERSONAL_REPORTS = PERSONAL / "reports"
SOURCES_FILE = SRC / "sources.json"
PERSONAL_SOURCES_FILE = SRC / "personal_sources.json"
STABLE_ASSETS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USD", "EUR", "CNY"}
DEFAULT_CANDIDATES = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON"]
SYMBOL_ALIASES = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "SOL": ["sol", "solana"],
    "BNB": ["bnb", "binance"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin"],
    "ADA": ["ada", "cardano"],
    "AVAX": ["avax", "avalanche"],
    "LINK": ["link", "chainlink"],
    "TON": ["ton", "toncoin"],
}


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


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "crypto-daily-bot/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


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


def load_personal_config() -> dict[str, Any]:
    return json.loads(PERSONAL_SOURCES_FILE.read_text(encoding="utf-8"))


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


def group_news_by_interest(news: list[NewsItem], interests: list[dict[str, Any]]) -> dict[str, list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = {item["key"]: [] for item in interests}
    overflow: list[NewsItem] = []
    for news_item in news:
        haystack = f"{news_item.title} {news_item.summary}".lower()
        matched = False
        for interest in interests:
            keywords = [str(keyword).lower() for keyword in interest.get("keywords", [])]
            if any(keyword in haystack for keyword in keywords):
                grouped[interest["key"]].append(news_item)
                matched = True
        if not matched:
            overflow.append(news_item)

    for interest in interests:
        key = interest["key"]
        if len(grouped[key]) < 3:
            needed = 3 - len(grouped[key])
            grouped[key].extend(overflow[:needed])
        grouped[key] = grouped[key][:9]
    return grouped


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


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_headers(method: str, request_path: str, body: str = "") -> dict[str, str]:
    api_key = os.getenv("OKX_API_KEY", "")
    secret_key = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    timestamp = okx_timestamp()
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "crypto-daily-bot/1.0",
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    if os.getenv("OKX_SIMULATED_TRADING") == "1":
        headers["x-simulated-trading"] = "1"
    return headers


def okx_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    request_path = f"{path}?{query}" if query else path
    base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
    return fetch_json(f"{base_url}{request_path}", headers=okx_headers("GET", request_path))


def okx_public_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    request_path = f"{path}?{query}" if query else path
    base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
    return fetch_json(f"{base_url}{request_path}")


def as_float(value: Any) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def fetch_okx_portfolio() -> dict[str, Any]:
    required = ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        return {"configured": False, "error": f"Missing GitHub Secrets: {', '.join(missing)}", "balances": [], "positions": []}

    try:
        balance_result = okx_get("/api/v5/account/balance")
        positions_result = okx_get("/api/v5/account/positions")
    except Exception as exc:
        return {"configured": True, "error": str(exc), "balances": [], "positions": []}

    if balance_result.get("code") != "0":
        return {
            "configured": True,
            "error": f"OKX balance error {balance_result.get('code')}: {balance_result.get('msg')}",
            "balances": [],
            "positions": [],
        }
    if positions_result.get("code") != "0":
        return {
            "configured": True,
            "error": f"OKX positions error {positions_result.get('code')}: {positions_result.get('msg')}",
            "balances": [],
            "positions": [],
        }

    account = (balance_result.get("data") or [{}])[0]
    balances = []
    for item in account.get("details", []):
        eq_usd = as_float(item.get("eqUsd"))
        equity = as_float(item.get("eq"))
        cash_balance = as_float(item.get("cashBal"))
        if eq_usd <= 0 and equity <= 0 and cash_balance <= 0:
            continue
        balances.append(
            {
                "ccy": item.get("ccy", ""),
                "eq": item.get("eq", ""),
                "cashBal": item.get("cashBal", ""),
                "availEq": item.get("availEq", ""),
                "eqUsd": item.get("eqUsd", ""),
                "upl": item.get("upl", ""),
                "openAvgPx": item.get("openAvgPx", ""),
                "spotUpl": item.get("spotUpl", ""),
            }
        )
    balances.sort(key=lambda item: as_float(item.get("eqUsd")), reverse=True)

    positions = []
    for item in positions_result.get("data", []):
        if as_float(item.get("pos")) == 0:
            continue
        positions.append(
            {
                "instId": item.get("instId", ""),
                "posSide": item.get("posSide", ""),
                "pos": item.get("pos", ""),
                "avgPx": item.get("avgPx", ""),
                "markPx": item.get("markPx", ""),
                "upl": item.get("upl", ""),
                "uplRatio": item.get("uplRatio", ""),
                "lever": item.get("lever", ""),
                "mgnMode": item.get("mgnMode", ""),
                "liqPx": item.get("liqPx", ""),
                "ccy": item.get("ccy", ""),
            }
        )
    positions.sort(key=lambda item: abs(as_float(item.get("upl"))), reverse=True)

    return {
        "configured": True,
        "error": "",
        "totalEq": account.get("totalEq", ""),
        "adjEq": account.get("adjEq", ""),
        "imr": account.get("imr", ""),
        "mmr": account.get("mmr", ""),
        "balances": balances[:20],
        "positions": positions[:20],
    }


def held_spot_symbols(okx: dict[str, Any]) -> list[str]:
    symbols = []
    for item in okx.get("balances", []):
        ccy = str(item.get("ccy", "")).upper()
        if not ccy or ccy in STABLE_ASSETS:
            continue
        if as_float(item.get("eqUsd")) <= 1:
            continue
        symbols.append(ccy)
    return sorted(set(symbols))


def ticker_change_pct(ticker: dict[str, Any]) -> float:
    last = as_float(ticker.get("last"))
    open_24h = as_float(ticker.get("open24h")) or as_float(ticker.get("sodUtc0"))
    if last <= 0 or open_24h <= 0:
        return 0.0
    return (last - open_24h) / open_24h * 100


def fetch_okx_spot_tickers() -> list[dict[str, Any]]:
    try:
        result = okx_public_get("/api/v5/market/tickers", {"instType": "SPOT"})
    except Exception:
        return []
    if result.get("code") != "0":
        return []
    return result.get("data", [])


def top_candidate_symbols(limit: int = 10) -> list[str]:
    tickers = fetch_okx_spot_tickers()
    candidates = []
    for ticker in tickers:
        inst_id = str(ticker.get("instId", ""))
        if not inst_id.endswith("-USDT"):
            continue
        base = inst_id.split("-", 1)[0].upper()
        if base in STABLE_ASSETS or any(part in base for part in ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")):
            continue
        quote_volume = as_float(ticker.get("volCcy24h")) * as_float(ticker.get("last"))
        if quote_volume <= 0:
            quote_volume = as_float(ticker.get("vol24h"))
        change = ticker_change_pct(ticker)
        liquidity_score = math.log10(max(quote_volume, 1))
        momentum_score = max(min(change, 18), -12) * 0.18
        range_score = max(as_float(ticker.get("high24h")) - as_float(ticker.get("low24h")), 0) / max(as_float(ticker.get("last")), 1) * 8
        score = liquidity_score + momentum_score + min(range_score, 4)
        candidates.append((score, base))
    candidates.sort(reverse=True)
    symbols = [base for _, base in candidates[:limit]]
    if len(symbols) < limit:
        for symbol in DEFAULT_CANDIDATES:
            if symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= limit:
                break
    return symbols[:limit]


def analysis_universe(okx: dict[str, Any]) -> list[str]:
    symbols = held_spot_symbols(okx)
    for symbol in top_candidate_symbols(10):
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        symbols = DEFAULT_CANDIDATES[:]
    return symbols[:20]


def parse_okx_candles(rows: list[list[str]]) -> list[dict[str, float]]:
    candles = []
    for row in reversed(rows):
        if len(row) < 6:
            continue
        candles.append(
            {
                "ts": as_float(row[0]),
                "open": as_float(row[1]),
                "high": as_float(row[2]),
                "low": as_float(row[3]),
                "close": as_float(row[4]),
                "volume": as_float(row[5]),
            }
        )
    return candles


def fetch_okx_candles(inst_id: str, bar: str, limit: int = 120) -> list[dict[str, float]]:
    try:
        result = okx_public_get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
    except Exception:
        return []
    if result.get("code") != "0":
        return []
    return parse_okx_candles(result.get("data", []))


def aggregate_candles(candles: list[dict[str, float]], group_size: int) -> list[dict[str, float]]:
    grouped = []
    usable = len(candles) - (len(candles) % group_size)
    for index in range(0, usable, group_size):
        chunk = candles[index : index + group_size]
        grouped.append(
            {
                "ts": chunk[-1]["ts"],
                "open": chunk[0]["open"],
                "high": max(item["high"] for item in chunk),
                "low": min(item["low"] for item in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(item["volume"] for item in chunk),
            }
        )
    return grouped


def closes(candles: list[dict[str, float]]) -> list[float]:
    return [item["close"] for item in candles if item.get("close", 0) > 0]


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * multiplier + current * (1 - multiplier)
    return current


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for idx in range(1, len(values)):
        change = values[idx] - values[idx - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd(values: list[float]) -> dict[str, float]:
    if len(values) < 35:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0}
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = fast - slow
    macd_series = []
    for idx in range(26, len(values) + 1):
        window = values[:idx]
        macd_series.append(ema(window, 12) - ema(window, 26))
    signal = ema(macd_series, 9) if len(macd_series) >= 9 else 0.0
    return {"macd": line, "signal": signal, "hist": line - signal}


def atr(candles: list[dict[str, float]], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    ranges = []
    for idx in range(1, len(candles)):
        current = candles[idx]
        previous = candles[idx - 1]
        ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"]),
            )
        )
    return sum(ranges[-period:]) / period


def bollinger(values: list[float], period: int = 20) -> dict[str, float]:
    middle = sma(values, period)
    if len(values) < period or middle <= 0:
        return {"mid": 0.0, "upper": 0.0, "lower": 0.0, "position": 0.5}
    sample = values[-period:]
    variance = sum((value - middle) ** 2 for value in sample) / period
    deviation = math.sqrt(variance)
    upper = middle + deviation * 2
    lower = middle - deviation * 2
    position = (values[-1] - lower) / (upper - lower) if upper > lower else 0.5
    return {"mid": middle, "upper": upper, "lower": lower, "position": position}


def pct_change(values: list[float], periods: int) -> float:
    if len(values) <= periods or values[-periods - 1] <= 0:
        return 0.0
    return (values[-1] - values[-periods - 1]) / values[-periods - 1] * 100


def analyze_candles(candles: list[dict[str, float]], label: str) -> dict[str, Any]:
    values = closes(candles)
    if len(values) < 35:
        return {"timeframe": label, "error": "Not enough candle data"}

    close = values[-1]
    ema20 = ema(values, 20)
    ema50 = ema(values, 50)
    rsi14 = rsi(values, 14)
    macd_data = macd(values)
    atr14 = atr(candles, 14)
    bb = bollinger(values, 20)
    support = min(item["low"] for item in candles[-20:])
    resistance = max(item["high"] for item in candles[-20:])
    momentum = pct_change(values, 6)
    volume_now = candles[-1]["volume"]
    volume_avg = sum(item["volume"] for item in candles[-20:]) / 20
    volume_ratio = volume_now / volume_avg if volume_avg > 0 else 1

    score = 50.0
    signals = []
    if close > ema20:
        score += 7
        signals.append("price_above_ema20")
    else:
        score -= 7
        signals.append("price_below_ema20")
    if ema20 > ema50 > 0:
        score += 9
        signals.append("ema20_above_ema50")
    elif ema50 > 0:
        score -= 7
        signals.append("ema20_below_ema50")
    if 45 <= rsi14 <= 65:
        score += 6
        signals.append("healthy_rsi")
    elif rsi14 > 72:
        score -= 8
        signals.append("overbought_rsi")
    elif rsi14 < 35:
        score -= 3
        signals.append("weak_rsi")
    if macd_data["hist"] > 0:
        score += 7
        signals.append("positive_macd_hist")
    else:
        score -= 5
        signals.append("negative_macd_hist")
    if momentum > 0:
        score += min(momentum, 12) * 0.6
        signals.append("positive_momentum")
    else:
        score += max(momentum, -12) * 0.45
        signals.append("negative_momentum")
    if volume_ratio > 1.25 and momentum > 0:
        score += 5
        signals.append("volume_expansion")
    if bb["position"] > 0.9:
        score -= 4
        signals.append("near_upper_bollinger")
    elif bb["position"] < 0.2:
        score -= 2
        signals.append("near_lower_bollinger")

    score = max(0, min(100, score))
    if score >= 68:
        bias = "bullish"
    elif score <= 42:
        bias = "bearish"
    else:
        bias = "neutral"

    if bias == "bullish":
        expected_price = max(resistance, close + atr14 * 1.2)
        downside_price = max(support, close - atr14)
    elif bias == "bearish":
        expected_price = support
        downside_price = min(support, close - atr14 * 1.2)
    else:
        expected_price = close + (resistance - support) * 0.15
        downside_price = support

    return {
        "timeframe": label,
        "current_price": close,
        "score": round(score, 1),
        "bias": bias,
        "rsi14": round(rsi14, 2),
        "ema20": round(ema20, 8),
        "ema50": round(ema50, 8),
        "macd_hist": round(macd_data["hist"], 8),
        "atr14": round(atr14, 8),
        "support": round(support, 8),
        "resistance": round(resistance, 8),
        "momentum_pct": round(momentum, 2),
        "volume_ratio": round(volume_ratio, 2),
        "bollinger_position": round(bb["position"], 2),
        "expected_price": round(expected_price, 8),
        "downside_price": round(downside_price, 8),
        "signals": signals[:8],
    }


def timeframe_probability(score: float) -> int:
    distance = abs(score - 50)
    return int(max(45, min(78, 48 + distance * 0.8)))


def analyze_symbol(symbol: str) -> dict[str, Any]:
    inst_id = f"{symbol.upper()}-USDT"
    daily = fetch_okx_candles(inst_id, "1Dutc", 120)
    four_hour = fetch_okx_candles(inst_id, "4H", 160)
    eight_hour = aggregate_candles(four_hour, 2)
    daily_result = analyze_candles(daily, "24h")
    eight_hour_result = analyze_candles(eight_hour, "8h")
    scores = [item["score"] for item in (daily_result, eight_hour_result) if "score" in item]
    combined = sum(scores) / len(scores) if scores else 50
    current_price = 0.0
    for item in (eight_hour_result, daily_result):
        if item.get("current_price"):
            current_price = item["current_price"]
            break
    expected = eight_hour_result.get("expected_price") or daily_result.get("expected_price") or current_price
    horizon = "2-4 days" if eight_hour_result.get("bias") == "bullish" else "1-2 weeks"
    return {
        "symbol": symbol.upper(),
        "instId": inst_id,
        "current_price": current_price,
        "combined_score": round(combined, 1),
        "probability": timeframe_probability(combined),
        "expected_price": expected,
        "target_time": horizon,
        "timeframes": [eight_hour_result, daily_result],
    }


def fetch_technical_analysis(okx: dict[str, Any]) -> dict[str, Any]:
    symbols = analysis_universe(okx)
    results = []
    for symbol in symbols:
        results.append(analyze_symbol(symbol))
    held = set(held_spot_symbols(okx))
    for item in results:
        item["held"] = item["symbol"] in held
    results.sort(key=lambda item: (item["held"], item["combined_score"]), reverse=True)
    return {
        "source": "OKX public market data",
        "timeframes": ["8h synthesized from 4h candles", "24h from 1Dutc candles"],
        "symbols": symbols,
        "results": results,
    }


def related_news_by_symbol(news: list[NewsItem], symbols: list[str]) -> dict[str, list[dict[str, str]]]:
    related: dict[str, list[dict[str, str]]] = {}
    for symbol in symbols:
        aliases = SYMBOL_ALIASES.get(symbol.upper(), [symbol.lower()])
        matches = []
        for item in news:
            haystack = f"{item.title} {item.summary}".lower()
            if any(re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", haystack) for alias in aliases):
                matches.append(
                    {
                        "source": item.source,
                        "title": item.title,
                        "summary": item.summary,
                        "url": item.link,
                    }
                )
        related[symbol.upper()] = matches[:5]
    return related


def build_prompt(
    report_date: str,
    market: list[dict[str, Any]],
    news: list[NewsItem],
    okx: dict[str, Any],
    technical: dict[str, Any],
) -> str:
    news_lines = "\n".join(
        f"- [{item.source}] {item.title} ({item.published})\n  {item.summary}\n  Link: {item.link}"
        for item in news
    )
    market_json = json.dumps(market, ensure_ascii=False, indent=2)
    okx_json = json.dumps(okx, ensure_ascii=False, indent=2)
    technical_json = json.dumps(technical, ensure_ascii=False, indent=2)
    coin_news_json = json.dumps(related_news_by_symbol(news, technical.get("symbols", [])), ensure_ascii=False, indent=2)

    return f"""
You are a professional crypto market editor and spot trading analyst writing for Chinese readers.
Create a Chinese crypto daily report for {report_date} from the market data, OKX portfolio, technical analysis, and news below.

Rules:
1. Return strict JSON only. Do not return Markdown or code fences.
2. The JSON object must include:
   - title: string
   - brief: string, under 80 Chinese characters
   - market_summary: 3-5 strings
   - key_events: 5-8 objects with category, title, summary, impact, source_url
   - watchlist: 3-5 strings
   - risk_notes: 3-5 strings
   - trade_conclusions: 5-10 objects with symbol, action, current_price, expected_price, target_time, probability, reason, risk
3. trade_conclusions must prioritize held OKX spot assets first, then high-potential candidates.
4. Use the supplied 8h and 24h technical analysis. The user is a spot trader, not a high-frequency trader.
5. Do not invent facts. If something is uncertain, say it needs further observation.
6. Tone: clear, calm, concise, useful for a morning briefing.
7. action must be one of: buy, add, hold, reduce, sell, watch.

Market data:
{market_json}

OKX portfolio:
{okx_json}

Automated technical analysis:
{technical_json}

Coin-related source news:
{coin_news_json}

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
        "trade_conclusions": [],
        "watchlist": ["Key BTC and ETH levels", "ETF, regulation, and macro rate headlines", "Capital flows across major chains"],
        "risk_notes": ["Crypto assets are highly volatile", "News feeds can be delayed", "This report is informational and is not investment advice"],
    }


def build_personal_prompt(report_date: str, config: dict[str, Any], grouped: dict[str, list[NewsItem]]) -> str:
    payload = {}
    for interest in config.get("interests", []):
        key = interest["key"]
        payload[interest["label"]] = [
            {
                "source": item.source,
                "title": item.title,
                "summary": item.summary,
                "published": item.published,
                "url": item.link,
            }
            for item in grouped.get(key, [])[:9]
        ]
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
You are a concise personal intelligence editor writing in Chinese.
Create a personal daily intelligence report for {report_date}.

Rules:
1. Return strict JSON only. Do not return Markdown or code fences.
2. JSON must include:
   - title: string
   - brief: string, under 80 Chinese characters
   - sections: array of objects with category, events
3. Each sections[].events must contain 3-9 objects with title, summary, why_it_matters, source_url.
4. Categories must match the configured interest labels.
5. Do not invent facts. Use only the supplied source items.
6. Tone: sharp, calm, useful for a morning personal/work briefing.

Configured interests and source items:
{data_json}
""".strip()


def fallback_personal_analysis(report_date: str, config: dict[str, Any], grouped: dict[str, list[NewsItem]]) -> dict[str, Any]:
    sections = []
    for interest in config.get("interests", []):
        events = [
            {
                "title": item.title,
                "summary": item.summary or "Source did not provide a summary.",
                "why_it_matters": "Worth monitoring for personal or work context.",
                "source_url": item.link,
            }
            for item in grouped.get(interest["key"], [])[:9]
        ]
        sections.append({"category": interest["label"], "events": events})
    return {
        "title": f"{report_date} Personal Brief",
        "brief": "Personal source collection generated; MiniMax summary unavailable.",
        "sections": sections,
    }


def call_minimax_json(prompt: str) -> dict[str, Any] | None:
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
            {"role": "system", "content": "You are a careful intelligence editor. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
    )
    text = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("MiniMax response did not contain JSON")
    return json.loads(match.group(0))


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


def compact_num(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "-"
    if abs(number) >= 1:
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    return f"{number:.8f}".rstrip("0").rstrip(".")


def usd(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "-"


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def render_okx_section(okx: dict[str, Any]) -> str:
    if not okx.get("configured"):
        return f"""
    <section>
      <h2>OKX Portfolio</h2>
      <div class="notice">{esc(okx.get("error", "OKX API is not configured."))}</div>
    </section>"""
    if okx.get("error"):
        return f"""
    <section>
      <h2>OKX Portfolio</h2>
      <div class="notice">{esc(okx.get("error"))}</div>
    </section>"""

    summary_cards = f"""
        <div class="portfolio-card">
          <span>Total Equity</span>
          <b>{usd(okx.get("totalEq"))}</b>
        </div>
        <div class="portfolio-card">
          <span>Adjusted Equity</span>
          <b>{usd(okx.get("adjEq"))}</b>
        </div>
        <div class="portfolio-card">
          <span>Initial Margin</span>
          <b>{usd(okx.get("imr"))}</b>
        </div>
        <div class="portfolio-card">
          <span>Maintenance Margin</span>
          <b>{usd(okx.get("mmr"))}</b>
        </div>"""

    balance_rows = "".join(
        f"""
          <tr>
            <td>{esc(item.get("ccy"))}</td>
            <td>{compact_num(item.get("eq"))}</td>
            <td>{compact_num(item.get("cashBal"))}</td>
            <td>{usd(item.get("eqUsd"))}</td>
            <td>{compact_num(item.get("openAvgPx"))}</td>
            <td>{usd(item.get("spotUpl"))}</td>
            <td>{usd(item.get("upl"))}</td>
          </tr>"""
        for item in okx.get("balances", [])
    ) or '<tr><td colspan="7">No non-zero balances.</td></tr>'

    position_rows = "".join(
        f"""
          <tr>
            <td>{esc(item.get("instId"))}</td>
            <td>{esc(item.get("posSide"))}</td>
            <td>{compact_num(item.get("pos"))}</td>
            <td>{compact_num(item.get("avgPx"))}</td>
            <td>{compact_num(item.get("markPx"))}</td>
            <td>{usd(item.get("upl"))}</td>
            <td>{pct(as_float(item.get("uplRatio")) * 100)}</td>
          </tr>"""
        for item in okx.get("positions", [])
    ) or '<tr><td colspan="7">No open derivative positions.</td></tr>'

    return f"""
    <section>
      <h2>OKX Portfolio</h2>
      <div class="portfolio-summary">{summary_cards}</div>
      <div class="table-wrap">
        <h3>Asset Balances</h3>
        <table>
          <thead><tr><th>Asset</th><th>Equity</th><th>Cash</th><th>USD Value</th><th>Spot Avg Price</th><th>Spot Upnl</th><th>Position Upnl</th></tr></thead>
          <tbody>{balance_rows}</tbody>
        </table>
      </div>
      <div class="table-wrap">
        <h3>Open Positions</h3>
        <table>
          <thead><tr><th>Instrument</th><th>Side</th><th>Size</th><th>Avg Price</th><th>Mark Price</th><th>Position Upnl</th><th>PnL %</th></tr></thead>
          <tbody>{position_rows}</tbody>
        </table>
      </div>
    </section>"""


def fmt_price(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "-"
    if number >= 100:
        return f"${number:,.2f}"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.8f}".rstrip("0").rstrip(".")


def render_technical_section(analysis: dict[str, Any], technical: dict[str, Any]) -> str:
    conclusions = analysis.get("trade_conclusions") or []
    if conclusions:
        rows = "".join(
            f"""
          <tr>
            <td>{esc(item.get("symbol"))}</td>
            <td><span class="action action-{esc(str(item.get("action", "watch")).lower())}">{esc(item.get("action"))}</span></td>
            <td>{fmt_price(item.get("current_price"))}</td>
            <td>{fmt_price(item.get("expected_price"))}</td>
            <td>{esc(item.get("target_time"))}</td>
            <td>{esc(item.get("probability"))}%</td>
            <td>{esc(item.get("reason"))}</td>
            <td>{esc(item.get("risk"))}</td>
          </tr>"""
            for item in conclusions
        )
        return f"""
    <section>
      <h2>Technical Outlook</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Coin</th><th>Action</th><th>Now</th><th>Expected</th><th>Time</th><th>Probability</th><th>Reason</th><th>Risk</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""

    rows = "".join(
        f"""
          <tr>
            <td>{esc(item.get("symbol"))}</td>
            <td>{esc("held" if item.get("held") else "candidate")}</td>
            <td>{fmt_price(item.get("current_price"))}</td>
            <td>{fmt_price(item.get("expected_price"))}</td>
            <td>{esc(item.get("target_time"))}</td>
            <td>{esc(item.get("probability"))}%</td>
            <td>{esc(item.get("combined_score"))}</td>
          </tr>"""
        for item in technical.get("results", [])[:12]
    ) or '<tr><td colspan="7">No technical analysis available.</td></tr>'
    return f"""
    <section>
      <h2>Technical Outlook</h2>
      <div class="notice">MiniMax trade conclusions are unavailable; showing automated indicator scores.</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Coin</th><th>Type</th><th>Now</th><th>Expected</th><th>Time</th><th>Probability</th><th>Score</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""


def render_report(
    report_date: str,
    analysis: dict[str, Any],
    market: list[dict[str, Any]],
    okx: dict[str, Any],
    technical: dict[str, Any],
) -> str:
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
    okx_section = render_okx_section(okx)
    technical_section = render_technical_section(analysis, technical)

    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(analysis.get("title"))}</title>
  <style>
    :root {{ color-scheme: light; --ink: #1A1A1A; --muted: #8E8E93; --paper: #F5F7FA; --panel: #FFFFFF; --line: #E2E8F0; --accent: #FF3B30; --hot: #007AFF; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    a {{ color: inherit; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 34px 20px 56px; }}
    header {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, .7fr); gap: 28px; align-items: end; padding: 34px 0 28px; border-bottom: 1px dashed var(--line); }}
    .date {{ color: var(--accent); font-weight: 700; margin-bottom: 14px; text-transform: uppercase; }}
    h1 {{ font-size: clamp(34px, 6vw, 68px); line-height: .95; letter-spacing: 0; margin: 0; max-width: 820px; font-weight: 700; }}
    .brief {{ font-size: 20px; line-height: 1.65; color: var(--muted); margin: 0; font-weight: 300; }}
    section {{ padding: 28px 0; border-bottom: 1px dashed var(--line); }}
    h2 {{ font-size: 22px; margin: 0 0 18px; font-weight: 700; }}
    .market {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; }}
    .coin, .event {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 16px; box-shadow: none; }}
    .coin b {{ display: block; font-size: 15px; margin-bottom: 8px; }}
    .price {{ font-size: 22px; font-weight: 300; font-family: Inter, "Helvetica Neue", sans-serif; }}
    .change {{ margin-top: 8px; color: var(--muted); font-size: 14px; font-weight: 300; }}
    .up {{ color: var(--accent); }}
    .down {{ color: var(--hot); }}
    .summary-list {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; padding: 0; margin: 0; list-style: none; }}
    .summary-list li {{ background: var(--panel); border: 1px dashed var(--line); border-left: 2px solid var(--accent); padding: 12px 14px; line-height: 1.6; font-weight: 300; }}
    .events {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
    .tag {{ display: inline-block; color: var(--ink); font-size: 12px; font-weight: 700; margin-bottom: 10px; letter-spacing: .08em; text-transform: uppercase; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 3px 0; }}
    .event h3 {{ font-size: 18px; line-height: 1.35; margin: 0 0 10px; font-weight: 700; }}
    .event p {{ color: var(--muted); line-height: 1.65; margin: 0 0 12px; font-weight: 300; }}
    .impact {{ font-size: 14px; color: var(--ink); font-weight: 300; }}
    .columns {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px; }}
    .portfolio-summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 22px; }}
    .portfolio-card {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 14px 16px; box-shadow: none; }}
    .portfolio-card span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .portfolio-card b {{ font-size: 21px; font-weight: 300; font-family: Inter, "Helvetica Neue", sans-serif; }}
    .table-wrap {{ margin-top: 18px; overflow-x: auto; }}
    .table-wrap h3 {{ margin: 0 0 10px; font-size: 16px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 680px; background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; overflow: hidden; box-shadow: none; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px dashed var(--line); text-align: left; font-size: 14px; white-space: nowrap; font-weight: 300; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    .notice {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 16px; color: var(--muted); line-height: 1.6; font-weight: 300; }}
    .action {{ display: inline-block; min-width: 56px; padding: 4px 0; border-radius: 0; text-align: center; font-size: 12px; font-weight: 700; background: transparent; color: var(--ink); border-top: 1px solid currentColor; border-bottom: 1px solid currentColor; text-transform: uppercase; }}
    .action-buy, .action-add {{ color: var(--accent); }}
    .action-reduce, .action-sell {{ color: var(--hot); }}
    .plain-list {{ margin: 0; padding-left: 18px; color: var(--muted); line-height: 1.8; }}
    footer {{ padding-top: 24px; color: var(--muted); font-size: 13px; line-height: 1.6; font-weight: 300; }}
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
    {okx_section}
    {technical_section}
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


def render_personal_report(report_date: str, analysis: dict[str, Any]) -> str:
    sections_html = []
    for section in analysis.get("sections", []):
        events = "".join(
            f"""
        <article class="event">
          <span class="tag">{esc(section.get("category"))}</span>
          <h3>{esc(item.get("title"))}</h3>
          <p>{esc(item.get("summary"))}</p>
          <div class="impact">{esc(item.get("why_it_matters"))}</div>
          {f'<p><a href="{esc(item.get("source_url"))}" target="_blank" rel="noreferrer">Source</a></p>' if item.get("source_url") else ""}
        </article>"""
            for item in section.get("events", [])[:9]
        ) or '<div class="notice">No source items available for this topic.</div>'
        sections_html.append(
            f"""
    <section>
      <h2>{esc(section.get("category"))}</h2>
      <div class="events">{events}</div>
    </section>"""
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(analysis.get("title"))}</title>
  <style>
    :root {{ color-scheme: light; --ink: #1A1A1A; --muted: #8E8E93; --paper: #F5F7FA; --panel: #FFFFFF; --line: #E2E8F0; --accent: #FF3B30; --hot: #007AFF; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    a {{ color: inherit; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 34px 20px 56px; }}
    header {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, .7fr); gap: 28px; align-items: end; padding: 34px 0 28px; border-bottom: 1px dashed var(--line); }}
    .date {{ color: var(--accent); font-weight: 700; margin-bottom: 14px; text-transform: uppercase; }}
    h1 {{ font-size: clamp(34px, 6vw, 68px); line-height: .95; letter-spacing: 0; margin: 0; max-width: 820px; font-weight: 700; }}
    .brief {{ font-size: 20px; line-height: 1.65; color: var(--muted); margin: 0; font-weight: 300; }}
    section {{ padding: 28px 0; border-bottom: 1px dashed var(--line); }}
    h2 {{ font-size: 22px; margin: 0 0 18px; font-weight: 700; }}
    .events {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
    .event {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 16px; box-shadow: none; }}
    .tag {{ display: inline-block; color: var(--ink); font-size: 12px; font-weight: 700; margin-bottom: 10px; letter-spacing: .08em; text-transform: uppercase; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 3px 0; }}
    .event h3 {{ font-size: 18px; line-height: 1.35; margin: 0 0 10px; font-weight: 700; }}
    .event p {{ color: var(--muted); line-height: 1.65; margin: 0 0 12px; font-weight: 300; }}
    .impact {{ font-size: 14px; color: var(--ink); line-height: 1.55; font-weight: 300; }}
    .notice {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 16px; color: var(--muted); line-height: 1.6; font-weight: 300; }}
    footer {{ padding-top: 24px; color: var(--muted); font-size: 13px; line-height: 1.6; font-weight: 300; }}
    @media (max-width: 760px) {{ header {{ grid-template-columns: 1fr; }} .brief {{ font-size: 18px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <div class="date">{esc(report_date)} · Personal Brief</div>
        <h1>{esc(analysis.get("title"))}</h1>
      </div>
      <p class="brief">{esc(analysis.get("brief"))}</p>
    </header>
    {"".join(sections_html)}
    <footer>Generated at {generated_at}. This automated brief summarizes public information for personal awareness.</footer>
  </div>
</body>
</html>"""


def render_index(latest_report: str, analysis: dict[str, Any], personal_latest: str, personal_analysis: dict[str, Any]) -> str:
    reports = sorted(REPORTS.glob("crypto-*.html"), reverse=True)
    links = "\n".join(
        f'<a class="report-link" href="reports/{path.name}">{path.stem.replace("crypto-", "")}</a>'
        for path in reports[:30]
    )
    personal_reports = sorted(PERSONAL_REPORTS.glob("personal-*.html"), reverse=True)
    personal_links = "\n".join(
        f'<a class="report-link" href="personal/reports/{path.name}">{path.stem.replace("personal-", "")}</a>'
        for path in personal_reports[:30]
    )
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Intelligence</title>
  <style>
    :root {{ --ink: #1A1A1A; --muted: #8E8E93; --paper: #F5F7FA; --panel: #FFFFFF; --line: #E2E8F0; --accent: #FF3B30; --blue: #007AFF; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 64px 22px; }}
    h1 {{ font-size: clamp(36px, 7vw, 78px); line-height: .92; margin: 0 0 42px; letter-spacing: 0; font-weight: 700; }}
    h2 {{ font-size: 28px; margin: 0 0 16px; font-weight: 700; }}
    h3 {{ font-size: 14px; margin: 28px 0 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }}
    p {{ color: var(--muted); font-size: 17px; line-height: 1.7; font-weight: 300; }}
    .columns {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    .daily-panel {{ background: var(--panel); border: 1px dashed var(--line); border-radius: 2px; padding: 22px; min-height: 460px; box-shadow: none; }}
    .label {{ display: inline-block; color: var(--ink); font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 3px 0; margin-bottom: 18px; }}
    .latest {{ display: inline-flex; align-items: center; gap: 10px; margin: 20px 0 8px; padding: 10px 0; color: var(--ink); text-decoration: none; border-top: 1px solid currentColor; border-bottom: 1px solid currentColor; font-size: 13px; font-weight: 700; text-transform: uppercase; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 12px; }}
    .report-link {{ display: block; padding: 12px 10px; color: var(--ink); border: 1px dashed var(--line); border-radius: 2px; text-decoration: none; background: transparent; font-weight: 300; }}
    @media (max-width: 820px) {{ .columns {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1>Daily Intelligence</h1>
    <div class="columns">
      <section class="daily-panel">
        <span class="label">Crypto</span>
        <h2>Crypto Daily</h2>
        <p>{esc(analysis.get("brief", "Daily automated crypto market briefing."))}</p>
        <a class="latest" href="reports/{esc(latest_report)}">Latest report</a>
        <h3>Archive</h3>
        <div class="grid">{links}</div>
      </section>
      <section class="daily-panel">
        <span class="label">Personal</span>
        <h2>Personal Brief</h2>
        <p>{esc(personal_analysis.get("brief", "Daily personal and work intelligence briefing."))}</p>
        <a class="latest" href="personal/reports/{esc(personal_latest)}">Latest report</a>
        <h3>Archive</h3>
        <div class="grid">{personal_links}</div>
      </section>
    </div>
  </main>
</body>
</html>"""


def render_personal_archive(personal_latest: str, personal_analysis: dict[str, Any]) -> str:
    personal_reports = sorted(PERSONAL_REPORTS.glob("personal-*.html"), reverse=True)
    links = "\n".join(
        f'<a class="report-link" href="reports/{path.name}">{path.stem.replace("personal-", "")}</a>'
        for path in personal_reports[:30]
    )
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Personal Brief</title>
  <style>
    :root {{ --ink: #1A1A1A; --muted: #8E8E93; --paper: #F5F7FA; --panel: #FFFFFF; --line: #E2E8F0; --accent: #FF3B30; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    main {{ max-width: 920px; margin: 0 auto; padding: 64px 22px; }}
    .label {{ display: inline-block; color: var(--ink); font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; border-top: 1px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 3px 0; margin-bottom: 18px; }}
    h1 {{ font-size: clamp(36px, 7vw, 72px); line-height: .95; margin: 0 0 18px; letter-spacing: 0; font-weight: 700; }}
    p {{ color: var(--muted); font-size: 18px; line-height: 1.7; max-width: 680px; font-weight: 300; }}
    .latest {{ display: inline-flex; margin: 22px 0 34px; padding: 10px 0; color: var(--ink); text-decoration: none; border-top: 1px solid currentColor; border-bottom: 1px solid currentColor; font-size: 13px; font-weight: 700; text-transform: uppercase; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 18px; }}
    .report-link {{ display: block; padding: 14px 16px; color: var(--ink); border: 1px dashed var(--line); border-radius: 2px; text-decoration: none; background: transparent; font-weight: 300; }}
  </style>
</head>
<body>
  <main>
    <span class="label">Personal</span>
    <h1>Personal Brief</h1>
    <p>{esc(personal_analysis.get("brief", "Daily personal and work intelligence briefing."))}</p>
    <a class="latest" href="reports/{esc(personal_latest)}">Latest report</a>
    <h2>Archive</h2>
    <div class="grid">{links}</div>
  </main>
</body>
</html>"""


def render(
    report_date: str,
    analysis: dict[str, Any],
    market: list[dict[str, Any]],
    okx: dict[str, Any],
    technical: dict[str, Any],
    personal_analysis: dict[str, Any],
) -> None:
    PUBLIC.mkdir(exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    PERSONAL.mkdir(parents=True, exist_ok=True)
    PERSONAL_REPORTS.mkdir(parents=True, exist_ok=True)
    report_name = f"crypto-{report_date}.html"
    personal_report_name = f"personal-{report_date}.html"
    (REPORTS / report_name).write_text(render_report(report_date, analysis, market, okx, technical), encoding="utf-8")
    (PERSONAL_REPORTS / personal_report_name).write_text(render_personal_report(report_date, personal_analysis), encoding="utf-8")
    (PUBLIC / "index.html").write_text(render_index(report_name, analysis, personal_report_name, personal_analysis), encoding="utf-8")
    (PERSONAL / "index.html").write_text(render_personal_archive(personal_report_name, personal_analysis), encoding="utf-8")


def main() -> None:
    report_date = os.getenv("REPORT_DATE") or datetime.now().strftime("%Y-%m-%d")
    config = load_config()
    personal_config = load_personal_config()
    news = fetch_news(config)
    personal_news = fetch_news(personal_config)
    personal_grouped = group_news_by_interest(personal_news, personal_config.get("interests", []))
    market = fetch_market(config)
    okx = fetch_okx_portfolio()
    technical = fetch_technical_analysis(okx)
    prompt = build_prompt(report_date, market, news, okx, technical)
    personal_prompt = build_personal_prompt(report_date, personal_config, personal_grouped)
    try:
        analysis = call_minimax(prompt) or fallback_analysis(report_date, market, news)
    except Exception:
        analysis = fallback_analysis(report_date, market, news)
    try:
        personal_analysis = call_minimax_json(personal_prompt) or fallback_personal_analysis(report_date, personal_config, personal_grouped)
    except Exception:
        personal_analysis = fallback_personal_analysis(report_date, personal_config, personal_grouped)
    render(report_date, analysis, market, okx, technical, personal_analysis)
    print(f"Generated public/reports/crypto-{report_date}.html")


if __name__ == "__main__":
    main()
