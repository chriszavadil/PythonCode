from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

OUT = Path("organ_audit/transport_trace/output")
OUT.mkdir(parents=True, exist_ok=True)

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERIES = [
    '"human organ" transport airport China',
    '"donated organ" transport flight China',
    'organ transplant "green channel" China airport',
    'organ transport airline China transplant',
    '人体捐献器官 转运 绿色通道',
    '人体捐献器官 航空运输',
]

ORGAN_TERMS = {
    "heart": ["heart", "心脏", "供心"],
    "liver": ["liver", "肝脏", "供肝"],
    "kidney": ["kidney", "肾脏", "供肾"],
    "lung": ["lung", "肺脏", "供肺"],
    "pancreas": ["pancreas", "胰腺"],
    "intestine": ["intestine", "小肠"],
    "cornea": ["cornea", "角膜"],
}

CITY_TERMS = [
    "北京", "上海", "广州", "深圳", "武汉", "南京", "天津", "杭州", "成都", "重庆",
    "西安", "长沙", "郑州", "济南", "青岛", "合肥", "福州", "厦门", "南昌", "南宁",
    "昆明", "贵阳", "兰州", "乌鲁木齐", "拉萨", "哈尔滨", "长春", "沈阳", "石家庄",
    "太原", "呼和浩特", "海口", "三亚", "无锡", "苏州", "宁波", "温州", "徐州",
    "大连", "珠海", "兰州", "银川", "西宁", "喀什", "克拉玛依",
]

FLIGHT_RE = re.compile(r"\b(?:[A-Z0-9]{2}|[A-Z]{2,3})[- ]?\d{3,4}\b")
DATE_CN_RE = re.compile(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日")


def gdelt_articles(query: str, start: str, end: str) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": 250,
        "format": "json",
        "sort": "datedesc",
        "startdatetime": start,
        "enddatetime": end,
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
    response = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0 research-audit/1.0"})
    response.raise_for_status()
    payload = response.json()
    return payload.get("articles", [])


def fetch_text(url: str) -> str:
    response = requests.get(
        url,
        timeout=35,
        allow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; public-record-research/1.0)"},
    )
    response.raise_for_status()
    if "text" not in response.headers.get("content-type", "").lower() and "html" not in response.headers.get("content-type", "").lower():
        return ""
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return text[:250_000]


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def classify(text: str) -> dict[str, Any]:
    organ_types = [name for name, terms in ORGAN_TERMS.items() if contains_any(text, terms)]
    cities = [city for city in CITY_TERMS if city in text]
    flights = sorted(set(m.group(0).replace(" ", "") for m in FLIGHT_RE.finditer(text)))
    dates = sorted(set(DATE_CN_RE.findall(text)))
    return {
        "organ_types": organ_types,
        "cities": cities,
        "flight_numbers": flights,
        "date_mentions": dates[:20],
        "mentions_green_channel": contains_any(text, ["green channel", "绿色通道", "生命通道"]),
        "mentions_notification_form": contains_any(text, ["运输通知单", "transport notification", "专用标志"]),
        "mentions_cotrs": contains_any(text, ["COTRS", "中国人体器官分配与共享计算机系统"]),
        "mentions_red_cross": contains_any(text, ["红十字", "Red Cross"]),
        "mentions_opo": contains_any(text, ["OPO", "器官获取组织"]),
    }


def main() -> None:
    raw: list[dict[str, Any]] = []
    years = range(2017, date.today().year + 1)
    errors: list[dict[str, str]] = []

    for year in years:
        start = f"{year}0101000000"
        end = f"{year}1231235959"
        for query in QUERIES:
            try:
                for article in gdelt_articles(query, start, end):
                    item = dict(article)
                    item["query"] = query
                    item["query_year"] = year
                    raw.append(item)
            except Exception as exc:  # preserve failures for auditability
                errors.append({"stage": "gdelt", "query": query, "year": str(year), "error": repr(exc)})
            time.sleep(5.5)

    by_url: dict[str, dict[str, Any]] = {}
    for item in raw:
        url = item.get("url") or ""
        if not url:
            continue
        existing = by_url.setdefault(url, dict(item))
        queries = set(existing.get("matched_queries", []))
        queries.add(item.get("query", ""))
        existing["matched_queries"] = sorted(q for q in queries if q)

    records: list[dict[str, Any]] = []
    for index, (url, item) in enumerate(sorted(by_url.items()), start=1):
        text = ""
        fetch_error = ""
        try:
            text = fetch_text(url)
        except Exception as exc:
            fetch_error = repr(exc)
            errors.append({"stage": "article", "url": url, "error": fetch_error})
        fields = classify(" ".join([item.get("title", ""), text]))
        records.append(
            {
                "url": url,
                "title": item.get("title", ""),
                "seendate": item.get("seendate", ""),
                "domain": item.get("domain", ""),
                "language": item.get("language", ""),
                "sourcecountry": item.get("sourcecountry", ""),
                "matched_queries": " | ".join(item.get("matched_queries", [])),
                "text_recovered": bool(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
                "text_chars": len(text),
                "organ_types": " | ".join(fields["organ_types"]),
                "cities": " | ".join(fields["cities"]),
                "flight_numbers": " | ".join(fields["flight_numbers"]),
                "date_mentions": " | ".join(fields["date_mentions"]),
                "mentions_green_channel": fields["mentions_green_channel"],
                "mentions_notification_form": fields["mentions_notification_form"],
                "mentions_cotrs": fields["mentions_cotrs"],
                "mentions_red_cross": fields["mentions_red_cross"],
                "mentions_opo": fields["mentions_opo"],
                "fetch_error": fetch_error,
                "text_excerpt": text[:1200],
            }
        )
        if index % 20 == 0:
            time.sleep(2)

    fieldnames = list(records[0].keys()) if records else ["url"]
    with (OUT / "transport_traces.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    with (OUT / "errors.json").open("w", encoding="utf-8") as handle:
        json.dump(errors, handle, ensure_ascii=False, indent=2)

    summary = {
        "generated_on": date.today().isoformat(),
        "query_count": len(QUERIES),
        "raw_article_hits": len(raw),
        "unique_urls": len(records),
        "full_text_recovered": sum(bool(r["text_recovered"]) for r in records),
        "green_channel_mentions": sum(bool(r["mentions_green_channel"]) for r in records),
        "notification_form_mentions": sum(bool(r["mentions_notification_form"]) for r in records),
        "cotrs_mentions": sum(bool(r["mentions_cotrs"]) for r in records),
        "red_cross_mentions": sum(bool(r["mentions_red_cross"]) for r in records),
        "opo_mentions": sum(bool(r["mentions_opo"]) for r in records),
        "organ_type_counts": Counter(
            organ for r in records for organ in str(r["organ_types"]).split(" | ") if organ
        ),
        "city_counts": Counter(city for r in records for city in str(r["cities"]).split(" | ") if city),
        "domains": Counter(str(r["domain"]) for r in records if r["domain"]).most_common(30),
        "limitations": [
            "GDELT and public web indexing are incomplete and this dataset is not a census of all transport events.",
            "One transport event may carry multiple organs, and one donor may generate multiple transport legs.",
            "Operational traces cannot establish donor consent or custody status without linked source records.",
            "Entity extraction is heuristic and every high-value match requires manual verification.",
        ],
    }
    with (OUT / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=lambda x: dict(x))


if __name__ == "__main__":
    main()
