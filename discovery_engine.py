#!/usr/bin/env python3
"""Active discovery engine for the AI stock-pool dashboard.

The first MVP intentionally stays conservative:
- it reads the existing stock pools;
- fetches official/blog RSS, Google News RSS, arXiv Atom, and live quote data;
- writes research tables and a Markdown report;
- never edits the formal stock-pool source CSVs.
"""

from __future__ import annotations

import cn_extra_sources
import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional runtime dependency.
    curl_requests = None


WEB_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEB_DIR.parent
POOL_FILE = WEB_DIR / "stock-pool.csv"
US_SOURCE_FILE = ROOT_DIR / "美股股票池.csv"
A_SOURCE_FILE = ROOT_DIR / "A股映射股票池.csv"
ALIASES_FILE = WEB_DIR / "mapping-aliases.json"
SIGNALS_FILE = WEB_DIR / "discovery-signals.csv"
PAPERS_FILE = WEB_DIR / "arxiv-papers.csv"
CANDIDATES_FILE = WEB_DIR / "discovery-candidates.csv"
HISTORY_FILE = WEB_DIR / "discovery-history.csv"
STATUS_FILE = WEB_DIR / "discovery-status.json"
REPORTS_DIR = WEB_DIR / "reports"

USER_AGENT = "StockDiscoveryEngine/0.1 (research dashboard; contact=local)"
QUOTE_API_URL = "https://pool.xwlab.win/api/quotes"


SIGNAL_FIELDS = [
    "signal_id",
    "date",
    "source_type",
    "source_name",
    "source_url",
    "title",
    "summary",
    "theme",
    "tickers_mentioned",
    "mapped_tickers",
    "mapped_a_shares",
    "evidence_strength",
    "confidence",
    "created_at",
]

PAPER_FIELDS = [
    "arxiv_id",
    "published",
    "updated",
    "title",
    "authors",
    "primary_category",
    "categories",
    "abstract",
    "abs_url",
    "pdf_url",
    "topic",
    "technical_signal",
    "bottleneck",
    "mapped_themes",
    "mapped_tickers",
    "mapped_a_shares",
    "paper_signal_score",
    "investment_readthrough",
    "risk_note",
]

CANDIDATE_FIELDS = [
    "run_date",
    "ticker",
    "company",
    "market",
    "pool_presence",
    "theme",
    "chain_layer",
    "mapped_us",
    "why_now",
    "source_strength_score",
    "exposure_purity_score",
    "paper_signal_score",
    "novelty_score",
    "sentiment_score",
    "market_setup_score",
    "risk_score",
    "total_score",
    "recommendation",
    "review_status",
    "supporting_signals",
    "supporting_papers",
    "price",
    "change_percent",
    "quote_timestamp",
    "notes",
]

HISTORY_FIELDS = [
    "date",
    "pool_size",
    "quotes_requested",
    "quotes_received",
    "missing_quotes",
    "extra_quotes",
    "usable_quotes",
    "signals",
    "arxiv_papers",
    "candidates",
    "observe_count",
    "observe_tickers",
    "report_href",
]


COMPANY_OVERRIDES = {
    "AMD": ("Advanced Micro Devices", "美股"),
    "HPE": ("Hewlett Packard Enterprise", "美股"),
    "SMCI": ("Super Micro Computer", "美股"),
    "ALAB": ("Astera Labs", "美股"),
    "CSCO": ("Cisco Systems", "美股"),
    "NOK": ("Nokia", "美股"),
    "NTAP": ("NetApp", "美股"),
    "PSTG": ("Pure Storage", "美股"),
    "AMKR": ("Amkor Technology", "美股"),
    "Q": ("Qnity Electronics", "美股"),
    "ENTG": ("Entegris", "美股"),
    "AMAT": ("Applied Materials", "美股"),
    "LRCX": ("Lam Research", "美股"),
    "KLAC": ("KLA", "美股"),
    "CAT": ("Caterpillar", "美股"),
    "CEG": ("Constellation Energy", "美股"),
    "NEE": ("NextEra Energy", "美股"),
    "ISRG": ("Intuitive Surgical", "美股"),
    "TER": ("Teradyne", "美股"),
    "ROK": ("Rockwell Automation", "美股"),
    "SU.PA": ("Schneider Electric", "欧洲"),
    "MOD": ("Modine Manufacturing", "美股"),
}


THEME_RULES = [
    {
        "theme": "Rack-scale AI factory",
        "keywords": [
            "ai factory",
            "ai factories",
            "rubin",
            "blackwell",
            "nvl72",
            "gb300",
            "rack-scale",
            "rack scale",
            "mgx",
            "ai pod",
            "ai infrastructure",
        ],
        "tickers": ["NVDA", "DELL", "HPE", "SMCI", "VRT", "ETN", "ANET", "MRVL", "CRDO"],
        "bottleneck": "compute/network/power/cooling",
        "default_layer": "中游",
    },
    {
        "theme": "CPO and optical interconnect",
        "keywords": [
            "cpo",
            "co-packaged optics",
            "co packaged optics",
            "optical interconnect",
            "silicon photonics",
            "photonics",
            "1.6t",
            "3.2t",
            "spectrum-x",
            "ethernet photonics",
            "serdes",
            "aec",
        ],
        "tickers": ["COHR", "LITE", "FN", "CIEN", "GLW", "MRVL", "CRDO", "ANET", "NVDA"],
        "bottleneck": "network/optical",
        "default_layer": "上游",
    },
    {
        "theme": "AI data center power and cooling",
        "keywords": [
            "liquid cooling",
            "cooling",
            "power demand",
            "power usage",
            "power plant",
            "power grid",
            "electricity",
            "datacenter power",
            "data center power",
            "grid",
            "pue",
            "water",
            "turbine",
            "natural gas",
            "nuclear",
            "off-grid",
            "off grid",
        ],
        "tickers": ["VRT", "ETN", "GEV", "BE", "FLNC", "CVX", "LNG", "SMR", "OKLO", "CAT", "CEG", "NEE"],
        "bottleneck": "power/cooling",
        "default_layer": "中游",
    },
    {
        "theme": "HBM memory and context storage",
        "keywords": [
            "hbm",
            "memory bandwidth",
            "long context",
            "kv cache",
            "context memory",
            "ssd",
            "storage",
            "nand",
            "dram",
            "high bandwidth memory",
        ],
        "tickers": ["MU", "WDC", "STX", "SNDK", "NTAP", "PSTG", "NVDA", "AMD"],
        "bottleneck": "memory/storage",
        "default_layer": "上游",
    },
    {
        "theme": "AI inference serving and networking",
        "keywords": [
            "inference serving",
            "speculative decoding",
            "distributed inference",
            "agentic inference",
            "serving optimization",
            "all-reduce",
            "gpu cluster",
            "ai networking",
            "ethernet",
            "nvlink",
            "bluefield",
        ],
        "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "ANET", "CRDO", "ORCL", "MSFT", "AMZN", "GOOG", "CRWV", "NBIS"],
        "bottleneck": "compute/network",
        "default_layer": "中游",
    },
    {
        "theme": "AI PCB and advanced packaging",
        "keywords": [
            "pcb",
            "ccl",
            "abf",
            "substrate",
            "advanced packaging",
            "packaging",
            "interposer",
            "cowos",
            "fan-out",
            "high-speed pcb",
        ],
        "tickers": ["NVDA", "DELL", "ANET", "AVGO", "MRVL", "TSM", "ASML", "AMKR", "INTC"],
        "bottleneck": "packaging/pcb",
        "default_layer": "上游",
    },
    {
        "theme": "Semiconductor materials and consumables",
        "keywords": [
            "semiconductor materials",
            "specialty chemicals",
            "electronic materials",
            "consumables",
            "cmp",
            "slurry",
            "photoresist",
            "filtration",
            "purification",
            "wet chemicals",
            "advanced packaging materials",
            "thermal management",
        ],
        "tickers": ["Q", "ENTG", "AMAT", "LRCX", "INTC", "TSM"],
        "bottleneck": "materials/consumables",
        "default_layer": "上游",
    },
    {
        "theme": "Semiconductor equipment and manufacturing",
        "keywords": [
            "semiconductor equipment",
            "euv",
            "lithography",
            "etch",
            "deposition",
            "metrology",
            "wafer",
            "foundry",
            "advanced node",
            "test equipment",
        ],
        "tickers": ["ASML", "TSM", "VECO", "INTC", "AMAT", "LRCX", "KLAC", "AMKR", "Q", "ENTG"],
        "bottleneck": "semicap/manufacturing",
        "default_layer": "上游",
    },
    {
        "theme": "Physical AI and robotics",
        "keywords": [
            "physical ai",
            "robotics",
            "robot",
            "embodied ai",
            "world model",
            "humanoid",
            "actuator",
            "sensor",
            "edge ai",
        ],
        "tickers": ["NVDA", "TSLA", "ISRG", "TER", "ROK"],
        "bottleneck": "edge/robotics",
        "default_layer": "下游",
    },
    {
        "theme": "Space defense and autonomous systems",
        "keywords": [
            "satellite",
            "space systems",
            "aerospace",
            "defense",
            "autonomous systems",
            "drone",
            "uav",
            "counter-drone",
            "low earth orbit",
            "leo",
        ],
        "tickers": ["RKLB", "ASTS", "PL", "LMT", "RTX", "AVAV"],
        "bottleneck": "space/defense",
        "default_layer": "下游",
    },
]


# 中文快讯关键词补充（财联社 / 金十）：theme 名与 THEME_RULES 对应，
# 让中文快讯也能命中主题，再经股票池映射过滤噪声。
CN_THEME_KEYWORDS: dict[str, list[str]] = {
    "Rack-scale AI factory": ["英伟达", "黄仁勋", "算力", "智算", "AI服务器", "服务器", "机架", "数据中心", "算力中心", "超节点"],
    "CPO and optical interconnect": ["光模块", "光通信", "硅光", "光芯片", "光互连", "CPO"],
    "AI data center power and cooling": ["液冷", "散热", "数据中心", "供电", "变压器", "储能", "PUE", "电力设备"],
    "HBM memory and context storage": ["HBM", "存储芯片", "内存", "DRAM", "固态硬盘", "存储"],
    "AI inference serving and networking": ["AI推理", "推理", "交换机", "网络设备", "以太网", "DPU", "网卡"],
    "AI PCB and advanced packaging": ["PCB", "覆铜板", "先进封装", "CoWoS", "封装测试", "载板", "印制电路板"],
    "Semiconductor materials and consumables": ["半导体材料", "光刻胶", "硅片", "靶材", "电子特气"],
    "Semiconductor equipment and manufacturing": ["半导体设备", "光刻机", "刻蚀", "晶圆", "芯片代工", "制程", "晶圆厂"],
    "Physical AI and robotics": ["人形机器人", "机器人", "具身智能", "自动驾驶", "激光雷达", "无人机"],
    "Space defense and autonomous systems": ["商业航天", "卫星", "低空经济", "军工", "国防"],
}

for _cn_rule in THEME_RULES:
    _cn_extra = CN_THEME_KEYWORDS.get(str(_cn_rule["theme"]), [])
    _cn_existing = set(_cn_rule["keywords"])
    _cn_rule["keywords"].extend(keyword for keyword in _cn_extra if keyword not in _cn_existing)
del _cn_rule, _cn_extra, _cn_existing


OFFICIAL_FEEDS = [
    {
        "name": "NVIDIA Blog",
        "url": "https://blogs.nvidia.com/feed/",
        "source_type": "official",
        "theme_hint": "",
    },
    {
        "name": "Microsoft Official Blog",
        "url": "https://blogs.microsoft.com/feed/",
        "source_type": "official",
        "theme_hint": "",
    },
    {
        "name": "AWS Machine Learning Blog",
        "url": "https://aws.amazon.com/blogs/machine-learning/feed/",
        "source_type": "official",
        "theme_hint": "",
    },
]


NEWS_QUERIES = [
    ("Vera Rubin AI factory NVIDIA", "Rack-scale AI factory"),
    ("AI data center liquid cooling power", "AI data center power and cooling"),
    ("co-packaged optics AI data center CPO", "CPO and optical interconnect"),
    ("long context inference serving KV cache AI", "AI inference serving and networking"),
    ("HBM AI server storage memory bandwidth", "HBM memory and context storage"),
    ("US domestic semiconductor supply chain advanced packaging materials equipment", "Semiconductor materials and consumables"),
    ("physical AI robotics world model", "Physical AI and robotics"),
    ("AI PCB CCL advanced packaging server", "AI PCB and advanced packaging"),
]


ARXIV_QUERIES = [
    ("Long context and KV cache", 'cat:cs.CL AND all:"long context"'),
    ("KV cache and inference memory", 'cat:cs.CL AND all:"KV cache"'),
    ("Mixture of experts", 'cat:cs.LG AND all:"mixture of experts"'),
    ("Speculative decoding", 'cat:cs.LG AND all:"speculative decoding"'),
    ("Inference serving", 'cat:cs.DC AND all:"inference serving"'),
    ("Optical interconnect", 'cat:cs.NI AND all:"optical interconnect"'),
    ("World models and robotics", 'cat:cs.RO AND all:"world model"'),
    ("Memory bandwidth", 'cat:cs.AR AND all:"memory bandwidth"'),
]


@dataclass
class Signal:
    signal_id: str
    date: str
    source_type: str
    source_name: str
    source_url: str
    title: str
    summary: str
    theme: str
    tickers_mentioned: str
    mapped_tickers: str
    mapped_a_shares: str
    evidence_strength: int
    confidence: int
    created_at: str

    def as_row(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in SIGNAL_FIELDS}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def report_today() -> str:
    timezone_name = os.environ.get("DISCOVERY_TIMEZONE", "Asia/Shanghai")
    try:
        report_timezone = ZoneInfo(timezone_name)
    except Exception:
        report_timezone = ZoneInfo("Asia/Shanghai")
    return now_utc().astimezone(report_timezone).date().isoformat()


def clean_text(value: object, max_len: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if local_name(child.tag) == name:
            return clean_text(child.text)
    return ""


def parse_any_date(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def iso_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).date().isoformat()


def fetch_text(url: str, timeout: int = 25) -> str:
    result = subprocess.run(
        [
            "curl",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            str(timeout),
            "--user-agent",
            USER_AGENT,
            url,
        ],
        check=True,
        capture_output=True,
        timeout=timeout + 5,
    )
    return result.stdout.decode("utf-8", errors="replace")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    os.replace(temporary, path)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def latest_discovery_date() -> str | None:
    candidates = read_csv_rows(CANDIDATES_FILE)
    candidate_dates = [row.get("run_date", "") for row in candidates if row.get("run_date")]
    if candidate_dates:
        return max(candidate_dates)

    signal_dates = [row.get("date", "") for row in read_csv_rows(SIGNALS_FILE) if row.get("date")]
    paper_dates = [row.get("published", "")[:10] for row in read_csv_rows(PAPERS_FILE) if row.get("published")]
    dates = signal_dates + paper_dates
    return max(dates) if dates else None


def discovery_result_is_valid(signals: list[object], candidates: list[dict[str, object]]) -> bool:
    return bool(signals) and bool(candidates)


def write_discovery_status(
    *,
    status: str,
    attempted_at: str,
    attempted_date: str,
    data_date: str | None,
    signals: int,
    papers: int,
    candidates: int,
    warnings: list[str],
    message: str,
) -> None:
    write_json(
        STATUS_FILE,
        {
            "version": 1,
            "status": status,
            "attemptedAt": attempted_at,
            "attemptedDate": attempted_date,
            "dataDate": data_date,
            "message": message,
            "counts": {"signals": signals, "papers": papers, "candidates": candidates},
            "warnings": warnings[:20],
        },
    )


def update_discovery_history(
    report_date: str,
    context: dict[str, object],
    quote_payload: dict[str, object],
    signals: list[object],
    papers: list[dict[str, object]],
    candidates: list[dict[str, object]],
) -> None:
    history = [row for row in read_csv_rows(HISTORY_FILE) if row.get("date") != report_date]
    observe = [row for row in candidates if row.get("recommendation") == "observe"]
    quotes = quote_payload.get("quotes", {})
    usable_quotes = len(quotes) if isinstance(quotes, dict) else 0
    missing = quote_payload.get("missing", [])
    if isinstance(missing, list):
        missing_text = ";".join(str(item) for item in missing)
    else:
        missing_text = str(missing or "")
    pool_rows = context.get("pool_rows", [])
    history.append(
        {
            "date": report_date,
            "pool_size": len(pool_rows) if isinstance(pool_rows, list) else 0,
            "quotes_requested": quote_payload.get("_online_requested", quote_payload.get("requested", 0)),
            "quotes_received": quote_payload.get("_online_received", quote_payload.get("received", 0)),
            "missing_quotes": missing_text,
            "extra_quotes": quote_payload.get("_extra_quote_count", 0),
            "usable_quotes": quote_payload.get("_quote_available_count", usable_quotes),
            "signals": len(signals),
            "arxiv_papers": len(papers),
            "candidates": len(candidates),
            "observe_count": len(observe),
            "observe_tickers": ";".join(str(row.get("ticker", "")) for row in observe if row.get("ticker")),
            "report_href": f"reports/discovery-{report_date}.md",
        }
    )
    history.sort(key=lambda row: str(row.get("date") or ""))
    write_csv_rows(HISTORY_FILE, HISTORY_FIELDS, history)


def merge_rows(existing: list[dict[str, str]], new_rows: list[dict[str, object]], key: str) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for row in existing:
        if row.get(key):
            merged[str(row[key])] = dict(row)
    for row in new_rows:
        if row.get(key):
            merged[str(row[key])] = dict(row)
    return list(merged.values())


def hash_id(*parts: object) -> str:
    payload = "|".join(clean_text(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def load_aliases(path: Path = ALIASES_FILE) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    aliases: dict[str, list[str]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            aliases[key] = [str(item).strip().upper() for item in value if str(item).strip()]
    return aliases


def build_pool_context() -> dict[str, object]:
    pool_rows = read_csv_rows(POOL_FILE)
    us_rows = read_csv_rows(US_SOURCE_FILE)
    a_rows = read_csv_rows(A_SOURCE_FILE)

    pool_by_ticker = {row["ticker"].strip().upper(): row for row in pool_rows if row.get("ticker")}
    source_by_ticker: dict[str, dict[str, str]] = {}
    for row in us_rows:
        if row.get("ticker"):
            ticker = row["ticker"].strip().upper()
            source_by_ticker[ticker] = {
                **row,
                "market": "美股",
                "source_us": "",
                "pool_status": row.get("status", ""),
            }
    for row in a_rows:
        if row.get("ticker"):
            ticker = row["ticker"].strip().upper()
            source_by_ticker[ticker] = {**row, "market": "A股"}

    company_to_ticker: dict[str, str] = {}
    for ticker, row in {**source_by_ticker, **pool_by_ticker}.items():
        company = clean_text(row.get("company", ""))
        if company:
            company_to_ticker[company.lower()] = ticker
    for ticker, (company, _market) in COMPANY_OVERRIDES.items():
        company_to_ticker[company.lower()] = ticker

    return {
        "pool_rows": pool_rows,
        "us_rows": us_rows,
        "a_rows": a_rows,
        "pool_by_ticker": pool_by_ticker,
        "source_by_ticker": source_by_ticker,
        "company_to_ticker": company_to_ticker,
    }


def theme_rule(theme: str) -> dict[str, object] | None:
    for rule in THEME_RULES:
        if rule["theme"] == theme:
            return rule
    return None


def analyze_themes(text: str, theme_hint: str = "") -> list[str]:
    lower = text.lower()
    scored: list[tuple[int, str]] = []
    for rule in THEME_RULES:
        hits = sum(1 for keyword in rule["keywords"] if keyword.lower() in lower)
        if hits:
            scored.append((hits, str(rule["theme"])))
    if theme_hint and not any(theme == theme_hint for _hits, theme in scored):
        scored.append((1, theme_hint))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [theme for _hits, theme in scored[:3]]


def direct_ticker_mentions(text: str, known_tickers: Iterable[str], company_to_ticker: dict[str, str]) -> set[str]:
    lower = text.lower()
    mentions: set[str] = set()
    us_tickers = [ticker for ticker in known_tickers if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z]{2})?", ticker)]
    for ticker in us_tickers:
        if "." in ticker:
            continue
        if re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text.upper()):
            mentions.add(ticker)
    for company, ticker in company_to_ticker.items():
        if len(company) >= 4 and company in lower:
            mentions.add(ticker)
    return mentions


def map_tickers(text: str, themes: list[str], aliases: dict[str, list[str]], context: dict[str, object]) -> tuple[list[str], list[str]]:
    pool_by_ticker: dict[str, dict[str, str]] = context["pool_by_ticker"]  # type: ignore[assignment]
    source_by_ticker: dict[str, dict[str, str]] = context["source_by_ticker"]  # type: ignore[assignment]
    company_to_ticker: dict[str, str] = context["company_to_ticker"]  # type: ignore[assignment]
    known = set(pool_by_ticker) | set(source_by_ticker) | set(COMPANY_OVERRIDES)
    mapped: set[str] = set(direct_ticker_mentions(text, known, company_to_ticker))

    lower = text.lower()
    for theme in themes:
        rule = theme_rule(theme)
        if rule:
            mapped.update(str(ticker).upper() for ticker in rule["tickers"])  # type: ignore[index]
    for alias, tickers in aliases.items():
        if alias.lower() in lower or alias in themes:
            mapped.update(tickers)

    mapped_us = sorted(ticker for ticker in mapped if not re.search(r"\.(SZ|SS|BJ)$", ticker))
    return mapped_us[:30], sorted(mapped)[:40]


def split_source_tokens(value: str) -> set[str]:
    return {token.strip().upper() for token in re.split(r"[;,；、/]", value or "") if token.strip()}


def map_a_shares(text: str, themes: list[str], mapped_us: list[str], context: dict[str, object]) -> list[str]:
    a_rows: list[dict[str, str]] = context["a_rows"]  # type: ignore[assignment]
    lower = text.lower()
    mapped_us_set = set(ticker.upper() for ticker in mapped_us)
    candidates: list[tuple[int, str]] = []

    for row in a_rows:
        ticker = row.get("ticker", "").strip().upper()
        if not ticker:
            continue
        source_us = row.get("source_us", "")
        source_tokens = split_source_tokens(source_us)
        combined = " ".join(
            [
                row.get("company", ""),
                row.get("source_us", ""),
                row.get("sector", ""),
                row.get("role", ""),
                row.get("key_focus", ""),
                row.get("pool_status", ""),
            ]
        ).lower()
        score = 0
        if mapped_us_set & source_tokens:
            score += 4
        for theme in themes:
            if theme.lower() in combined:
                score += 2
            rule = theme_rule(theme)
            if rule:
                score += sum(1 for keyword in rule["keywords"] if keyword.lower() in combined)  # type: ignore[index]
        if row.get("company", "").lower() in lower:
            score += 3
        if score:
            candidates.append((score, ticker))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [ticker for _score, ticker in candidates[:24]]


def parse_feed_entries(xml_text: str, source: dict[str, str]) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[dict[str, str]] = []
    if local_name(root.tag).lower() in {"rss", "rdf"}:
        nodes = [node for node in root.iter() if local_name(node.tag) == "item"]
        for item in nodes:
            entries.append(
                {
                    "title": child_text(item, "title"),
                    "summary": child_text(item, "description"),
                    "url": child_text(item, "link"),
                    "date": child_text(item, "pubDate"),
                    "source_name": source["name"],
                    "source_type": source["source_type"],
                    "theme_hint": source.get("theme_hint", ""),
                }
            )
    else:
        nodes = [node for node in root.iter() if local_name(node.tag) == "entry"]
        for entry in nodes:
            link = ""
            for child in entry:
                if local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    if child.attrib.get("rel") in {"alternate", ""}:
                        break
            entries.append(
                {
                    "title": child_text(entry, "title"),
                    "summary": child_text(entry, "summary"),
                    "url": link or child_text(entry, "id"),
                    "date": child_text(entry, "published") or child_text(entry, "updated"),
                    "source_name": source["name"],
                    "source_type": source["source_type"],
                    "theme_hint": source.get("theme_hint", ""),
                }
            )
    return entries


def evidence_strength(source_type: str, theme_count: int, mapped_count: int) -> int:
    base = {"official": 78, "news": 52, "arxiv": 46, "paper": 46, "market": 40}.get(source_type, 45)
    return min(95, base + min(theme_count, 3) * 4 + min(mapped_count, 8))


def confidence_score(source_type: str, theme_count: int, mapped_count: int, a_count: int) -> int:
    base = {"official": 60, "news": 44, "arxiv": 38, "paper": 38}.get(source_type, 35)
    return min(95, base + min(theme_count, 3) * 8 + min(mapped_count, 6) * 3 + min(a_count, 4) * 2)


def make_signal(entry: dict[str, str], aliases: dict[str, list[str]], context: dict[str, object], generated_at: str) -> Signal | None:
    title = clean_text(entry.get("title", ""), 240)
    summary = clean_text(entry.get("summary", ""), 600)
    url = clean_text(entry.get("url", ""), 500)
    text = f"{title} {summary}"
    themes = analyze_themes(text, entry.get("theme_hint", ""))
    if not themes:
        return None
    mapped_us, mapped_all = map_tickers(text, themes, aliases, context)
    mapped_a = map_a_shares(text, themes, mapped_us, context)
    if not mapped_all and not mapped_a:
        return None
    dt = parse_any_date(entry.get("date", "")) or now_utc()
    source_type = entry.get("source_type", "news")
    signal_id = hash_id(source_type, entry.get("source_name", ""), url, title)
    return Signal(
        signal_id=signal_id,
        date=iso_date(dt),
        source_type=source_type,
        source_name=clean_text(entry.get("source_name", ""), 120),
        source_url=url,
        title=title,
        summary=summary,
        theme="; ".join(themes),
        tickers_mentioned="; ".join(sorted(direct_ticker_mentions(text, mapped_all, context["company_to_ticker"]))),  # type: ignore[arg-type]
        mapped_tickers="; ".join(mapped_us),
        mapped_a_shares="; ".join(mapped_a),
        evidence_strength=evidence_strength(source_type, len(themes), len(mapped_us)),
        confidence=confidence_score(source_type, len(themes), len(mapped_us), len(mapped_a)),
        created_at=generated_at,
    )


# --- 中文快讯源：财联社电报 / 金十数据快讯（免费，无需 key）---
CLS_ROLL_API = "https://www.cls.cn/v1/roll/get_roll_list"
JIN10_FLASH_API = "https://www.jin10.com/flash_newest.js"
CN_FLASH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _fetch_text_with_headers(url: str, referer: str, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": CN_FLASH_UA, "Referer": referer, "Accept": "*/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _cls_signature(query: str) -> str:
    return hashlib.md5(hashlib.sha1(query.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def _cls_stock_id_to_pool(stock_id: str) -> str:
    """财联社 sh600693 -> 股票池 600693.SS；sz -> .SZ；bj -> .BJ。"""
    sid = stock_id.strip().lower()
    if sid.startswith("sh") and len(sid) > 2:
        return sid[2:].upper() + ".SS"
    if sid.startswith("sz") and len(sid) > 2:
        return sid[2:].upper() + ".SZ"
    if sid.startswith("bj") and len(sid) > 2:
        return sid[2:].upper() + ".BJ"
    return ""


def fetch_cls_telegraph(max_items: int = 50) -> list[tuple[dict[str, str], list[str]]]:
    """财联社电报滚动。返回 (entry, 明确关联的A股代码) 列表。"""
    rn = max(1, min(max_items, 50))
    query = f"app=CailianpressWeb&last_time=&last_time_key=&os=web&rn={rn}&sv=8.4.6"
    url = f"{CLS_ROLL_API}?{query}&sign={_cls_signature(query)}"
    payload = json.loads(_fetch_text_with_headers(url, referer="https://www.cls.cn/telegraph"))
    if not isinstance(payload, dict) or payload.get("errno") != 0:
        raise RuntimeError(f"CLS errno={payload.get('errno') if isinstance(payload, dict) else 'n/a'}")
    results: list[tuple[dict[str, str], list[str]]] = []
    for item in ((payload.get("data") or {}).get("roll_data", []) or [])[:max_items]:
        if not isinstance(item, dict) or item.get("is_ad"):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title and not content:
            continue
        try:
            date_str = datetime.fromtimestamp(int(item.get("ctime") or 0), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            date_str = ""
        subjects = item.get("subjects") or []
        theme_hint = ""
        if subjects and isinstance(subjects[0], dict):
            theme_hint = str(subjects[0].get("subject_name") or "")
        a_codes = [
            code
            for stock in (item.get("stock_list") or [])
            if isinstance(stock, dict) and (code := _cls_stock_id_to_pool(str(stock.get("StockID") or "")))
        ]
        results.append(
            (
                {
                    "title": title or content[:80],
                    "summary": str(item.get("brief") or "") or content,
                    "url": str(item.get("shareurl") or "https://www.cls.cn/telegraph"),
                    "date": date_str,
                    "source_name": "财联社电报",
                    "source_type": "news",
                    "theme_hint": theme_hint,
                },
                sorted(set(a_codes)),
            )
        )
    return results


def fetch_jin10_flash(max_items: int = 50) -> list[tuple[dict[str, str], list[str]]]:
    """金十数据最新快讯（仅最近约 50 条，随每日任务增量累积）。"""
    raw = _fetch_text_with_headers(JIN10_FLASH_API, referer="https://www.jin10.com/")
    match = re.search(r"var\s+newest\s*=\s*(\[.*\])\s*;", raw, re.S)
    if not match:
        raise RuntimeError("jin10 payload parse failed")
    items = json.loads(match.group(1))
    results: list[tuple[dict[str, str], list[str]]] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue
        data = item.get("data") or {}
        content = str(data.get("content") or "").strip()
        title = str(data.get("title") or "").strip()
        if not content and not title:
            continue
        if not title:
            bracket = re.match(r"^【([^】]{2,80})】", content)
            title = bracket.group(1) if bracket else content[:80]
        results.append(
            (
                {
                    "title": title,
                    "summary": content,
                    "url": "https://www.jin10.com/",
                    "date": str(item.get("time") or ""),
                    "source_name": "金十数据",
                    "source_type": "news",
                    "theme_hint": "",
                },
                [],
            )
        )
    return results


def fetch_cn_flash_news(
    days: int,
    aliases: dict[str, list[str]],
    context: dict[str, object],
    generated_at: str,
    max_items_per_source: int = 30,
) -> tuple[list[Signal], list[str]]:
    """中文快讯 -> 统一 Signal（含财联社明确关联的 A 股）。"""
    cutoff = now_utc() - timedelta(days=days)
    raw: list[tuple[dict[str, str], list[str]]] = []
    warnings: list[str] = []
    for name, fetcher in (("财联社电报", fetch_cls_telegraph), ("金十数据", fetch_jin10_flash)):
        try:
            raw.extend(fetcher(max_items_per_source))
        except Exception as error:
            warnings.append(f"{name}: {type(error).__name__}: {str(error)[:120]}")
    try:
        for item in cn_extra_sources.fetch_all_cn_extra():
            entry = {
                "title": (item.get("title") or item.get("text", "")[:60]),
                "summary": item.get("text", ""),
                "url": item.get("url", ""),
                "source_name": item.get("source", ""),
                "date": item.get("published_at", ""),
                "source_type": "news",
            }
            raw.append((entry, []))
    except Exception as error:
        warnings.append(f"中文补充源: {type(error).__name__}: {str(error)[:120]}")
    signals: list[Signal] = []
    seen: set[str] = set()
    for entry, a_codes in raw:
        dt = parse_any_date(entry.get("date", "")) or now_utc()
        if dt < cutoff:
            continue
        signal = make_signal(entry, aliases, context, generated_at)
        if not signal or signal.signal_id in seen:
            continue
        if a_codes:
            existing = [code for code in signal.mapped_a_shares.split("; ") if code]
            signal.mapped_a_shares = "; ".join(sorted(set(existing) | set(a_codes)))
        seen.add(signal.signal_id)
        signals.append(signal)
    signals.sort(key=lambda row: (row.date, row.evidence_strength, row.confidence), reverse=True)
    return signals, warnings


def fetch_official_and_news(
    days: int,
    aliases: dict[str, list[str]],
    context: dict[str, object],
    generated_at: str,
    max_items_per_source: int,
) -> tuple[list[Signal], list[str]]:
    cutoff = now_utc() - timedelta(days=days)
    sources = list(OFFICIAL_FEEDS)
    for query, theme_hint in NEWS_QUERIES:
        params = {
            "q": f"{query} when:{days}d",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        sources.append(
            {
                "name": f"Google News: {query}",
                "url": "https://news.google.com/rss/search?" + urllib.parse.urlencode(params),
                "source_type": "news",
                "theme_hint": theme_hint,
            }
        )

    signals: list[Signal] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for source in sources:
        try:
            xml_text = fetch_text(source["url"])
            entries = parse_feed_entries(xml_text, source)
        except Exception as error:
            warnings.append(f"{source['name']}: {type(error).__name__}: {str(error)[:120]}")
            continue
        for entry in entries[:max_items_per_source]:
            dt = parse_any_date(entry.get("date", "")) or now_utc()
            if dt < cutoff:
                continue
            signal = make_signal(entry, aliases, context, generated_at)
            if not signal or signal.signal_id in seen:
                continue
            seen.add(signal.signal_id)
            signals.append(signal)
    signals.sort(key=lambda row: (row.date, row.evidence_strength, row.confidence), reverse=True)
    return signals, warnings


def arxiv_query_url(query: str, max_results: int) -> str:
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": "0",
        "max_results": str(max_results),
    }
    return "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)


def parse_arxiv_feed(xml_text: str) -> list[dict[str, object]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = [node for node in root.iter() if local_name(node.tag) == "entry"]
    papers: list[dict[str, object]] = []
    for entry in entries:
        entry_id = child_text(entry, "id")
        arxiv_id = entry_id.rstrip("/").split("/")[-1]
        links: dict[str, str] = {}
        categories: list[str] = []
        primary_category = ""
        authors: list[str] = []
        for child in entry:
            lname = local_name(child.tag)
            if lname == "author":
                name = child_text(child, "name")
                if name:
                    authors.append(name)
            elif lname == "category" and child.attrib.get("term"):
                categories.append(child.attrib["term"])
            elif lname == "primary_category" and child.attrib.get("term"):
                primary_category = child.attrib["term"]
            elif lname == "link" and child.attrib.get("href"):
                rel = child.attrib.get("rel", "alternate")
                title = child.attrib.get("title", "")
                if title == "pdf" or child.attrib.get("type") == "application/pdf":
                    links["pdf"] = child.attrib["href"]
                elif rel == "alternate":
                    links["abs"] = child.attrib["href"]
        if not primary_category and categories:
            primary_category = categories[0]
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "published": child_text(entry, "published"),
                "updated": child_text(entry, "updated"),
                "title": child_text(entry, "title"),
                "authors": "; ".join(authors),
                "primary_category": primary_category,
                "categories": "; ".join(dict.fromkeys(categories)),
                "abstract": child_text(entry, "summary"),
                "abs_url": links.get("abs", entry_id),
                "pdf_url": links.get("pdf", ""),
            }
        )
    return papers


def paper_signal_score(title: str, abstract: str, categories: str, theme_count: int, mapped_count: int) -> int:
    text = f"{title} {abstract} {categories}".lower()
    score = 35 + min(theme_count, 3) * 10 + min(mapped_count, 8) * 2
    high_signal_terms = [
        "inference",
        "serving",
        "long context",
        "kv cache",
        "mixture of experts",
        "world model",
        "robotics",
        "optical",
        "memory bandwidth",
        "distributed",
        "gpu",
    ]
    score += sum(4 for term in high_signal_terms if term in text)
    if any(category in categories for category in ["cs.CL", "cs.LG", "cs.DC", "cs.NI", "cs.RO", "cs.AR"]):
        score += 5
    return max(0, min(100, score))


def technical_signal_for(themes: list[str]) -> str:
    bottlenecks = []
    for theme in themes:
        rule = theme_rule(theme)
        if rule and rule.get("bottleneck"):
            bottlenecks.append(str(rule["bottleneck"]))
    return "; ".join(dict.fromkeys(bottlenecks)) or "AI method / system signal"


def fetch_arxiv_papers(
    days: int,
    max_results: int,
    delay_seconds: float,
    aliases: dict[str, list[str]],
    context: dict[str, object],
) -> tuple[list[dict[str, object]], list[Signal], list[str]]:
    cutoff = now_utc() - timedelta(days=days)
    papers: dict[str, dict[str, object]] = {}
    signals: list[Signal] = []
    warnings: list[str] = []
    generated_at = now_utc().isoformat()

    for index, (topic, query) in enumerate(ARXIV_QUERIES):
        try:
            xml_text = fetch_text(arxiv_query_url(query, max_results), timeout=30)
            raw_papers = parse_arxiv_feed(xml_text)
        except Exception as error:
            warnings.append(f"arXiv {topic}: {type(error).__name__}: {str(error)[:120]}")
            raw_papers = []
        for raw in raw_papers:
            published = parse_any_date(str(raw.get("published", "")))
            if published and published < cutoff:
                continue
            title = clean_text(raw.get("title", ""), 260)
            abstract = clean_text(raw.get("abstract", ""), 1200)
            text = f"{title} {abstract} {topic}"
            themes = analyze_themes(text, topic_to_theme_hint(topic))
            mapped_us, _mapped_all = map_tickers(text, themes, aliases, context)
            mapped_a = map_a_shares(text, themes, mapped_us, context)
            score = paper_signal_score(title, abstract, str(raw.get("categories", "")), len(themes), len(mapped_us))
            if score < 45 or not themes:
                continue
            arxiv_id = str(raw.get("arxiv_id", ""))
            paper_row = {
                "arxiv_id": arxiv_id,
                "published": iso_date(published) or clean_text(raw.get("published", "")),
                "updated": iso_date(parse_any_date(str(raw.get("updated", "")))) or clean_text(raw.get("updated", "")),
                "title": title,
                "authors": clean_text(raw.get("authors", ""), 500),
                "primary_category": clean_text(raw.get("primary_category", "")),
                "categories": clean_text(raw.get("categories", "")),
                "abstract": abstract,
                "abs_url": clean_text(raw.get("abs_url", "")),
                "pdf_url": clean_text(raw.get("pdf_url", "")),
                "topic": topic,
                "technical_signal": technical_signal_for(themes),
                "bottleneck": technical_signal_for(themes),
                "mapped_themes": "; ".join(themes),
                "mapped_tickers": "; ".join(mapped_us),
                "mapped_a_shares": "; ".join(mapped_a),
                "paper_signal_score": score,
                "investment_readthrough": investment_readthrough(themes, mapped_us, mapped_a),
                "risk_note": "论文是技术前沿信号，不等同商业订单；需等待产品路线、capex、财报或供应链验证。",
            }
            papers[arxiv_id] = paper_row
            signal = Signal(
                signal_id=hash_id("arxiv", arxiv_id, title),
                date=str(paper_row["published"]),
                source_type="arxiv",
                source_name=f"arXiv: {topic}",
                source_url=str(paper_row["abs_url"]),
                title=title,
                summary=abstract,
                theme="; ".join(themes),
                tickers_mentioned="",
                mapped_tickers="; ".join(mapped_us),
                mapped_a_shares="; ".join(mapped_a),
                evidence_strength=evidence_strength("arxiv", len(themes), len(mapped_us)),
                confidence=confidence_score("arxiv", len(themes), len(mapped_us), len(mapped_a)),
                created_at=generated_at,
            )
            signals.append(signal)
        if index < len(ARXIV_QUERIES) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    paper_rows = list(papers.values())
    paper_rows.sort(key=lambda row: (str(row.get("published", "")), int(row.get("paper_signal_score", 0))), reverse=True)
    signals.sort(key=lambda row: (row.date, row.evidence_strength), reverse=True)
    return paper_rows, signals, warnings


def topic_to_theme_hint(topic: str) -> str:
    mapping = {
        "Long context and KV cache": "HBM memory and context storage",
        "KV cache and inference memory": "HBM memory and context storage",
        "Mixture of experts": "AI inference serving and networking",
        "Speculative decoding": "AI inference serving and networking",
        "Inference serving": "AI inference serving and networking",
        "Optical interconnect": "CPO and optical interconnect",
        "World models and robotics": "Physical AI and robotics",
        "Memory bandwidth": "HBM memory and context storage",
    }
    return mapping.get(topic, "")


def investment_readthrough(themes: list[str], mapped_us: list[str], mapped_a: list[str]) -> str:
    if not themes:
        return "仅记录技术方向，暂未形成明确投资映射。"
    primary = themes[0]
    return (
        f"{primary} 指向 {technical_signal_for(themes)} 瓶颈；"
        f"美股锚点 {', '.join(mapped_us[:8]) or '待映射'}；"
        f"A股映射 {', '.join(mapped_a[:8]) or '待映射'}。"
    )


def fetch_quotes() -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    try:
        payload = json.loads(fetch_text(QUOTE_API_URL, timeout=80))
        payload["_online_requested"] = payload.get("requested", 0)
        payload["_online_received"] = payload.get("received", 0)
        return payload, warnings
    except Exception as error:
        warnings.append(f"quote api: {type(error).__name__}: {str(error)[:120]}")
        return {"quotes": {}, "requested": 0, "received": 0, "missing": []}, warnings


def yahoo_chart_quote(symbol: str) -> dict[str, object] | None:
    if curl_requests is None:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
    response = curl_requests.get(
        url,
        params={"range": "5d", "interval": "1d", "includePrePost": "false", "events": "div,splits"},
        impersonate="chrome",
        timeout=10,
    )
    response.raise_for_status()
    chart = response.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        return None
    result = chart["result"][0]
    meta = result.get("meta", {})
    timestamps = result.get("timestamp", [])
    quote_rows = result.get("indicators", {}).get("quote", [])
    if not timestamps or not quote_rows:
        return None
    series = quote_rows[0]
    closes = series.get("close", [])
    highs = series.get("high", [])
    lows = series.get("low", [])
    volumes = series.get("volume", [])
    valid = [index for index, value in enumerate(closes) if finite_float(value) is not None]
    if not valid:
        return None
    last_index = valid[-1]
    previous_index = valid[-2] if len(valid) > 1 else None
    last_price = finite_float(closes[last_index])
    previous_close = finite_float(closes[previous_index]) if previous_index is not None else None
    if last_price is None:
        return None
    change = last_price - previous_close if previous_close else None
    change_percent = (change / previous_close * 100) if change is not None and previous_close else None
    day_high = finite_float(highs[last_index]) if last_index < len(highs) else None
    day_low = finite_float(lows[last_index]) if last_index < len(lows) else None
    volume = finite_float(volumes[last_index]) if last_index < len(volumes) else None
    return {
        "price": round(last_price, 4),
        "previousClose": round(previous_close, 4) if previous_close is not None else None,
        "change": round(change, 4) if change is not None else None,
        "changePercent": round(change_percent, 4) if change_percent is not None else None,
        "dayHigh": round(day_high, 4) if day_high is not None else None,
        "dayLow": round(day_low, 4) if day_low is not None else None,
        "volume": int(volume) if volume is not None else None,
        "currency": meta.get("currency") or ("CNY" if re.search(r"\.(SZ|SS|BJ)$", symbol) else "USD"),
        "market": "A股" if re.search(r"\.(SZ|SS|BJ)$", symbol) else "美股",
        "timestamp": datetime.fromtimestamp(timestamps[last_index], timezone.utc).isoformat(),
    }


def collect_signal_tickers(signals: list[Signal], papers: list[dict[str, object]]) -> list[str]:
    tickers: set[str] = set()
    for signal in signals:
        tickers.update(split_source_tokens(signal.mapped_tickers))
        tickers.update(split_source_tokens(signal.mapped_a_shares))
    for paper in papers:
        tickers.update(split_source_tokens(str(paper.get("mapped_tickers", ""))))
        tickers.update(split_source_tokens(str(paper.get("mapped_a_shares", ""))))
    return sorted(tickers)


def enrich_quotes(quotes_payload: dict[str, object], tickers: Iterable[str], max_extra: int) -> list[str]:
    warnings: list[str] = []
    quotes = quotes_payload.setdefault("quotes", {})
    if not isinstance(quotes, dict):
        quotes_payload["quotes"] = {}
        quotes = quotes_payload["quotes"]  # type: ignore[assignment]
    if curl_requests is None:
        return ["curl_cffi unavailable: skipped extra Yahoo quote enrichment"]
    enriched = 0
    original_extra = int(quotes_payload.get("_extra_quote_count", 0) or 0)
    for ticker in tickers:
        if enriched >= max_extra:
            break
        if ticker in quotes:
            continue
        try:
            quote = yahoo_chart_quote(ticker)
        except Exception as error:
            warnings.append(f"extra quote {ticker}: {type(error).__name__}: {str(error)[:80]}")
            continue
        if quote:
            quotes[ticker] = quote
            enriched += 1
    quotes_payload["_extra_quote_count"] = original_extra + enriched
    quotes_payload["_quote_available_count"] = len(quotes)
    return warnings


def quote_for(quotes_payload: dict[str, object], ticker: str) -> dict[str, object] | None:
    quotes = quotes_payload.get("quotes", {})
    if isinstance(quotes, dict) and ticker in quotes and isinstance(quotes[ticker], dict):
        return quotes[ticker]  # type: ignore[return-value]
    return None


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def market_setup_score(quote: dict[str, object] | None, status: str = "") -> tuple[int, str]:
    if not quote:
        return 5, "无线上行情，按中性处理"
    change = finite_float(quote.get("changePercent"))
    if change is None:
        return 5, "行情无涨跌幅，按中性处理"
    score = 8
    note = "日内位置温和"
    if change <= -8:
        score, note = 3, "单日大跌，需要确认是否基本面破坏"
    elif change <= -4:
        score, note = 5, "短线偏弱，可观察是否回撤提供更好位置"
    elif change <= 4:
        score, note = 8, "日内位置温和"
    elif change <= 8:
        score, note = 6, "日内较强，注意追高"
    else:
        score, note = 3, "日内过热，优先观察不追"
    if "过热" in status or "高估值" in status:
        score = min(score, 4)
        note += "；原池状态提示过热/高估值"
    return score, note


def ticker_presence(ticker: str, context: dict[str, object]) -> str:
    pool_by_ticker: dict[str, dict[str, str]] = context["pool_by_ticker"]  # type: ignore[assignment]
    source_by_ticker: dict[str, dict[str, str]] = context["source_by_ticker"]  # type: ignore[assignment]
    if ticker in pool_by_ticker:
        return "online_pool"
    if ticker in source_by_ticker:
        return "source_pool_only"
    return "not_in_pool"


def row_for_ticker(ticker: str, context: dict[str, object]) -> dict[str, str]:
    pool_by_ticker: dict[str, dict[str, str]] = context["pool_by_ticker"]  # type: ignore[assignment]
    source_by_ticker: dict[str, dict[str, str]] = context["source_by_ticker"]  # type: ignore[assignment]
    if ticker in pool_by_ticker:
        return pool_by_ticker[ticker]
    if ticker in source_by_ticker:
        return source_by_ticker[ticker]
    company, market = COMPANY_OVERRIDES.get(ticker, (ticker, "美股"))
    return {"ticker": ticker, "company": company, "market": market, "chain_layer": "", "source_us": "", "status": ""}


def candidate_scores(
    ticker: str,
    support_signals: list[Signal],
    support_papers: list[dict[str, object]],
    quote: dict[str, object] | None,
    context: dict[str, object],
) -> tuple[dict[str, int], list[str]]:
    row = row_for_ticker(ticker, context)
    presence = ticker_presence(ticker, context)
    status = row.get("status") or row.get("pool_status", "")
    notes: list[str] = []

    source_types = Counter(signal.source_type for signal in support_signals)
    if source_types.get("official"):
        source_strength = min(20, 18 + source_types["official"])
    elif source_types.get("news"):
        source_strength = min(16, 10 + source_types["news"] * 2)
    elif source_types.get("arxiv"):
        source_strength = min(10, 6 + source_types["arxiv"])
    else:
        source_strength = 0

    if presence == "online_pool":
        exposure = 13
    elif presence == "source_pool_only":
        exposure = 11
        notes.append("已在源表但未出现在当前线上合并表")
    else:
        exposure = 8
        notes.append("不在当前股票池，需人工核实业务纯度")
    if re.search(r"\.(SZ|SS|BJ)$", ticker) and not row.get("evidence_level"):
        exposure = min(exposure, 8)
        notes.append("A股映射证据等级待补")

    max_paper_score = max([int(paper.get("paper_signal_score", 0) or 0) for paper in support_papers] or [0])
    paper_score = min(15, round(max_paper_score / 10 + len(support_papers) * 1.5))

    signal_dates = [parse_any_date(signal.date) for signal in support_signals]
    signal_dates = [dt for dt in signal_dates if dt]
    if signal_dates:
        newest_age = (now_utc() - max(signal_dates)).days
        novelty = 10 if newest_age <= 2 else 8 if newest_age <= 7 else 5
    else:
        novelty = 4
    if presence == "not_in_pool":
        novelty = min(10, novelty + 1)

    sentiment = min(10, source_types.get("news", 0) * 2 + source_types.get("official", 0))
    market_score, market_note = market_setup_score(quote, status)
    notes.append(market_note)

    risk_score = 5
    if presence == "not_in_pool":
        risk_score = 3
    if re.search(r"\.(SZ|SS|BJ)$", ticker) and ("概念" in row.get("role", "") or not row.get("evidence_level")):
        risk_score = min(risk_score, 3)
    if "过热" in status or "高估值" in status:
        risk_score = min(risk_score, 2)
    if not support_signals and support_papers:
        risk_score = min(risk_score, 3)
        notes.append("仅有论文信号，缺少产业采用确认")

    scores = {
        "source_strength_score": source_strength,
        "exposure_purity_score": exposure,
        "paper_signal_score": paper_score,
        "novelty_score": novelty,
        "sentiment_score": sentiment,
        "market_setup_score": market_score,
        "risk_score": risk_score,
    }
    return scores, notes


def recommendation(total: int, presence: str, support_signals: list[Signal], support_papers: list[dict[str, object]]) -> str:
    if presence == "online_pool":
        return "already_in_pool"
    has_non_paper = any(signal.source_type in {"official", "news"} for signal in support_signals)
    if total >= 80 and has_non_paper:
        return "propose_add"
    if total >= 65 or (support_papers and has_non_paper):
        return "observe"
    if presence == "source_pool_only":
        return "observe"
    return "reject"


def build_candidates(
    signals: list[Signal],
    papers: list[dict[str, object]],
    quotes_payload: dict[str, object],
    context: dict[str, object],
    run_date: str | None = None,
) -> list[dict[str, object]]:
    by_ticker_signals: dict[str, list[Signal]] = defaultdict(list)
    by_ticker_papers: dict[str, list[dict[str, object]]] = defaultdict(list)

    for signal in signals:
        tickers = split_source_tokens(signal.mapped_tickers) | split_source_tokens(signal.mapped_a_shares)
        for ticker in tickers:
            by_ticker_signals[ticker].append(signal)

    for paper in papers:
        tickers = split_source_tokens(str(paper.get("mapped_tickers", ""))) | split_source_tokens(str(paper.get("mapped_a_shares", "")))
        for ticker in tickers:
            by_ticker_papers[ticker].append(paper)

    all_tickers = sorted(set(by_ticker_signals) | set(by_ticker_papers))
    run_date = run_date or report_today()
    rows: list[dict[str, object]] = []
    for ticker in all_tickers:
        support_signals = by_ticker_signals.get(ticker, [])
        support_papers = by_ticker_papers.get(ticker, [])
        if not support_signals and not support_papers:
            continue
        info = row_for_ticker(ticker, context)
        presence = ticker_presence(ticker, context)
        quote = quote_for(quotes_payload, ticker)
        scores, notes = candidate_scores(ticker, support_signals, support_papers, quote, context)
        total = sum(scores.values())
        themes = sorted(
            set(
                theme.strip()
                for signal in support_signals
                for theme in signal.theme.split(";")
                if theme.strip()
            )
            | set(
                theme.strip()
                for paper in support_papers
                for theme in str(paper.get("mapped_themes", "")).split(";")
                if theme.strip()
            )
        )
        why_titles = [signal.title for signal in support_signals[:3]] + [str(paper.get("title", "")) for paper in support_papers[:2]]
        quote_change = finite_float(quote.get("changePercent")) if quote else None
        quote_price = finite_float(quote.get("price")) if quote else None
        row = {
            "run_date": run_date,
            "ticker": ticker,
            "company": info.get("company", COMPANY_OVERRIDES.get(ticker, (ticker, ""))[0]),
            "market": info.get("market", COMPANY_OVERRIDES.get(ticker, ("", "美股"))[1]) or ("A股" if re.search(r"\.(SZ|SS|BJ)$", ticker) else "美股"),
            "pool_presence": presence,
            "theme": "; ".join(themes[:4]),
            "chain_layer": info.get("chain_layer", "") or infer_layer(themes),
            "mapped_us": info.get("source_us", "") if re.search(r"\.(SZ|SS|BJ)$", ticker) else ticker,
            "why_now": " / ".join(clean_text(title, 120) for title in why_titles if title),
            **scores,
            "total_score": total,
            "recommendation": recommendation(total, presence, support_signals, support_papers),
            "review_status": "pending",
            "supporting_signals": "; ".join(signal.signal_id for signal in support_signals[:12]),
            "supporting_papers": "; ".join(str(paper.get("arxiv_id", "")) for paper in support_papers[:12] if paper.get("arxiv_id")),
            "price": round(quote_price, 4) if quote_price is not None else "",
            "change_percent": round(quote_change, 4) if quote_change is not None else "",
            "quote_timestamp": quote.get("timestamp", "") if quote else "",
            "notes": "；".join(dict.fromkeys(notes)),
        }
        rows.append(row)

    rows.sort(key=lambda row: (int(row["total_score"]), row["recommendation"] == "already_in_pool"), reverse=True)
    return rows


def infer_layer(themes: Iterable[str]) -> str:
    for theme in themes:
        rule = theme_rule(theme)
        if rule and rule.get("default_layer"):
            return str(rule["default_layer"])
    return ""


def quote_market_summary(quotes_payload: dict[str, object], limit: int = 8) -> tuple[list[tuple[str, float, dict[str, object]]], list[tuple[str, float, dict[str, object]]]]:
    quotes = quotes_payload.get("quotes", {})
    rows: list[tuple[str, float, dict[str, object]]] = []
    if isinstance(quotes, dict):
        for ticker, quote in quotes.items():
            if isinstance(quote, dict):
                change = finite_float(quote.get("changePercent"))
                if change is not None:
                    rows.append((str(ticker), change, quote))
    rows.sort(key=lambda item: item[1], reverse=True)
    return rows[:limit], rows[-limit:][::-1]


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    if not rows:
        return "_无_"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(clean_text(cell, 180).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def write_report(
    path: Path,
    signals: list[Signal],
    papers: list[dict[str, object]],
    candidates: list[dict[str, object]],
    quotes_payload: dict[str, object],
    warnings: list[str],
    context: dict[str, object],
    report_date: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pool_rows: list[dict[str, str]] = context["pool_rows"]  # type: ignore[assignment]
    top_gainers, top_losers = quote_market_summary(quotes_payload)
    quote_requested = quotes_payload.get("_online_requested", quotes_payload.get("requested", ""))
    quote_received = quotes_payload.get("_online_received", quotes_payload.get("received", ""))
    quote_available = quotes_payload.get("_quote_available_count", len(quotes_payload.get("quotes", {})) if isinstance(quotes_payload.get("quotes", {}), dict) else "")
    extra_quote_count = quotes_payload.get("_extra_quote_count", 0)
    quote_missing = quotes_payload.get("missing", [])

    proposed = [row for row in candidates if row.get("recommendation") == "propose_add"]
    observe = [row for row in candidates if row.get("recommendation") == "observe"]
    in_pool = [row for row in candidates if row.get("recommendation") == "already_in_pool"]

    content = [
        f"# 主动探索报告 - {report_date}",
        "",
        "## 摘要",
        "",
        f"- 当前线上合并股票池：{len(pool_rows)} 只。",
        f"- 线上股票池行情：requested={quote_requested}，received={quote_received}，missing={quote_missing or []}。",
        f"- 候选补充行情：extra={extra_quote_count}，本次可用报价={quote_available}。",
        f"- 本次新增/更新信号：{len(signals)} 条；arXiv 论文：{len(papers)} 篇；候选标的：{len(candidates)} 个。",
        "- 规则：本报告只生成研究候选，不自动修改正式股票池。",
        "",
        "## 建议新增候选",
        "",
        markdown_table(
            ["代码", "公司", "市场", "主题", "总分", "建议", "为什么现在"],
            [
                [
                    row.get("ticker", ""),
                    row.get("company", ""),
                    row.get("market", ""),
                    row.get("theme", ""),
                    row.get("total_score", ""),
                    row.get("recommendation", ""),
                    row.get("why_now", ""),
                ]
                for row in proposed[:12]
            ],
        ),
        "",
        "## 观察候选",
        "",
        markdown_table(
            ["代码", "公司", "市场", "池状态", "主题", "总分", "备注"],
            [
                [
                    row.get("ticker", ""),
                    row.get("company", ""),
                    row.get("market", ""),
                    row.get("pool_presence", ""),
                    row.get("theme", ""),
                    row.get("total_score", ""),
                    row.get("notes", ""),
                ]
                for row in observe[:18]
            ],
        ),
        "",
        "## 已在池内但信号增强的标的",
        "",
        markdown_table(
            ["代码", "公司", "主题", "总分", "日涨跌", "信号"],
            [
                [
                    row.get("ticker", ""),
                    row.get("company", ""),
                    row.get("theme", ""),
                    row.get("total_score", ""),
                    row.get("change_percent", ""),
                    row.get("why_now", ""),
                ]
                for row in in_pool[:18]
            ],
        ),
        "",
        "## 高相关官方/新闻信号",
        "",
        markdown_table(
            ["日期", "来源", "主题", "强度", "标题", "链接"],
            [
                [signal.date, signal.source_name, signal.theme, signal.evidence_strength, signal.title, signal.source_url]
                for signal in signals
                if signal.source_type in {"official", "news"}
            ][:18],
        ),
        "",
        "## 高相关 arXiv 论文",
        "",
        markdown_table(
            ["日期", "主题", "分数", "标题", "映射", "链接"],
            [
                [
                    paper.get("published", ""),
                    paper.get("mapped_themes", ""),
                    paper.get("paper_signal_score", ""),
                    paper.get("title", ""),
                    paper.get("mapped_tickers", ""),
                    paper.get("abs_url", ""),
                ]
                for paper in papers[:18]
            ],
        ),
        "",
        "## 行情位置",
        "",
        "### 涨幅靠前",
        "",
        markdown_table(
            ["代码", "涨跌幅", "价格", "市场"],
            [[ticker, change, quote.get("price", ""), quote.get("market", "")] for ticker, change, quote in top_gainers],
        ),
        "",
        "### 跌幅靠前",
        "",
        markdown_table(
            ["代码", "涨跌幅", "价格", "市场"],
            [[ticker, change, quote.get("price", ""), quote.get("market", "")] for ticker, change, quote in top_losers],
        ),
        "",
        "## 输出文件",
        "",
        f"- `{SIGNALS_FILE.name}`",
        f"- `{PAPERS_FILE.name}`",
        f"- `{CANDIDATES_FILE.name}`",
        "",
        "## 运行警告",
        "",
        markdown_table(["warning"], [[warning] for warning in warnings]) if warnings else "_无_",
        "",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate active discovery candidates for the stock-pool dashboard.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window for feeds and papers.")
    parser.add_argument("--max-arxiv-results", type=int, default=10, help="Max arXiv results per query.")
    parser.add_argument("--max-feed-items", type=int, default=20, help="Max entries to process per feed/query.")
    parser.add_argument("--max-extra-quotes", type=int, default=40, help="Max non-online-pool tickers to enrich with Yahoo quotes.")
    parser.add_argument("--arxiv-delay", type=float, default=3.0, help="Delay between arXiv API calls.")
    parser.add_argument("--report-date", default=report_today(), help="Report date suffix; defaults to Asia/Shanghai.")
    parser.add_argument("--fresh", action="store_true", help="Ignore existing discovery CSVs and rebuild from this run only.")
    parser.add_argument("--skip-network", action="store_true", help="Only rebuild candidates/report from existing CSVs.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    generated_at = now_utc().isoformat()
    aliases = load_aliases()
    context = build_pool_context()
    warnings: list[str] = []

    if args.skip_network:
        existing_signals = [Signal(**{field: row.get(field, "") for field in SIGNAL_FIELDS}) for row in read_csv_rows(SIGNALS_FILE)]
        papers = read_csv_rows(PAPERS_FILE)
        data_date = latest_discovery_date() or args.report_date
        quote_payload, quote_warnings = fetch_quotes()
        warnings.extend(quote_warnings)
        warnings.extend(enrich_quotes(quote_payload, collect_signal_tickers(existing_signals, papers), args.max_extra_quotes))
        candidates = build_candidates(existing_signals, papers, quote_payload, context, data_date)
        write_csv_rows(CANDIDATES_FILE, CANDIDATE_FIELDS, candidates)
        report_path = REPORTS_DIR / f"discovery-{args.report_date}.md"
        write_report(report_path, existing_signals, papers, candidates, quote_payload, warnings, context, args.report_date)
        write_discovery_status(
            status="reused",
            attempted_at=generated_at,
            attempted_date=args.report_date,
            data_date=data_date,
            signals=len(existing_signals),
            papers=len(papers),
            candidates=len(candidates),
            warnings=warnings,
            message="本次仅重建候选与报告，沿用已有发现数据。",
        )
        print_summary(existing_signals, papers, candidates, report_path, warnings)
        return 0

    feed_signals, feed_warnings = fetch_official_and_news(
        args.days,
        aliases,
        context,
        generated_at,
        max_items_per_source=args.max_feed_items,
    )
    warnings.extend(feed_warnings)

    cn_signals, cn_warnings = fetch_cn_flash_news(
        args.days,
        aliases,
        context,
        generated_at,
        max_items_per_source=args.max_feed_items,
    )
    warnings.extend(cn_warnings)

    papers, paper_signals, arxiv_warnings = fetch_arxiv_papers(
        days=args.days,
        max_results=args.max_arxiv_results,
        delay_seconds=args.arxiv_delay,
        aliases=aliases,
        context=context,
    )
    warnings.extend(arxiv_warnings)

    quote_payload, quote_warnings = fetch_quotes()
    warnings.extend(quote_warnings)
    extra_quote_warnings = enrich_quotes(
        quote_payload,
        collect_signal_tickers(feed_signals + cn_signals + paper_signals, papers),
        max_extra=args.max_extra_quotes,
    )
    warnings.extend(extra_quote_warnings)

    all_signals = feed_signals + cn_signals + paper_signals
    existing_signals = [] if args.fresh else read_csv_rows(SIGNALS_FILE)
    existing_papers = [] if args.fresh else read_csv_rows(PAPERS_FILE)
    merged_signals = merge_rows(existing_signals, [signal.as_row() for signal in all_signals], "signal_id")
    merged_papers = merge_rows(existing_papers, papers, "arxiv_id")
    merged_signals.sort(key=lambda row: (str(row.get("date", "")), int(row.get("evidence_strength", 0) or 0)), reverse=True)
    merged_papers.sort(key=lambda row: (str(row.get("published", "")), int(row.get("paper_signal_score", 0) or 0)), reverse=True)

    signal_objects = [Signal(**{field: row.get(field, "") for field in SIGNAL_FIELDS}) for row in merged_signals]
    candidates = build_candidates(signal_objects, merged_papers, quote_payload, context, args.report_date)

    if not discovery_result_is_valid(all_signals, candidates):
        report_path = REPORTS_DIR / f"discovery-{args.report_date}.md"
        write_report(report_path, all_signals, papers, candidates, quote_payload, warnings, context, args.report_date)
        write_discovery_status(
            status="failed",
            attempted_at=generated_at,
            attempted_date=args.report_date,
            data_date=latest_discovery_date(),
            signals=len(all_signals),
            papers=len(papers),
            candidates=len(candidates),
            warnings=warnings,
            message="外部数据抓取未形成有效结果，已保留上一次有效数据。",
        )
        print_summary(all_signals, papers, candidates, report_path, warnings)
        print("Safety gate: refusing to replace valid discovery artifacts with an empty result.", file=sys.stderr)
        return 2

    write_csv_rows(SIGNALS_FILE, SIGNAL_FIELDS, merged_signals)
    write_csv_rows(PAPERS_FILE, PAPER_FIELDS, merged_papers)
    write_csv_rows(CANDIDATES_FILE, CANDIDATE_FIELDS, candidates)
    report_path = REPORTS_DIR / f"discovery-{args.report_date}.md"
    write_report(report_path, signal_objects, merged_papers, candidates, quote_payload, warnings, context, args.report_date)
    update_discovery_history(args.report_date, context, quote_payload, signal_objects, merged_papers, candidates)
    write_discovery_status(
        status="success",
        attempted_at=generated_at,
        attempted_date=args.report_date,
        data_date=args.report_date,
        signals=len(signal_objects),
        papers=len(merged_papers),
        candidates=len(candidates),
        warnings=warnings,
        message="主动发现数据已更新。",
    )
    print_summary(signal_objects, merged_papers, candidates, report_path, warnings)
    return 0


def print_summary(signals: list[Signal], papers: list[dict[str, object]], candidates: list[dict[str, object]], report_path: Path, warnings: list[str]) -> None:
    rec_counts = Counter(str(row.get("recommendation", "")) for row in candidates)
    print(f"Signals: {len(signals)}")
    print(f"arXiv papers: {len(papers)}")
    print(f"Candidates: {len(candidates)} {dict(rec_counts)}")
    print(f"Report: {report_path}")
    if warnings:
        print("Warnings:")
        for warning in warnings[:8]:
            print(f"- {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
