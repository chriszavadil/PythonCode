from __future__ import annotations

"""Extract privacy-safe passages around donor-source and coercion terminology.

This is a focused primary-source pass over judgments that survived manual
triage. It distinguishes (1) the proven Taiwan-to-China brokerage prosecution,
(2) its 2026 sentencing appeal, and (3) separate scam-compound trafficking
cases whose chats discuss threatened organ removal. The categories must not be
conflated.
"""

import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://judgment.judicial.gov.tw"
DETAIL = f"{BASE}/FJUD/data.aspx"
OUT = Path("organ_audit/taiwan_court_corpus/focused")
OUT.mkdir(parents=True, exist_ok=True)

CASES = {
    "china_brokerage_trial": "CHDM,113,金訴,657,20250724,1",
    "china_brokerage_sentencing_appeal": "TCHM,114,金上訴,1935,20260211,1",
    "scam_compound_trial_503": "TNDM,114,訴,503,20251127,4",
    "scam_compound_trial_1628": "TNDM,114,訴,1628,20251127,1",
    "scam_compound_appeal_68": "TNHM,115,上訴,68,20260528,2",
    "scam_compound_appeal_69": "TNHM,115,上訴,69,20260528,1",
}

TERMS = [
    "活摘", "器官來源", "來源不明", "器官提供人", "器官捐贈", "捐贈者",
    "供體", "供體費", "器官買賣", "購買費用", "戒護人員", "伊斯坦堡宣言",
    "世界衛生組織", "緩刑", "證據", "未有證據", "無證據", "刑罰化",
    "拆器官", "拆解費", "人口販運", "美索", "詐欺園區",
]


def fetch(session: requests.Session, judgment_id: str) -> tuple[dict[str, str], bytes]:
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = session.get(
                DETAIL,
                params={"ty": "JD", "id": judgment_id, "ot": "in"},
                timeout=60,
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            soup = BeautifulSoup(response.text, "html.parser")
            meta = soup.select_one("div#jud")
            cells = meta.select("div.col-td") if meta else []
            body = soup.select_one("div.htmlcontent")
            time.sleep(0.35)
            return (
                {
                    "title": cells[0].get_text(" ", strip=True) if len(cells) > 0 else "",
                    "date": cells[1].get_text(" ", strip=True) if len(cells) > 1 else "",
                    "case_reason": cells[2].get_text(" ", strip=True) if len(cells) > 2 else "",
                    "content": body.get_text(" ", strip=True) if body else "",
                },
                response.content,
            )
        except requests.RequestException as exc:
            last = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {judgment_id}: {last!r}")


def redact(text: str) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    value = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(r"[A-Z]\d{8,10}", "[ID REDACTED]", value)
    value = re.sub(r"\b09\d{8}\b", "[PHONE REDACTED]", value)
    return value


def merge_intervals(intervals: list[tuple[int, int]], gap: int = 250) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        old_start, old_end = merged[-1]
        if start <= old_end + gap:
            merged[-1] = (old_start, max(old_end, end))
        else:
            merged.append((start, end))
    return merged


def passages(content: str) -> list[dict[str, object]]:
    text = re.sub(r"\s+", " ", content).strip()
    hits: list[tuple[int, int, str]] = []
    for term in TERMS:
        for match in re.finditer(re.escape(term), text):
            hits.append((match.start(), match.end(), term))
    intervals = sorted((max(0, start - 650), min(len(text), end + 950)) for start, end, _ in hits)
    merged = merge_intervals(intervals)

    rows: list[dict[str, object]] = []
    for index, (start, end) in enumerate(merged, start=1):
        segment = text[start:end]
        found = sorted({term for term in TERMS if term in segment})
        rows.append(
            {
                "passage_number": index,
                "matched_terms": " | ".join(found),
                "passage_redacted": redact(segment),
                "passage_sha256": hashlib.sha256(segment.encode("utf-8")).hexdigest(),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; focused-court-source-audit/1.0; +https://github.com/chriszavadil/PythonCode)",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        }
    )

    rows: list[dict[str, object]] = []
    case_summaries: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []

    for category, judgment_id in CASES.items():
        try:
            detail, raw = fetch(session, judgment_id)
            extracted = passages(detail["content"])
            counts = Counter(term for term in TERMS for _ in re.finditer(re.escape(term), detail["content"]))
            case_summaries.append(
                {
                    "category": category,
                    "judgment_id": judgment_id,
                    "title": detail["title"],
                    "date": detail["date"],
                    "case_reason": detail["case_reason"],
                    "passages": len(extracted),
                    "term_counts": dict(counts),
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                    "public_url": f"{DETAIL}?ty=JD&id={quote(judgment_id, safe=',')}&ot=in",
                }
            )
            for item in extracted:
                rows.append(
                    {
                        "category": category,
                        "judgment_id": judgment_id,
                        "title": detail["title"],
                        "date": detail["date"],
                        **item,
                    }
                )
        except Exception as exc:
            errors.append({"category": category, "judgment_id": judgment_id, "error": repr(exc)})

    write_csv(
        OUT / "focus_passages.csv",
        rows,
        [
            "category", "judgment_id", "title", "date", "passage_number",
            "matched_terms", "passage_redacted", "passage_sha256",
        ],
    )
    write_csv(OUT / "errors.csv", errors, ["category", "judgment_id", "error"])
    (OUT / "case_summaries.json").write_text(
        json.dumps(
            {
                "generated_on": date.today().isoformat(),
                "cases": case_summaries,
                "errors": errors,
                "interpretation_limit": (
                    "The China brokerage judgments establish brokerage and payments, not donor custody status. "
                    "The scam-compound judgments establish chats and trafficking conduct, not a completed transplant."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if errors:
        raise RuntimeError(f"focused extraction errors: {errors!r}")


if __name__ == "__main__":
    main()
