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

OUT = Path("organ_audit/taiwan_brokerage/debug")
OUT.mkdir(parents=True, exist_ok=True)
OFFICIAL_ID = "CHDM,113,金訴,657,20250724,1"
SOURCES = [
    "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=" + quote(OFFICIAL_ID, safe=","),
    "https://judgment.judicial.gov.tw/FJUD/printData.aspx?id=" + quote(OFFICIAL_ID, safe=","),
    "https://top-lawyer1111.com/content/" + quote(OFFICIAL_ID, safe=""),
]
DATE_RE = re.compile(r"\d{2,3}年\d{1,2}月\d{1,2}日(?:至同年\d{1,2}月\d{1,2}日間某日)?")
ORGAN_RE = re.compile(r"(?:肝臟|腎臟|心臟|肺臟|胰臟)(?:手術)?移植")


def normalize(value: object) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def fetch_html() -> tuple[requests.Response, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for url in SOURCES:
        for attempt in range(1, 4):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; court-table-audit/1.0)",
                        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                    },
                )
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding
                if "113年度金訴字第657號" not in response.text:
                    raise ValueError("case number absent from response")
                return response, errors
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
        text = normalize(getattr(node, "string", node if isinstance(node, str) else ""))
        match = re.search(r"附表[一二三四]", text)
        if match:
            return match.group(0)
    return ""


def main() -> None:
    response, errors = fetch_html()
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    report = {
        "url": response.url,
        "html_sha256": hashlib.sha256(response.content).hexdigest(),
        "fetch_errors_before_success": errors,
        "html_table_count": len(soup.find_all("table")),
        "tables": [],
    }

    for index, tag in enumerate(soup.find_all("table"), start=1):
        text = normalize(tag.get_text(" ", strip=True))
        dates = sorted(set(DATE_RE.findall(text)))
        organs = sorted(set(ORGAN_RE.findall(text)))
        try:
            frames = pd.read_html(StringIO(str(tag)), flavor="lxml")
            frame_shapes = [list(frame.shape) for frame in frames]
            columns = [[normalize(col) for col in frame.columns.tolist()] for frame in frames]
        except ValueError:
            frame_shapes = []
            columns = []
        report["tables"].append(
            {
                "table_index": index,
                "nearest_appendix_label": nearest_label(tag),
                "contains_patient_header": "病患姓名" in text,
                "contains_transplant_time_header": "移植時間" in text,
                "text_length": len(text),
                "frame_shapes": frame_shapes,
                "columns": columns,
                "detected_dates": dates,
                "detected_organs": organs,
                "appendix_labels_inside": sorted(set(re.findall(r"附表[一二三四]", text))),
            }
        )

    (OUT / "table_structure.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
