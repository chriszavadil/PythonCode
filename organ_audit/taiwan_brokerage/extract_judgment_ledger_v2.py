from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUT = Path("organ_audit/taiwan_brokerage/output_v2")
OUT.mkdir(parents=True, exist_ok=True)
CASE_ID = "CHDM-113-JINSU-657-20250724"
OFFICIAL_ID = "CHDM,113,金訴,657,20250724,1"
SOURCES = [
    "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=" + quote(OFFICIAL_ID, safe=","),
    "https://judgment.judicial.gov.tw/FJUD/printData.aspx?id=" + quote(OFFICIAL_ID, safe=","),
    "https://top-lawyer1111.com/content/" + quote(OFFICIAL_ID, safe=""),
]
HOSPITAL_BY_APPENDIX = {
    "附表一": "Qingdao University Affiliated Hospital",
    "附表二": "Xiangya Third Hospital, Central South University",
}
DATE_RE = re.compile(
    r"(?P<year>\d{2,3})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日"
    r"(?:至同年(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日間某日)?"
)
ORGAN_RE = re.compile(r"(?P<organ>肝臟|腎臟|心臟|肺臟|胰臟)(?:手術)?移植")


def normalize(value: Any) -> str:
    return " ".join(str(value if value is not None else "").replace("\u3000", " ").split())


def fetch_source() -> tuple[requests.Response, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for url in SOURCES:
        for attempt in range(1, 4):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; court-ledger-audit/2.0)",
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


def nearest_appendix_label(table) -> str:
    node = table
    for _ in range(120):
        node = node.previous_element
        if node is None:
            break
        text = normalize(getattr(node, "string", node if isinstance(node, str) else ""))
        match = re.search(r"附表[一二]", text)
        if match:
            return match.group(0)
    return ""


def roc_date_window(text: str) -> tuple[str, str, str]:
    match = DATE_RE.search(text)
    if not match:
        return "", "", ""
    year = int(match.group("year")) + 1911
    start = f"{year:04d}-{int(match.group('m1')):02d}-{int(match.group('d1')):02d}"
    if match.group("m2") and match.group("d2"):
        end = f"{year:04d}-{int(match.group('m2')):02d}-{int(match.group('d2')):02d}"
        return start, end, "date-range"
    return start, start, "exact-date"


def patient_token(name: str) -> str:
    return hashlib.sha256((CASE_ID + "|" + name).encode("utf-8")).hexdigest()[:12]


def evidence_flags(text: str) -> dict[str, bool]:
    return {
        "has_bank_or_payment_records": any(term in text for term in ["匯款", "交易明細", "帳戶", "現金", "付款"]),
        "has_travel_records": any(term in text for term in ["入出境", "出入境", "搭機", "在陸病患時間比對"]),
        "has_medical_records": any(term in text for term in ["病歷", "抗排斥藥"]),
        "has_chat_or_notes": any(term in text for term in ["對話紀錄", "筆記"]),
        "has_witness_statement": any(term in text for term in ["證人", "供述"]),
    }


def extract_case_tables(soup: BeautifulSoup) -> list[tuple[str, pd.DataFrame]]:
    selected: list[tuple[str, pd.DataFrame]] = []
    for table in soup.find_all("table"):
        text = normalize(table.get_text(" ", strip=True))
        if "病患姓名" not in text or "移植時間" not in text:
            continue
        frames = pd.read_html(StringIO(str(table)), flavor="lxml")
        if not frames:
            continue
        frame = frames[0].fillna("")
        # The page has one outer layout table containing all appendices. Keep only
        # the two compact nested case tables (header + six rows; header + four rows).
        if frame.shape[1] != 10 or frame.shape[0] > 10:
            continue
        label = nearest_appendix_label(table)
        if label in HOSPITAL_BY_APPENDIX:
            selected.append((label, frame))
    return selected


def build_rows(case_tables: list[tuple[str, pd.DataFrame]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, frame in case_tables:
        for raw_index, raw in frame.iterrows():
            values = [normalize(value) for value in raw.tolist()]
            joined = " | ".join(values)
            organ_match = ORGAN_RE.search(joined)
            start, end, precision = roc_date_window(joined)
            if not organ_match or not start:
                continue
            patient_name = values[1] if len(values) > 1 else ""
            outcome = values[6] if len(values) > 6 else joined
            evidence_text = " | ".join(values[7:]) if len(values) > 7 else joined
            flags = evidence_flags(evidence_text)
            rows.append(
                {
                    "case_id": "",
                    "source_appendix": label,
                    "source_row_number": values[0] if values else str(raw_index),
                    "patient_token": patient_token(patient_name) if patient_name else "",
                    "hospital": HOSPITAL_BY_APPENDIX[label],
                    "organ": organ_match.group("organ"),
                    "transplant_date_start": start,
                    "transplant_date_end": end,
                    "date_precision": precision,
                    "outcome_deceased": "歿" in outcome or "死亡" in outcome,
                    "outcome_redacted": "deceased" if ("歿" in outcome or "死亡" in outcome) else ("alive" if "存活" in outcome else "not stated"),
                    **flags,
                }
            )

    rows.sort(key=lambda row: (row["transplant_date_start"], row["hospital"], row["organ"]))
    for index, row in enumerate(rows, start=1):
        row["case_id"] = f"{CASE_ID}-C{index:02d}"

    # Hard validation prevents a malformed page or nested-table duplication from
    # silently becoming an evidentiary claim.
    if len(rows) != 10:
        raise RuntimeError(f"Expected 10 appendix rows, extracted {len(rows)}")
    if len({(r['source_appendix'], r['source_row_number']) for r in rows}) != 10:
        raise RuntimeError("Duplicate appendix-row identifiers detected")
    return rows


def main() -> None:
    response, fetch_errors = fetch_source()
    soup = BeautifulSoup(response.text, "html.parser")
    case_tables = extract_case_tables(soup)
    rows = build_rows(case_tables)

    fields = [
        "case_id", "source_appendix", "source_row_number", "patient_token",
        "hospital", "organ", "transplant_date_start", "transplant_date_end",
        "date_precision", "outcome_deceased", "outcome_redacted",
        "has_bank_or_payment_records", "has_travel_records", "has_medical_records",
        "has_chat_or_notes", "has_witness_statement",
    ]
    with (OUT / "deidentified_case_ledger.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary: dict[str, Any] = {
        "generated_on": date.today().isoformat(),
        "case_number": "Taiwan Changhua District Court 113年度金訴字第657號",
        "source_url": response.url,
        "source_content_sha256": hashlib.sha256(response.content).hexdigest(),
        "source_bytes": len(response.content),
        "fetch_errors_before_success": fetch_errors,
        "compact_case_tables": len(case_tables),
        "deidentified_appendix_rows": len(rows),
        "hospital_counts": {},
        "organ_counts": {},
        "outcome_counts": {},
        "date_range": [rows[0]["transplant_date_start"], rows[-1]["transplant_date_end"]],
        "date_range_case_count": sum(1 for row in rows if row["date_precision"] == "date-range"),
        "privacy_note": "Names and diagnoses are omitted; patient_token is a one-way case-local hash.",
        "interpretation_limit": "The judgment establishes illegal brokerage and payments for these recipient operations. It does not identify donors or establish prisoner sourcing.",
    }
    for row in rows:
        summary["hospital_counts"][row["hospital"]] = summary["hospital_counts"].get(row["hospital"], 0) + 1
        summary["organ_counts"][row["organ"]] = summary["organ_counts"].get(row["organ"], 0) + 1
        summary["outcome_counts"][row["outcome_redacted"]] = summary["outcome_counts"].get(row["outcome_redacted"], 0) + 1
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
