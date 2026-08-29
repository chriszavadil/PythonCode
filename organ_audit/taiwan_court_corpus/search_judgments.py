from __future__ import annotations

"""Search Taiwan's public judgment system for post-2015 transplant-brokerage cases.

The goal is to discover additional case-level records with independently useful
anchors (court number, operation date, hospital, payment, travel or follow-up
records). A match is a research lead, never proof of prisoner sourcing.

HTML parsing patterns are adapted from the MIT-licensed public project
asgard-ai-platform/mcp-tw-judgment:
https://github.com/asgard-ai-platform/mcp-tw-judgment
"""

import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://judgment.judicial.gov.tw"
SEARCH_URL = f"{BASE_URL}/FJUD/qryresult.aspx"
LIST_URL = f"{BASE_URL}/FJUD/qryresultlst.aspx"
DETAIL_URL = f"{BASE_URL}/FJUD/data.aspx"
OUT = Path("organ_audit/taiwan_court_corpus/output")
OUT.mkdir(parents=True, exist_ok=True)

# Narrow, overlapping queries are intentional. They make the search reproducible
# while limiting site load and false positives from generic medical disputes.
SEARCH_QUERIES = [
    "器官移植 仲介 中國",
    "器官移植 仲介 大陸",
    "器官移植 匯款 中國",
    "器官移植 抗排斥藥 入出境",
    "器官移植 抗排斥藥 匯款",
    "境外器官移植 仲介",
    "腎臟移植 仲介 中國",
    "肝臟移植 仲介 中國",
    "器官買賣 中國 移植",
    "青島大學附屬醫院 移植",
    "湘雅三醫院 移植",
    "天津第一中心醫院 移植",
    "中山大學附屬第一醫院 移植",
    "廣州醫科大學附屬第二醫院 移植",
]

# Known public case used only as a parser and scoring control.
BASELINE_IDS = ["CHDM,113,金訴,657,20250724,1"]

MAX_PAGES_PER_QUERY = 25
MAX_UNIQUE_DETAILS = 600
REQUEST_DELAY_SECONDS = 0.35

ORGAN_TERMS = {
    "器官移植", "肝臟移植", "肝移植", "腎臟移植", "腎移植", "心臟移植",
    "肺臟移植", "胰臟移植", "移植手術", "器官來源",
}
CROSS_BORDER_TERMS = {
    "中國", "大陸", "境外", "赴陸", "青島", "長沙", "廣州", "天津", "武漢",
    "上海", "北京", "湘雅", "中山大學", "青島大學", "第一中心醫院",
}
BROKERAGE_TERMS = {
    "仲介", "居間", "介紹費", "仲介費", "報酬", "佣金", "對價", "招攬",
    "媒介", "器官買賣", "器官來源費", "代辦",
}
PAYMENT_TERMS = {
    "匯款", "轉帳", "交易明細", "帳戶", "地下匯兌", "現金", "人民幣",
    "新臺幣", "費用", "支付", "收款", "價金",
}
TRAVEL_MEDICAL_TERMS = {
    "抗排斥藥", "免疫抑制", "入出境", "出入境", "搭機", "航班", "病歷",
    "就醫紀錄", "處方", "健保", "移植時間", "手術日期",
}
PROVENANCE_CUSTODY_TERMS = {
    "死刑犯", "受刑人", "囚犯", "監獄", "拘留", "羈押", "法輪功", "維吾爾",
    "捐贈者", "供體", "器官捐贈", "COTRS", "器官分配",
}
EVIDENCE_TERMS = {
    "對話紀錄", "微信", "LINE", "通訊軟體", "證人", "供述", "扣案", "搜索",
    "帳冊", "入出境資料", "醫療紀錄", "抗排斥藥物", "銀行", "匯兌",
}
HOSPITAL_TERMS = {
    "青島大學附屬醫院", "中南大學湘雅三醫院", "湘雅三醫院",
    "天津市第一中心醫院", "天津第一中心醫院", "中山大學附屬第一醫院",
    "廣州醫科大學附屬第二醫院", "武漢協和醫院", "武漢同濟醫院",
}

ALL_TERM_GROUPS = {
    "organ": ORGAN_TERMS,
    "cross_border": CROSS_BORDER_TERMS,
    "brokerage": BROKERAGE_TERMS,
    "payment": PAYMENT_TERMS,
    "travel_medical": TRAVEL_MEDICAL_TERMS,
    "provenance_custody": PROVENANCE_CUSTODY_TERMS,
    "evidence": EVIDENCE_TERMS,
    "hospital": HOSPITAL_TERMS,
}


@dataclass
class SearchResult:
    judgment_id: str
    title: str
    roc_date: str
    case_reason: str
    url: str
    summary: str
    query: str


@dataclass
class Candidate:
    judgment_id: str
    title: str
    roc_date: str
    case_reason: str
    public_url: str
    score: int
    matched_groups: str
    matched_terms: str
    source_queries: str
    excerpt_redacted: str
    content_sha256: str


class JudgmentClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; public-interest-court-audit/1.0; +https://github.com/chriszavadil/PythonCode)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            }
        )

    def get(self, url: str, *, params: dict[str, object]) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(url, params=params, timeout=60)
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding
                time.sleep(REQUEST_DELAY_SECONDS)
                return response
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
        raise RuntimeError(f"request failed: {url} {params!r}: {last_error!r}")

    def search(self, keyword: str) -> tuple[int | None, list[SearchResult]]:
        response = self.get(SEARCH_URL, params={"akw": keyword})
        html = response.text
        qid_match = re.search(r'name="hidQID"[^>]+value="([a-f0-9]+)"', html)
        if not qid_match:
            raise RuntimeError(f"hidQID not found for query {keyword!r}")
        qid = qid_match.group(1)
        count_match = re.search(r'<span class="badge">(\d+)</span>', html)
        total = int(count_match.group(1)) if count_match else None

        records: list[SearchResult] = []
        for page in range(1, MAX_PAGES_PER_QUERY + 1):
            list_response = self.get(
                LIST_URL,
                params={"ty": "JUDBOOK", "q": qid, "page": page},
            )
            page_records = parse_result_list(list_response.text, keyword)
            if not page_records:
                break
            records.extend(page_records)
            if len(page_records) < 20:
                break
            if total is not None and len(records) >= total:
                break
        return total, records

    def detail(self, judgment_id: str) -> tuple[dict[str, str], bytes]:
        response = self.get(
            DETAIL_URL,
            params={"ty": "JD", "id": judgment_id, "ot": "in"},
        )
        return parse_detail(response.text), response.content


def parse_result_list(html: str, query: str) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.jub-table")
    if not table:
        return []
    results: list[SearchResult] = []
    for row in table.find_all("tr"):
        if "summary" in (row.get("class") or []):
            if results:
                results[-1].summary = row.get_text(" ", strip=True)
            continue
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[1].find("a", href=True)
        href = link.get("href", "") if link else ""
        parsed = parse_qs(urlparse(href).query)
        judgment_id = parsed.get("id", [""])[0]
        if not judgment_id:
            continue
        title = re.sub(r"\s*（\d+K）", "", cells[1].get_text(" ", strip=True))
        results.append(
            SearchResult(
                judgment_id=judgment_id,
                title=title,
                roc_date=cells[2].get_text(" ", strip=True),
                case_reason=cells[3].get_text(" ", strip=True),
                url=href,
                summary="",
                query=query,
            )
        )
    return results


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


def post_2015(judgment_id: str, roc_date: str) -> bool:
    parts = judgment_id.split(",")
    if len(parts) >= 5 and re.fullmatch(r"\d{8}", parts[4]):
        return parts[4] >= "20150101"
    match = re.search(r"(\d{2,3})[.年/-]", roc_date)
    return bool(match and int(match.group(1)) >= 104)


def matched_terms(content: str) -> dict[str, list[str]]:
    return {
        group: sorted(term for term in terms if term in content)
        for group, terms in ALL_TERM_GROUPS.items()
    }


def score_candidate(groups: dict[str, list[str]]) -> int:
    score = 0
    if groups["organ"]:
        score += 3
    if groups["cross_border"]:
        score += 2
    if groups["brokerage"]:
        score += 3
    if groups["payment"]:
        score += 2
    if groups["travel_medical"]:
        score += 2
    if groups["evidence"]:
        score += 2
    if groups["hospital"]:
        score += 2
    if groups["provenance_custody"]:
        score += 1  # lead only; never treated as proof
    if groups["organ"] and groups["cross_border"] and groups["brokerage"]:
        score += 3
    if groups["payment"] and groups["travel_medical"]:
        score += 2
    return score


def qualifying(groups: dict[str, list[str]], score: int) -> bool:
    # Require organ context plus a cross-border or named-hospital anchor and at
    # least one independently useful operational/evidentiary category.
    anchor = bool(groups["cross_border"] or groups["hospital"])
    operational = bool(
        groups["brokerage"]
        or groups["payment"]
        or groups["travel_medical"]
        or groups["evidence"]
    )
    return bool(groups["organ"] and anchor and operational and score >= 7)


def redact_excerpt(text: str, terms: Iterable[str], width: int = 440) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    positions = [compact.find(term) for term in terms if compact.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - width // 3)
    excerpt = compact[start : start + width]

    # Conservative masking of common role-labelled names and direct identifiers.
    excerpt = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        excerpt,
    )
    excerpt = re.sub(r"[A-Z]\d{8,10}", "[ID REDACTED]", excerpt)
    excerpt = re.sub(r"\b09\d{8}\b", "[PHONE REDACTED]", excerpt)
    return excerpt


def public_url(judgment_id: str) -> str:
    from urllib.parse import quote

    return f"{DETAIL_URL}?ty=JD&id={quote(judgment_id, safe=',')}&ot=in"


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    client = JudgmentClient()
    query_stats: list[dict[str, object]] = []
    by_id: dict[str, SearchResult] = {}
    source_queries: dict[str, set[str]] = defaultdict(set)
    errors: list[dict[str, str]] = []

    for query in SEARCH_QUERIES:
        try:
            total, records = client.search(query)
            query_stats.append(
                {
                    "query": query,
                    "reported_total": total if total is not None else "",
                    "records_retrieved": len(records),
                }
            )
            for record in records:
                if not post_2015(record.judgment_id, record.roc_date):
                    continue
                by_id.setdefault(record.judgment_id, record)
                source_queries[record.judgment_id].add(query)
        except Exception as exc:  # keep remaining queries reproducible
            errors.append({"stage": "search", "identifier": query, "error": repr(exc)})

    for baseline_id in BASELINE_IDS:
        by_id.setdefault(
            baseline_id,
            SearchResult(
                judgment_id=baseline_id,
                title="known parser control",
                roc_date="",
                case_reason="",
                url="",
                summary="",
                query="baseline",
            ),
        )
        source_queries[baseline_id].add("baseline control")

    # Deterministic cap: prioritize records found by more independent queries.
    ids = sorted(
        by_id,
        key=lambda item: (-len(source_queries[item]), item),
    )[:MAX_UNIQUE_DETAILS]

    candidates: list[Candidate] = []
    detail_index: list[dict[str, object]] = []
    group_counts: Counter[str] = Counter()

    for judgment_id in ids:
        seed = by_id[judgment_id]
        try:
            detail, raw = client.detail(judgment_id)
            content = detail["content"]
            groups = matched_terms(content)
            score = score_candidate(groups)
            for group, values in groups.items():
                if values:
                    group_counts[group] += 1

            flat_terms = sorted({term for values in groups.values() for term in values})
            detail_index.append(
                {
                    "judgment_id": judgment_id,
                    "title": detail["title"] or seed.title,
                    "roc_date": detail["date"] or seed.roc_date,
                    "case_reason": detail["case_reason"] or seed.case_reason,
                    "score": score,
                    "source_query_count": len(source_queries[judgment_id]),
                    "source_queries": " | ".join(sorted(source_queries[judgment_id])),
                    "matched_groups": " | ".join(group for group, values in groups.items() if values),
                    "matched_terms": " | ".join(flat_terms),
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                    "public_url": public_url(judgment_id),
                }
            )

            if qualifying(groups, score):
                candidates.append(
                    Candidate(
                        judgment_id=judgment_id,
                        title=detail["title"] or seed.title,
                        roc_date=detail["date"] or seed.roc_date,
                        case_reason=detail["case_reason"] or seed.case_reason,
                        public_url=public_url(judgment_id),
                        score=score,
                        matched_groups=" | ".join(group for group, values in groups.items() if values),
                        matched_terms=" | ".join(flat_terms),
                        source_queries=" | ".join(sorted(source_queries[judgment_id])),
                        excerpt_redacted=redact_excerpt(content, flat_terms),
                        content_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                )
        except Exception as exc:
            errors.append({"stage": "detail", "identifier": judgment_id, "error": repr(exc)})

    candidate_rows = [asdict(item) for item in sorted(candidates, key=lambda item: (-item.score, item.roc_date, item.judgment_id))]
    detail_rows = sorted(detail_index, key=lambda item: (-int(item["score"]), str(item["judgment_id"])))

    write_csv(
        OUT / "query_statistics.csv",
        query_stats,
        ["query", "reported_total", "records_retrieved"],
    )
    write_csv(
        OUT / "detail_index.csv",
        detail_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "score",
            "source_query_count", "source_queries", "matched_groups",
            "matched_terms", "content_sha256", "public_url",
        ],
    )
    write_csv(
        OUT / "candidates.csv",
        candidate_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "public_url",
            "score", "matched_groups", "matched_terms", "source_queries",
            "excerpt_redacted", "content_sha256",
        ],
    )
    write_csv(OUT / "errors.csv", errors, ["stage", "identifier", "error"])

    summary = {
        "generated_on": date.today().isoformat(),
        "queries_attempted": len(SEARCH_QUERIES),
        "queries_succeeded": len(query_stats),
        "unique_post_2015_results_before_cap": len(by_id),
        "details_attempted": len(ids),
        "details_retrieved": len(detail_rows),
        "qualifying_case_level_leads": len(candidate_rows),
        "errors": len(errors),
        "matched_group_document_counts": dict(group_counts),
        "baseline_ids": BASELINE_IDS,
        "methodological_limit": (
            "A candidate is a public-record lead, not evidence of prisoner sourcing. "
            "A positive prisoner-sourcing finding requires an independently corroborated "
            "custody-to-testing-to-death/procurement-to-recipient chain."
        ),
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_lines = [
        "# Taiwan court-corpus scan: post-2015 transplant brokerage",
        "",
        f"Generated: {summary['generated_on']}",
        "",
        "## Scope",
        "",
        "The script searched Taiwan's public judgment system with overlapping, narrowly defined terms for cross-border transplant brokerage, payments, travel, anti-rejection medication, named Chinese hospitals, and donor-provenance language.",
        "",
        "## Results",
        "",
        f"- Queries succeeded: {summary['queries_succeeded']} / {summary['queries_attempted']}",
        f"- Unique post-2015 judgments queued: {summary['unique_post_2015_results_before_cap']}",
        f"- Full judgment texts retrieved: {summary['details_retrieved']}",
        f"- Mechanically qualifying case-level leads: {summary['qualifying_case_level_leads']}",
        f"- Retrieval or parsing errors: {summary['errors']}",
        "",
        "## Evidence rule",
        "",
        "A match establishes only that a public judgment contains useful operational anchors. It does not establish the identity or custody status of an organ donor. The candidates file deliberately uses short, role-redacted excerpts and omits party names and medical diagnoses.",
        "",
        "## Outputs",
        "",
        "- `query_statistics.csv`: query-level result counts",
        "- `detail_index.csv`: metadata and matched-term index for all fetched judgments",
        "- `candidates.csv`: higher-scoring leads for manual authentication",
        "- `errors.csv`: reproducibility log",
        "- `summary.json`: machine-readable summary",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")

    # The known public judgment must be retrievable; otherwise a green workflow
    # would provide false confidence about the corpus connection.
    if BASELINE_IDS[0] not in {row["judgment_id"] for row in detail_rows}:
        raise RuntimeError("baseline judgment could not be retrieved")


if __name__ == "__main__":
    main()
