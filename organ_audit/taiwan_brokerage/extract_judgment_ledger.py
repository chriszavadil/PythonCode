from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUT = Path("organ_audit/taiwan_brokerage/output")
OUT.mkdir(parents=True, exist_ok=True)

CASE_ID = "CHDM-113-JINSU-657-20250724"
OFFICIAL_ID = "CHDM,113,金訴,657,20250724,1"
SOURCES = [
    "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=" + quote(OFFICIAL_ID, safe=","),
    "https://judgment.judicial.gov.tw/FJUD/printData.aspx?id=" + quote(OFFICIAL_ID, safe=","),
    "https://top-lawyer1111.com/content/" + quote(OFFICIAL_ID, safe=""),
]

ROC_DATE_RE = re.compile(r"(?P<year>\d{2,3})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
ORGAN_RE = re.compile(r"(?P<organ>肝臟|腎臟|心臟|肺臟|胰臟)(?:手術)?移植")
MONEY_RE = re.compile(r"(?P<amount>\d+(?:萬)?(?:\d{1,4})?)元")

HOSPITAL_BY_TABLE = {
    "附表一": "Qingdao University Affiliated Hospital",
    "附表二": "Xiangya Third Hospital, Central South University",
}


def fetch_source() -> tuple[str, str, dict[str, Any]]:
    errors: list[dict[str, str]] = []
    for url in SOURCES:
        try:
            response = requests.get(
                url,
                timeout=45,
                allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; court-record-audit/1.0)",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                },
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            html = response.text
            if "113年度金訴字第657號" not in html and "113年度金訴字第657號" not in BeautifulSoup(html, "html.parser").get_text():
                errors.append({"url": url, "error": "case number not present in response"})
                continue
            meta = {
                "selected_url": response.url,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "html_bytes": len(response.content),
                "html_sha256": hashlib.sha256(response.content).hexdigest(),
                "attempt_errors": errors,
            }
            return response.url, html, meta
        except Exception as exc:
            errors.append({"url": url, "error": repr(exc)})
    raise RuntimeError(json.dumps(errors, ensure_ascii=False, indent=2))


def normalize(value: Any) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def roc_to_iso(text: str) -> str:
    match = ROC_DATE_RE.search(text)
    if not match:
        return ""
    year = int(match.group("year")) + 1911
    return f"{year:04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"


def patient_token(name: str) -> str:
    return hashlib.sha256((CASE_ID + "|" + name).encode("utf-8")).hexdigest()[:12]


def evidence_flags(text: str) -> dict[str, bool]:
    return {
        "has_bank_or_payment_records": any(term in text for term in ["匯款", "交易明細", "帳戶", "現金"]),
        "has_travel_records": any(term in text for term in ["入出境", "出入境", "搭機"]),
        "has_medical_records": any(term in text for term in ["病歷", "抗排斥藥"]),
        "has_chat_or_notes": any(term in text for term in ["對話紀錄", "筆記"]),
        "has_witness_statement": any(term in text for term in ["證人", "供述"]),
    }


def extract_tables(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(html, flavor="lxml")
    except ValueError:
        return []


def candidate_case_tables(tables: list[pd.DataFrame]) -> list[pd.DataFrame]:
    out: list[pd.DataFrame] = []
    for frame in tables:
        flat = " ".join(normalize(x) for x in frame.astype(str).fillna("").to_numpy().ravel())
        if "病患姓名" in flat and "移植時間" in flat:
            out.append(frame)
    return out


def rows_from_html_tables(tables: list[pd.DataFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table_index, frame in enumerate(candidate_case_tables(tables), start=1):
        frame = frame.fillna("")
        for row_index, raw in frame.iterrows():
            values = [normalize(value) for value in raw.tolist()]
            joined = " | ".join(values)
            date_iso = roc_to_iso(joined)
            organ_match = ORGAN_RE.search(joined)
            if not date_iso or not organ_match:
                continue
            patient_name = values[1] if len(values) > 1 else ""
            table_label = "附表一" if table_index == 1 else "附表二"
            flags = evidence_flags(joined)
            rows.append(
                {
                    "case_id": f"{CASE_ID}-T{table_index:01d}-R{len(rows)+1:02d}",
                    "source_table": table_label,
                    "source_row_index": int(row_index) if isinstance(row_index, int) else str(row_index),
                    "patient_token": patient_token(patient_name) if patient_name else "",
                    "hospital": HOSPITAL_BY_TABLE.get(table_label, ""),
                    "organ": organ_match.group("organ"),
                    "transplant_date": date_iso,
                    "outcome_deceased": "歿" in joined or "死亡" in joined,
                    "outcome_text_redacted": "deceased" if ("歿" in joined or "死亡" in joined) else ("alive" if "存活" in joined else "not stated"),
                    **flags,
                }
            )
    return rows


def text_sections(html: str) -> dict[str, str]:
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    positions = {label: text.find(label) for label in ["附表一", "附表二", "附表三", "附表四"]}
    sections: dict[str, str] = {}
    for label in ["附表一", "附表二"]:
        start = positions[label]
        if start < 0:
            continue
        later = [pos for key, pos in positions.items() if pos > start and key != label]
        end = min(later) if later else len(text)
        sections[label] = text[start:end]
    return sections


def fallback_rows_from_text(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, section in text_sections(html).items():
        # Split on numbered patient rows while retaining each numbered block.
        chunks = re.split(r"(?=\n?\s*[1-9]\s+[^\n]{1,30}\s+(?:B型|C型|末期|腎臟|肝|不詳))", section)
        for chunk in chunks:
            date_iso = roc_to_iso(chunk)
            organ_match = ORGAN_RE.search(chunk)
            if not date_iso or not organ_match:
                continue
            compact = normalize(chunk)
            # The patient name sits immediately after the row number in the judgment table.
            name_match = re.search(r"(?:^|\s)([1-9])\s+([^\s]{2,6})\s+", compact)
            patient_name = name_match.group(2) if name_match else ""
            flags = evidence_flags(compact)
            rows.append(
                {
                    "case_id": f"{CASE_ID}-{label}-R{len(rows)+1:02d}",
                    "source_table": label,
                    "source_row_index": name_match.group(1) if name_match else "",
                    "patient_token": patient_token(patient_name) if patient_name else "",
                    "hospital": HOSPITAL_BY_TABLE.get(label, ""),
                    "organ": organ_match.group("organ"),
                    "transplant_date": date_iso,
                    "outcome_deceased": "歿" in compact or "死亡" in compact,
                    "outcome_text_redacted": "deceased" if ("歿" in compact or "死亡" in compact) else ("alive" if "存活" in compact else "not stated"),
                    **flags,
                }
            )
    return rows


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["hospital"], row["organ"], row["transplant_date"])
        if key in seen:
            continue
        seen.add(key)
        row = dict(row)
        row["case_id"] = f"{CASE_ID}-C{len(out)+1:02d}"
        out.append(row)
    return sorted(out, key=lambda item: item["transplant_date"])


def main() -> None:
    source_url, html, source_meta = fetch_source()
    tables = extract_tables(html)
    rows = rows_from_html_tables(tables)
    if len(rows) < 8:
        rows.extend(fallback_rows_from_text(html))
    rows = dedupe_rows(rows)

    fields = [
        "case_id", "source_table", "source_row_index", "patient_token", "hospital", "organ",
        "transplant_date", "outcome_deceased", "outcome_text_redacted",
        "has_bank_or_payment_records", "has_travel_records", "has_medical_records",
        "has_chat_or_notes", "has_witness_statement",
    ]
    with (OUT / "deidentified_case_ledger.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "generated_on": date.today().isoformat(),
        "case_number": "Taiwan Changhua District Court 113年度金訴字第657號",
        "selected_source_url": source_url,
        "source_integrity": source_meta,
        "html_table_count": len(tables),
        "candidate_case_table_count": len(candidate_case_tables(tables)),
        "deidentified_cases_extracted": len(rows),
        "hospital_counts": {},
        "organ_counts": {},
        "date_range": [min((r["transplant_date"] for r in rows), default=""), max((r["transplant_date"] for r in rows), default="")],
        "privacy_note": "Patient names and medical diagnoses are omitted; patient_token is a one-way case-local hash.",
        "interpretation_limit": "The judgment proves illegal brokerage and payments. It does not identify the organ donors or establish prisoner sourcing.",
    }
    for row in rows:
        summary["hospital_counts"][row["hospital"]] = summary["hospital_counts"].get(row["hospital"], 0) + 1
        summary["organ_counts"][row["organ"]] = summary["organ_counts"].get(row["organ"], 0) + 1
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
