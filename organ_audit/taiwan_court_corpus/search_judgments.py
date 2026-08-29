from __future__ import annotations

"""Search Taiwan's public judgment system for post-2015 transplant-brokerage cases.

Version 2 deliberately scores *local narrative windows* around transplant terms.
The earlier whole-document scoring could falsely combine, for example, a medical
history reference to transplantation with an unrelated fraud allegation, or
mistake "中國信託" (CTBC Bank) for a China geographic reference.

A candidate remains a research lead, never evidence of prisoner sourcing.

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
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://judgment.judicial.gov.tw"
SEARCH_URL = f"{BASE_URL}/FJUD/qryresult.aspx"
LIST_URL = f"{BASE_URL}/FJUD/qryresultlst.aspx"
DETAIL_URL = f"{BASE_URL}/FJUD/data.aspx"
OUT = Path("organ_audit/taiwan_court_corpus/output")
OUT.mkdir(parents=True, exist_ok=True)

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
    "人體器官移植條例 仲介",
    "人體器官移植條例 中國",
    "抗排斥藥物 境外移植",
    "赴中國 移植手術 仲介",
    "洗腎 仲介 大陸 移植",
    "青島大學附屬醫院 移植",
    "湘雅三醫院 移植",
    "天津第一中心醫院 移植",
    "中山大學附屬第一醫院 移植",
    "廣州醫科大學附屬第二醫院 移植",
]

# Known public case used only as a parser and strict-scoring control.
BASELINE_IDS = ["CHDM,113,金訴,657,20250724,1"]

MAX_PAGES_PER_QUERY = 25
MAX_UNIQUE_DETAILS = 700
REQUEST_DELAY_SECONDS = 0.35
WINDOW_BEFORE = 900
WINDOW_AFTER = 1700

ORGAN_ANCHOR_TERMS = {
    "器官移植", "肝臟移植", "肝移植", "腎臟移植", "腎移植", "心臟移植",
    "肺臟移植", "胰臟移植", "移植手術", "境外移植", "器官買賣",
}
CROSS_BORDER_TERMS = {
    "中國", "中國大陸", "大陸地區", "大陸", "境外", "赴陸", "赴中國",
    "青島", "長沙", "廣州", "天津", "武漢", "上海", "北京", "湘雅",
    "中山大學", "青島大學", "第一中心醫院",
}
BROKERAGE_TERMS = {
    "仲介", "居間", "介紹費", "仲介費", "報酬", "佣金", "對價", "招攬",
    "媒介", "器官買賣", "器官來源費", "代辦", "轉介病患", "介紹病患",
}
PAYMENT_TERMS = {
    "匯款", "轉帳", "交易明細", "帳戶", "地下匯兌", "現金", "人民幣",
    "新臺幣", "費用", "支付", "收款", "價金", "手術費", "醫療費",
}
TRAVEL_MEDICAL_TERMS = {
    "抗排斥藥", "抗排斥藥物", "免疫抑制", "入出境", "出入境", "搭機",
    "航班", "病歷", "就醫紀錄", "處方", "健保", "移植時間", "手術日期",
    "返臺", "赴陸", "赴中國",
}
PROVENANCE_CUSTODY_TERMS = {
    "死刑犯", "受刑人", "囚犯", "監獄", "拘留", "羈押", "法輪功", "維吾爾",
    "捐贈者", "供體", "器官捐贈", "COTRS", "器官分配", "器官來源",
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
    "organ": ORGAN_ANCHOR_TERMS,
    "cross_border": CROSS_BORDER_TERMS,
    "brokerage": BROKERAGE_TERMS,
    "payment": PAYMENT_TERMS,
    "travel_medical": TRAVEL_MEDICAL_TERMS,
    "provenance_custody": PROVENANCE_CUSTODY_TERMS,
    "evidence": EVIDENCE_TERMS,
    "hospital": HOSPITAL_TERMS,
}

# Taiwan entities that contain the literal characters 中國 but do not provide a
# geographic China anchor. Matching them as "China" caused obvious false leads.
NON_GEOGRAPHIC_CHINA_PREFIXES = {
    "中國信託", "中國人壽", "中國鋼鐵", "中國醫藥大學", "中國文化大學",
    "中國時報", "中國輸出入銀行", "中國石油", "中國國際商銀",
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
class WindowAssessment:
    start: int
    end: int
    score: int
    groups: dict[str, list[str]]
    qualifying: bool
    reason: str
    text: str


@dataclass
class Candidate:
    judgment_id: str
    title: str
    roc_date: str
    case_reason: str
    public_url: str
    strict_score: int
    local_group_count: int
    matched_groups_local: str
    matched_terms_local: str
    source_queries: str
    excerpt_redacted: str
    content_sha256: str


class JudgmentClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; public-interest-court-audit/2.0; +https://github.com/chriszavadil/PythonCode)",
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
        judgment_id = parse_qs(urlparse(href).query).get("id", [""])[0]
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


def valid_cross_border_occurrence(text: str, term: str, position: int) -> bool:
    if term != "中國":
        return True
    suffix = text[position : position + 12]
    return not any(suffix.startswith(prefix) for prefix in NON_GEOGRAPHIC_CHINA_PREFIXES)


def terms_in_text(text: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for group, terms in ALL_TERM_GROUPS.items():
        found: list[str] = []
        for term in sorted(terms, key=len, reverse=True):
            positions = [match.start() for match in re.finditer(re.escape(term), text)]
            if group == "cross_border":
                positions = [pos for pos in positions if valid_cross_border_occurrence(text, term, pos)]
            if positions:
                found.append(term)
        groups[group] = sorted(set(found))
    return groups


def local_score(groups: dict[str, list[str]]) -> int:
    score = 0
    if groups["organ"]:
        score += 4
    if groups["cross_border"]:
        score += 3
    if groups["hospital"]:
        score += 4
    if groups["brokerage"]:
        score += 4
    if groups["payment"]:
        score += 2
    if groups["travel_medical"]:
        score += 3
    if groups["evidence"]:
        score += 1
    if groups["provenance_custody"]:
        score += 1
    if groups["organ"] and groups["cross_border"] and groups["brokerage"]:
        score += 5
    if groups["organ"] and groups["payment"] and groups["travel_medical"]:
        score += 3
    if groups["organ"] and groups["hospital"]:
        score += 3
    return score


def strict_qualification(groups: dict[str, list[str]], score: int) -> tuple[bool, str]:
    if not groups["organ"]:
        return False, "no transplant anchor in local window"
    if not (groups["cross_border"] or groups["hospital"]):
        return False, "no China/cross-border or named-hospital anchor in local window"

    operational_path = bool(
        groups["brokerage"]
        or (groups["payment"] and groups["travel_medical"])
        or (groups["hospital"] and groups["payment"] and groups["evidence"])
    )
    if not operational_path:
        return False, "no local brokerage or payment-plus-travel/medical path"
    if score < 12:
        return False, "local score below strict threshold"
    return True, "strict local co-occurrence satisfied"


def assessment_windows(content: str) -> list[WindowAssessment]:
    compact = re.sub(r"\s+", " ", content).strip()
    anchor_positions: list[int] = []
    for term in ORGAN_ANCHOR_TERMS:
        anchor_positions.extend(match.start() for match in re.finditer(re.escape(term), compact))

    assessments: list[WindowAssessment] = []
    seen_ranges: set[tuple[int, int]] = set()
    for position in sorted(set(anchor_positions)):
        start = max(0, position - WINDOW_BEFORE)
        end = min(len(compact), position + WINDOW_AFTER)
        # Merge near-identical overlapping anchor windows deterministically.
        rounded = (start // 250 * 250, min(len(compact), ((end + 249) // 250) * 250))
        if rounded in seen_ranges:
            continue
        seen_ranges.add(rounded)
        text = compact[start:end]
        groups = terms_in_text(text)
        score = local_score(groups)
        qualifies, reason = strict_qualification(groups, score)
        assessments.append(
            WindowAssessment(
                start=start,
                end=end,
                score=score,
                groups=groups,
                qualifying=qualifies,
                reason=reason,
                text=text,
            )
        )
    return assessments


def best_window(content: str) -> WindowAssessment:
    windows = assessment_windows(content)
    if not windows:
        return WindowAssessment(
            start=0,
            end=min(len(content), 1200),
            score=0,
            groups={group: [] for group in ALL_TERM_GROUPS},
            qualifying=False,
            reason="no transplant anchor in judgment",
            text=re.sub(r"\s+", " ", content)[:1200],
        )
    return sorted(
        windows,
        key=lambda item: (
            not item.qualifying,
            -item.score,
            -sum(bool(values) for values in item.groups.values()),
            item.start,
        ),
    )[0]


def redact_excerpt(text: str, width: int = 650) -> str:
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


def flatten_terms(groups: dict[str, list[str]]) -> list[str]:
    return sorted({term for values in groups.values() for term in values})


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
        except Exception as exc:
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

    ids = sorted(by_id, key=lambda item: (-len(source_queries[item]), item))[:MAX_UNIQUE_DETAILS]

    candidates: list[Candidate] = []
    detail_index: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    local_group_counts: Counter[str] = Counter()

    for judgment_id in ids:
        seed = by_id[judgment_id]
        try:
            detail, raw = client.detail(judgment_id)
            content = detail["content"]
            window = best_window(content)
            global_groups = terms_in_text(content)
            for group, values in window.groups.items():
                if values:
                    local_group_counts[group] += 1

            local_terms = flatten_terms(window.groups)
            local_groups = [group for group, values in window.groups.items() if values]
            base_row = {
                "judgment_id": judgment_id,
                "title": detail["title"] or seed.title,
                "roc_date": detail["date"] or seed.roc_date,
                "case_reason": detail["case_reason"] or seed.case_reason,
                "strict_score": window.score,
                "strict_qualifying": window.qualifying,
                "qualification_reason": window.reason,
                "local_group_count": len(local_groups),
                "matched_groups_local": " | ".join(local_groups),
                "matched_terms_local": " | ".join(local_terms),
                "matched_groups_global": " | ".join(
                    group for group, values in global_groups.items() if values
                ),
                "source_query_count": len(source_queries[judgment_id]),
                "source_queries": " | ".join(sorted(source_queries[judgment_id])),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "public_url": public_url(judgment_id),
            }
            detail_index.append(base_row)

            if window.qualifying:
                candidates.append(
                    Candidate(
                        judgment_id=judgment_id,
                        title=detail["title"] or seed.title,
                        roc_date=detail["date"] or seed.roc_date,
                        case_reason=detail["case_reason"] or seed.case_reason,
                        public_url=public_url(judgment_id),
                        strict_score=window.score,
                        local_group_count=len(local_groups),
                        matched_groups_local=" | ".join(local_groups),
                        matched_terms_local=" | ".join(local_terms),
                        source_queries=" | ".join(sorted(source_queries[judgment_id])),
                        excerpt_redacted=redact_excerpt(window.text),
                        content_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                )
            else:
                exclusions.append(
                    {
                        **base_row,
                        "excerpt_redacted": redact_excerpt(window.text, width=360),
                    }
                )
        except Exception as exc:
            errors.append({"stage": "detail", "identifier": judgment_id, "error": repr(exc)})

    candidate_rows = [
        asdict(item)
        for item in sorted(
            candidates,
            key=lambda item: (-item.strict_score, item.roc_date, item.judgment_id),
        )
    ]
    detail_rows = sorted(
        detail_index,
        key=lambda item: (
            not bool(item["strict_qualifying"]),
            -int(item["strict_score"]),
            str(item["judgment_id"]),
        ),
    )
    exclusion_rows = sorted(
        exclusions,
        key=lambda item: (-int(item["strict_score"]), str(item["judgment_id"])),
    )

    write_csv(
        OUT / "query_statistics.csv",
        query_stats,
        ["query", "reported_total", "records_retrieved"],
    )
    write_csv(
        OUT / "detail_index.csv",
        detail_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "strict_score",
            "strict_qualifying", "qualification_reason", "local_group_count",
            "matched_groups_local", "matched_terms_local", "matched_groups_global",
            "source_query_count", "source_queries", "content_sha256", "public_url",
        ],
    )
    write_csv(
        OUT / "candidates.csv",
        candidate_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "public_url",
            "strict_score", "local_group_count", "matched_groups_local",
            "matched_terms_local", "source_queries", "excerpt_redacted",
            "content_sha256",
        ],
    )
    write_csv(
        OUT / "excluded_false_positives.csv",
        exclusion_rows,
        [
            "judgment_id", "title", "roc_date", "case_reason", "strict_score",
            "strict_qualifying", "qualification_reason", "local_group_count",
            "matched_groups_local", "matched_terms_local", "matched_groups_global",
            "source_query_count", "source_queries", "excerpt_redacted",
            "content_sha256", "public_url",
        ],
    )
    write_csv(OUT / "errors.csv", errors, ["stage", "identifier", "error"])

    baseline_qualified = BASELINE_IDS[0] in {row["judgment_id"] for row in candidate_rows}
    summary = {
        "generated_on": date.today().isoformat(),
        "method_version": 2,
        "queries_attempted": len(SEARCH_QUERIES),
        "queries_succeeded": len(query_stats),
        "unique_post_2015_results_before_cap": len(by_id),
        "details_attempted": len(ids),
        "details_retrieved": len(detail_rows),
        "strict_case_level_leads": len(candidate_rows),
        "excluded_after_local_cooccurrence_test": len(exclusion_rows),
        "errors": len(errors),
        "local_matched_group_document_counts": dict(local_group_counts),
        "baseline_ids": BASELINE_IDS,
        "baseline_qualified": baseline_qualified,
        "methodological_limit": (
            "A strict candidate is a public-record lead, not evidence of prisoner sourcing. "
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
        "## Version 2 correction",
        "",
        "The first scan scored terms anywhere in a full judgment and therefore overmatched unrelated cases. Version 2 requires the transplant, cross-border/hospital, and operational evidence to occur in the same local narrative window. It also excludes non-geographic uses such as 中國信託 from the China anchor.",
        "",
        "## Results",
        "",
        f"- Queries succeeded: {summary['queries_succeeded']} / {summary['queries_attempted']}",
        f"- Unique post-2015 judgments queued: {summary['unique_post_2015_results_before_cap']}",
        f"- Full judgment texts retrieved: {summary['details_retrieved']}",
        f"- Strict local-co-occurrence leads: {summary['strict_case_level_leads']}",
        f"- Excluded after local review: {summary['excluded_after_local_cooccurrence_test']}",
        f"- Retrieval or parsing errors: {summary['errors']}",
        f"- Known brokerage control qualified: {summary['baseline_qualified']}",
        "",
        "## Evidence rule",
        "",
        "A match establishes only that a public judgment contains a locally coherent transplant-brokerage or transplant-payment narrative. It does not establish donor identity or custody status. Short excerpts are role-redacted and party names or medical diagnoses are not intentionally exported.",
        "",
        "## Outputs",
        "",
        "- `candidates.csv`: strict local-co-occurrence leads",
        "- `excluded_false_positives.csv`: transparent rejection log",
        "- `detail_index.csv`: all fetched judgments and qualification reasons",
        "- `query_statistics.csv`: query-level retrieval counts",
        "- `errors.csv`: reproducibility log",
        "- `summary.json`: machine-readable summary",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")

    if BASELINE_IDS[0] not in {row["judgment_id"] for row in detail_rows}:
        raise RuntimeError("baseline judgment could not be retrieved")
    if not baseline_qualified:
        raise RuntimeError("strict scoring failed to retain the known brokerage control")


if __name__ == "__main__":
    main()
