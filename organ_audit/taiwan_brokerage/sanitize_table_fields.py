from __future__ import annotations

import hashlib
import json
import re
import time
from io import StringIO
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUT = Path("organ_audit/taiwan_brokerage/field_audit")
OUT.mkdir(parents=True, exist_ok=True)
OFFICIAL_ID = "CHDM,113,金訴,657,20250724,1"
SOURCES = [
    "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=" + quote(OFFICIAL_ID, safe=","),
    "https://judgment.judicial.gov.tw/FJUD/printData.aspx?id=" + quote(OFFICIAL_ID, safe=","),
    "https://top-lawyer1111.com/content/" + quote(OFFICIAL_ID, safe=""),
]
DATE_RE = re.compile(r"\d{2,3}年\d{1,2}月\d{1,2}日(?:至同年\d{1,2}月\d{1,2}日間某日)?")
MONEY_RE = re.compile(r"(?:人民幣|新臺幣|美金|美元)?\s*[0-9０-９,，萬万]+(?:元|萬元|萬餘元|餘元)")
ORGAN_RE = re.compile(r"肝臟|腎臟|心臟|肺臟|胰臟")
PUBLIC_MARKERS = [
    "移植", "匯款", "現金", "帳戶", "交易明細", "入出境", "出入境", "病歷",
    "抗排斥藥", "對話紀錄", "筆記", "證人", "供述", "存活", "死亡", "歿",
    "青島大學附屬醫院", "湘雅三醫院", "青島大學附屬醫院", "中南大學湘雅三醫院",
]


def norm(value: object) -> str:
    return " ".join(str(value if value is not None else "").replace("\u3000", " ").split())


def fetch() -> requests.Response:
    errors: list[dict[str, str]] = []
    for url in SOURCES:
        for attempt in range(1, 4):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; court-field-audit/1.0)",
                        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                    },
                )
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding
                if "113年度金訴字第657號" not in response.text:
                    raise ValueError("case number missing")
                return response
            except Exception as exc:
                errors.append({"url": url, "attempt": str(attempt), "error": repr(exc)})
                time.sleep(attempt)
    raise RuntimeError(json.dumps(errors, ensure_ascii=False, indent=2))


def nearest_label(table) -> str:
    node = table
    for _ in range(120):
        node = node.previous_element
        if node is None:
            break
        text = norm(getattr(node, "string", node if isinstance(node, str) else ""))
        match = re.search(r"附表[一二]", text)
        if match:
            return match.group(0)
    return ""


def sanitize_cell(value: object) -> dict[str, object]:
    text = norm(value)
    return {
        "length": len(text),
        "dates": DATE_RE.findall(text),
        "money": MONEY_RE.findall(text),
        "organs": sorted(set(ORGAN_RE.findall(text))),
        "markers": [marker for marker in PUBLIC_MARKERS if marker in text],
    }


def main() -> None:
    response = fetch()
    soup = BeautifulSoup(response.text, "html.parser")
    tables: list[dict[str, object]] = []

    for tag in soup.find_all("table"):
        text = norm(tag.get_text(" ", strip=True))
        if "病患姓名" not in text or "移植時間" not in text:
            continue
        frames = pd.read_html(StringIO(str(tag)), flavor="lxml")
        if not frames:
            continue
        frame = frames[0].fillna("")
        if frame.shape[1] != 10 or frame.shape[0] > 10:
            continue
        label = nearest_label(tag)
        if label not in {"附表一", "附表二"}:
            continue
        row_reports: list[dict[str, object]] = []
        for idx, row in frame.iterrows():
            values = row.tolist()
            row_reports.append(
                {
                    "row_index": int(idx) if isinstance(idx, int) else str(idx),
                    "cells": [sanitize_cell(value) for value in values],
                }
            )
        tables.append(
            {
                "appendix": label,
                "shape": list(frame.shape),
                "column_labels": [norm(column) for column in frame.columns.tolist()],
                "rows": row_reports,
            }
        )

    report = {
        "source_url": response.url,
        "source_sha256": hashlib.sha256(response.content).hexdigest(),
        "privacy": "Names, diagnoses, addresses, phone numbers, and unrestricted cell text are omitted.",
        "tables": tables,
    }
    (OUT / "sanitized_field_map.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
