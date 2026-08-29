from __future__ import annotations

"""Refine court-corpus candidates using local co-occurrence, not document-wide terms.

Long criminal judgments can contain unrelated allegations, medical histories and
financial evidence. This pass requires transplant, cross-border, brokerage and
operational evidence to occur within the same bounded text window before a case
is retained as a lead.
"""

import csv
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://judgment.judicial.gov.tw"
DETAIL_URL = f"{BASE_URL}/FJUD/data.aspx"
ROOT = Path("organ_audit/taiwan_court_corpus/output")
INPUT = ROOT / "detail_index.csv"
OUT = ROOT / "proximity"
OUT.mkdir(parents=True, exist_ok=True)

WINDOW_SIZE = 3000
WINDOW_STEP = 750
REQUEST_DELAY_SECONDS = 0.3
BASELINE_ID = "CHDM,113,金訴,657,20250724,1"

ORGAN_TERMS = {
    "器官移植", "肝臟移植", "肝移植", "腎臟移植", "腎移植", "心臟移植",
    "肺臟移植", "胰臟移植", "移植手術", "器官來源",
}
CROSS_BORDER_TERMS = {
    "中國", "大陸", "境外", "赴陸", "青島", "長沙", "廣州", "天津", "武漢",
    "上海", "北京", "湘雅", "中山大學", "青島大學", "第一中心醫院",
}
BROKERAGE_TERMS = {
    "仲介", "仲介費", "居間", "介紹費", "佣金", "招攬", "媒介", "器官買賣",
    "器官來源費", "代辦",
}
PAYMENT_TERMS = {
    "匯款", "轉帳", "交易明細", "帳戶", "地下匯兌", "現金", "人民幣",
    "新臺幣", "收款", "價金", "支付",
}
TRAVEL_MEDICAL_TERMS = {
    "抗排斥藥", "抗排斥藥物", "免疫抑制", "入出境", "出入境", "搭機", "航班",
    "移植時間", "手術日期", "病歷", "醫療紀錄",
}
EVIDENCE_TERMS = {
    "對話紀錄", "微信", "LINE", "通訊軟體", "帳冊", "扣案", "搜索", "證人",
    "供述", "入出境資料", "銀行交易", "匯兌",
}
HOSPITAL_TERMS = {
    "青島大學附屬醫院", "中南大學湘雅三醫院", "湘雅三醫院",
    "天津市第一中心醫院", "天津第一中心醫院", "中山大學附屬第一醫院",
    "廣州醫科大學附屬第二醫院", "武漢協和醫院", "武漢同濟醫院",
}
CUSTODY_TERMS = {
    "死刑犯", "受刑人", "囚犯", "監獄", "拘留", "羈押", "法輪功", "維吾爾",
    "供體", "捐贈者", "器官捐贈", "COTRS", "器官分配",
}

GROUPS = {
    "organ": ORGAN_TERMS,
    "cross_border": CROSS_BORDER_TERMS,
    "brokerage": BROKERAGE_TERMS,
    "payment": PAYMENT_TERMS,
    "travel_medical": TRAVEL_MEDICAL_TERMS,
    "evidence": EVIDENCE_TERMS,
    "hospital": HOSPITAL_TERMS,
    "custody": CUSTODY_TERMS,
}


@dataclass
class RefinedLead:
    judgment_id: str
    title: str
    roc_date: str
    case_reason: str
    public_url: str
    proximity_score: int
    matched_groups: str
    matched_terms: str
    best_window_redacted: str
    content_sha256: str


class Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; public-interest-court-audit/1.1; +https://github.com/chriszavadil/PythonCode)",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            }
        )

    def fetch(self, judgment_id: str) -> tuple[dict[str, str], bytes]:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(
                    DETAIL_URL,
                    params={"ty": "JD", "id": judgment_id, "ot": "in"},
                    timeout=60,
                )
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding
                time.sleep(REQUEST_DELAY_SECONDS)
                return parse_detail(response.text), response.content
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
        raise RuntimeError(f"failed to fetch {judgment_id}: {last_error!r}")


def parse_detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.select_one("div#jud")
    values = meta.select("div.col-td") if meta else []
    body = soup.select_one("div.htmlcontent")
    return {
        "title": values[0].get_text(" ", strip=True) if len(values) > 0 else "",
        "date": values[1].get_text(" ", strip=True) if len(values) > 1 else "",
        "case_reason": values[2].get_text(" ", strip=True) if len(values) > 2 else "",
        "content": body.get_text("\n", strip=True) if body else "",
    }


def windows(text: str) -> Iterable[str]:
    compact = re.sub(r"[\t\u3000 ]+", " ", text)
    compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
    if len(compact) <= WINDOW_SIZE:
        yield compact
        return
    for start in range(0, len(compact), WINDOW_STEP):
        window = compact[start : start + WINDOW_SIZE]
        if not window:
            break
        yield window
        if start + WINDOW_SIZE >= len(compact):
            break


def matches(text: str) -> dict[str, list[str]]:
    return {
        group: sorted(term for term in terms if term in text)
        for group, terms in GROUPS.items()
    }


def proximity_score(groups: dict[str, list[str]]) -> int:
    score = 0
    score += 4 if groups["organ"] else 0
    score += 3 if groups["cross_border"] else 0
    score += 5 if groups["brokerage"] else 0
    score += 3 if groups["payment"] else 0
    score += 3 if groups["travel_medical"] else 0
    score += 2 if groups["evidence"] else 0
    score += 3 if groups["hospital"] else 0
    score += 1 if groups["custody"] else 0
    if groups["brokerage"] and groups["payment"]:
        score += 2
    if groups["travel_medical"] and groups["evidence"]:
        score += 1
    if groups["hospital"] and groups["travel_medical"]:
        score += 2
    return score


def qualifies(groups: dict[str, list[str]], score: int) -> bool:
    operational_categories = sum(
        bool(groups[name])
        for name in ("payment", "travel_medical", "evidence", "hospital")
    )
    return bool(
        groups["organ"]
        and groups["cross_border"]
        and groups["brokerage"]
        and operational_categories >= 2
        and score >= 17
    )


def redact(text: str, width: int = 1300) -> str:
    excerpt = re.sub(r"\s+", " ", text).strip()[:width]
    excerpt = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        excerpt,
    )
    excerpt = re.sub(r"[A-Z]\d{8,10}", "[ID REDACTED]", excerpt)
    excerpt = re.sub(r"\b09\d{8}\b", "[PHONE REDACTED]", excerpt)
    return excerpt


def public_url(judgment_id: str) -> str:
    return f"{DETAIL_URL}?ty=JD&id={quote(judgment_id, safe=',')}&ot=in"


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if not INPUT.exists():
        raise FileNotFoundError(INPUT)

    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        input_rows = list(csv.DictReader(handle))

    client = Client()
    leads: list[RefinedLead] = []
    audit_rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []

    for input_row in input_rows:
        judgment_id = input_row["judgment_id"]
        try:
            detail, raw = client.fetch(judgment_id)
            best_score = -1
            best_groups: dict[str, list[str]] = {name: [] for name in GROUPS}
            best_window = ""
            best_qualifies = False

            for window in windows(detail["content"]):
                groups = matches(window)
                score = proximity_score(groups)
                is_qualified = qualifies(groups, score)
                rank = (1 if is_qualified else 0, score)
                best_rank = (1 if best_qualifies else 0, best_score)
                if rank > best_rank:
                    best_score = score
                    best_groups = groups
                    best_window = window
                    best_qualifies = is_qualified

            flat_terms = sorted({term for values in best_groups.values() for term in values})
            audit_rows.append(
                {
                    "judgment_id": judgment_id,
                    "title": detail["title"] or input_row.get("title", ""),
                    "roc_date": detail["date"] or input_row.get("roc_date", ""),
                    "case_reason": detail["case_reason"] or input_row.get("case_reason", ""),
                    "qualified": best_qualifies,
                    "proximity_score": best_score,
                    "matched_groups": " | ".join(group for group, values in best_groups.items() if values),
                    "matched_terms": " | ".join(flat_terms),
                    "best_window_redacted": redact(best_window),
                    "public_url": public_url(judgment_id),
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )

            if best_qualifies:
                leads.append(
                    RefinedLead(
                        judgment_id=judgment_id,
                        title=detail["title"] or input_row.get("title", ""),
                        roc_date=detail["date"] or input_row.get("roc_date", ""),
                        case_reason=detail["case_reason"] or input_row.get("case_reason", ""),
                        public_url=public_url(judgment_id),
                        proximity_score=best_score,
                        matched_groups=" | ".join(group for group, values in best_groups.items() if values),
                        matched_terms=" | ".join(flat_terms),
                        best_window_redacted=redact(best_window),
                        content_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                )
        except Exception as exc:
            errors.append({"judgment_id": judgment_id, "error": repr(exc)})

    lead_rows = [asdict(lead) for lead in sorted(leads, key=lambda item: (-item.proximity_score, item.roc_date, item.judgment_id))]
    audit_rows.sort(key=lambda row: (not bool(row["qualified"]), -int(row["proximity_score"]), str(row["judgment_id"])))

    write_csv(
        OUT / "refined_candidates.csv",
        lead_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "public_url",
            "proximity_score", "matched_groups", "matched_terms",
            "best_window_redacted", "content_sha256",
        ],
    )
    write_csv(
        OUT / "refinement_audit.csv",
        audit_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "qualified",
            "proximity_score", "matched_groups", "matched_terms",
            "best_window_redacted", "public_url", "content_sha256",
        ],
    )
    write_csv(OUT / "errors.csv", errors, ["judgment_id", "error"])

    summary = {
        "generated_on": date.today().isoformat(),
        "input_judgments": len(input_rows),
        "details_retrieved": len(audit_rows),
        "document_wide_candidates_before_refinement": sum(
            1 for row in input_rows if int(row.get("score") or 0) >= 7
        ),
        "proximity_validated_leads": len(lead_rows),
        "novel_leads_excluding_known_baseline": sum(
            1 for row in lead_rows if row["judgment_id"] != BASELINE_ID
        ),
        "errors": len(errors),
        "window_size_chars": WINDOW_SIZE,
        "window_step_chars": WINDOW_STEP,
        "rule": (
            "Within one local window: organ + cross-border + brokerage, plus at least "
            "two of payment/travel-medical/evidence/named-hospital and score >=17."
        ),
        "evidence_limit": (
            "A validated lead identifies a public court record about a transplant-related "
            "cross-border brokerage pathway. It does not identify the organ donor or prove "
            "prisoner sourcing."
        ),
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = [
        "# Proximity-refined Taiwan transplant brokerage leads",
        "",
        f"Generated: {summary['generated_on']}",
        "",
        "Document-wide term scoring produced false positives because long judgments combine unrelated medical, financial and criminal facts. This second pass keeps a record only when the required concepts occur inside the same 3,000-character sliding window.",
        "",
        f"- Input judgments: {summary['input_judgments']}",
        f"- Details retrieved: {summary['details_retrieved']}",
        f"- Proximity-validated leads: {summary['proximity_validated_leads']}",
        f"- Potentially novel leads excluding the known Changhua case: {summary['novel_leads_excluding_known_baseline']}",
        f"- Errors: {summary['errors']}",
        "",
        "A retained lead is not evidence of prisoner sourcing. Manual authentication must determine whether it concerns actual transplant brokerage, then obtain donor-provenance records for the same recipient operation.",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    if BASELINE_ID not in {row["judgment_id"] for row in lead_rows}:
        raise RuntimeError("proximity rule rejected the known baseline case")


if __name__ == "__main__":
    main()
