from __future__ import annotations

"""Extract sentence-level reasoning from the 2026 Chen Yao-li sentencing appeal."""

import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

JUDGMENT_ID = "TCHM,114,金上訴,1935,20260211,1"
BASE = "https://judgment.judicial.gov.tw"
DETAIL = f"{BASE}/FJUD/data.aspx"
OUT = Path("organ_audit/taiwan_court_corpus/focused_appeal")
OUT.mkdir(parents=True, exist_ok=True)

TERMS = (
    "活摘器官", "活摘", "器官來源不明", "來源不明", "器官提供人",
    "器官捐贈", "捐贈者", "器官買賣", "器官來源", "無證據",
    "伊斯坦堡宣言", "世界衛生組織", "不宜宣告緩刑",
)


def redact(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        value,
    )
    return value


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; appeal-reasoning-audit/1.0; +https://github.com/chriszavadil/PythonCode)",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        }
    )
    response = session.get(
        DETAIL,
        params={"ty": "JD", "id": JUDGMENT_ID, "ot": "in"},
        timeout=60,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "html.parser")
    body = soup.select_one("div.htmlcontent")
    if not body:
        raise RuntimeError("judgment body missing")
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    time.sleep(0.35)

    # Sentence-like units preserve the court's sequence without publishing the
    # entire judgment. Include two neighboring units for context.
    units = [item.strip() for item in re.split(r"(?<=[。；：])", text) if item.strip()]
    selected: set[int] = set()
    for index, unit in enumerate(units):
        if any(term in unit for term in TERMS):
            selected.update(range(max(0, index - 2), min(len(units), index + 3)))

    blocks: list[list[int]] = []
    for index in sorted(selected):
        if not blocks or index > blocks[-1][-1] + 1:
            blocks.append([index])
        else:
            blocks[-1].append(index)

    passages = []
    for number, block in enumerate(blocks, start=1):
        raw = " ".join(units[index] for index in block)
        passages.append(
            {
                "passage_number": number,
                "matched_terms": sorted(term for term in TERMS if term in raw),
                "text_redacted": redact(raw),
            }
        )

    output = {
        "generated_on": date.today().isoformat(),
        "judgment_id": JUDGMENT_ID,
        "public_url": f"{DETAIL}?ty=JD&id={quote(JUDGMENT_ID, safe=',')}&ot=in",
        "passages": passages,
        "interpretation_limit": (
            "This appeal addressed suspended sentences. The underlying facts and convictions were not within the appellate scope. "
            "Quoted allegations by the prosecutor are not findings unless the court expressly adopts them."
        ),
    }
    (OUT / "appeal_reasoning.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
