from __future__ import annotations

"""Extract sentence-level issue and decision reasoning from the 2026 Chen Yao-li appeal."""

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

ISSUE_TERMS = (
    "活摘器官", "活摘", "器官來源不明", "來源不明", "器官提供人",
    "器官捐贈", "捐贈者", "器官買賣", "器官來源", "無證據",
    "伊斯坦堡宣言", "世界衛生組織", "不宜宣告緩刑",
)
DECISION_TERMS = (
    "本院認為", "本院審酌", "本院查", "綜上", "駁回", "上訴無理由",
    "上訴為無理由", "應予維持", "原判決", "緩刑宣告", "緩刑之宣告",
    "量刑", "強摘", "無證據", "器官來源", "器官買賣",
)
DECISION_START_MARKERS = (
    "本院之判斷", "本院判斷", "本院認定", "駁回上訴部分",
    "上訴駁回部分", "本院審酌", "本院查", "本院認為",
)


def redact(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        value,
    )
    return value


def select_blocks(
    units: list[str],
    terms: tuple[str, ...],
    *,
    start_index: int = 0,
    context_before: int = 2,
    context_after: int = 3,
) -> list[dict[str, object]]:
    selected: set[int] = set()
    for index in range(start_index, len(units)):
        unit = units[index]
        if any(term in unit for term in terms):
            selected.update(
                range(
                    max(start_index, index - context_before),
                    min(len(units), index + context_after),
                )
            )

    blocks: list[list[int]] = []
    for index in sorted(selected):
        if not blocks or index > blocks[-1][-1] + 1:
            blocks.append([index])
        else:
            blocks[-1].append(index)

    passages: list[dict[str, object]] = []
    for number, block in enumerate(blocks, start=1):
        raw = " ".join(units[index] for index in block)
        passages.append(
            {
                "passage_number": number,
                "unit_start": block[0],
                "unit_end": block[-1],
                "matched_terms": sorted(term for term in terms if term in raw),
                "text_redacted": redact(raw),
            }
        )
    return passages


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; appeal-reasoning-audit/1.1; +https://github.com/chriszavadil/PythonCode)",
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

    # Sentence-like units preserve sequence without republishing the whole judgment.
    units = [item.strip() for item in re.split(r"(?<=[。；：])", text) if item.strip()]

    issue_passages = select_blocks(units, ISSUE_TERMS)

    marker_indices = [
        index
        for index, unit in enumerate(units)
        if index >= len(units) // 2 and any(marker in unit for marker in DECISION_START_MARKERS)
    ]
    decision_start = min(marker_indices) if marker_indices else int(len(units) * 0.65)
    decision_passages = select_blocks(
        units,
        DECISION_TERMS,
        start_index=decision_start,
        context_before=3,
        context_after=4,
    )

    # Include a bounded, redacted tail for independent checking if headings are unusual.
    tail_start = max(decision_start, len(units) - 80)
    decision_tail = redact(" ".join(units[tail_start:]))

    output = {
        "generated_on": date.today().isoformat(),
        "judgment_id": JUDGMENT_ID,
        "public_url": f"{DETAIL}?ty=JD&id={quote(JUDGMENT_ID, safe=',')}&ot=in",
        "unit_count": len(units),
        "decision_start_unit": decision_start,
        "issue_passages": issue_passages,
        "decision_passages": decision_passages,
        "decision_tail_redacted": decision_tail,
        "interpretation_limit": (
            "This appeal addressed suspended sentences. The underlying facts and convictions were not within the appellate scope. "
            "Quoted allegations by the prosecutor or defense are not court findings unless the decision section expressly adopts them."
        ),
    }
    (OUT / "appeal_reasoning.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
