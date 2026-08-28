from __future__ import annotations

import csv
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path("organ_audit/iorg_audit")
QUERY = '"IORG0003571"'
SEARCH_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

TRANSPLANT_RE = re.compile(
    r"transplant|donor|organ procurement|heart graft|liver graft|kidney graft",
    re.IGNORECASE,
)
APPROVAL_RE = re.compile(
    r"(?:approval|approved|protocol|ethics|ethical|IRB).{0,180}IORG0003571|"
    r"IORG0003571.{0,180}(?:approval|approved|protocol|ethics|ethical|IRB)",
    re.IGNORECASE | re.DOTALL,
)
DATE_RE = re.compile(
    r"(?:dated|date|on)\s+(?:the\s+)?(?:\d{1,2}[\s/-])?"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December|\d{1,2})"
    r"[\s/-]+(?:\d{1,2}[\s,/-]+)?(?:19|20)\d{2}",
    re.IGNORECASE,
)


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "independent-research-audit/1.0 (public literature audit)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_text(url: str) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "independent-research-audit/1.0 (public literature audit)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except Exception:
        return None
    return raw.decode("utf-8", errors="replace")


def clean_xml_text(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
        text = " ".join(root.itertext())
    except ET.ParseError:
        text = re.sub(r"<[^>]+>", " ", xml_text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def contexts(text: str, term: str = "IORG0003571", radius: int = 350) -> list[str]:
    output: list[str] = []
    lowered = text.lower()
    needle = term.lower()
    start = 0
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            break
        snippet = text[max(0, index - radius) : min(len(text), index + len(term) + radius)]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if snippet not in output:
            output.append(snippet)
        start = index + len(needle)
    return output[:8]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    params = urllib.parse.urlencode(
        {
            "query": QUERY,
            "format": "json",
            "pageSize": 1000,
            "resultType": "core",
        }
    )
    payload = fetch_json(f"{SEARCH_API}?{params}")
    hit_count = int(payload.get("hitCount", 0))
    results = payload.get("resultList", {}).get("result", [])

    rows: list[dict[str, object]] = []
    for result in results:
        pmcid = result.get("pmcid", "") or ""
        full_text = ""
        if pmcid:
            xml_text = fetch_text(FULLTEXT_API.format(pmcid=pmcid))
            if xml_text:
                full_text = clean_xml_text(xml_text)
        fallback = " ".join(
            str(result.get(field, "") or "")
            for field in ("title", "abstractText", "authorString", "journalTitle")
        )
        searchable = full_text or fallback
        snippets = contexts(searchable)
        combined_context = " || ".join(snippets)
        rows.append(
            {
                "pmcid": pmcid,
                "pmid": result.get("pmid", "") or "",
                "doi": result.get("doi", "") or "",
                "title": result.get("title", "") or "",
                "journal": result.get("journalTitle", "") or "",
                "publication_year": result.get("pubYear", "") or "",
                "publication_date": result.get("firstPublicationDate", "") or "",
                "author_string": result.get("authorString", "") or "",
                "is_retracted": bool(result.get("isRetracted", False)),
                "is_open_access": bool(result.get("isOpenAccess", False)),
                "full_text_recovered": bool(full_text),
                "iorg_occurrences": searchable.lower().count("iorg0003571"),
                "uses_as_approval_or_protocol": bool(APPROVAL_RE.search(combined_context)),
                "contains_transplant_terms_nearby": bool(TRANSPLANT_RE.search(combined_context)),
                "contains_date_nearby": bool(DATE_RE.search(combined_context)),
                "contexts": combined_context,
            }
        )
        time.sleep(0.05)

    fieldnames = list(rows[0].keys()) if rows else ["pmcid", "pmid", "doi", "title"]
    with (OUT / "europe_pmc_iorg0003571_results.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "query": QUERY,
        "europe_pmc_hit_count": hit_count,
        "results_returned": len(rows),
        "full_text_recovered": sum(bool(row["full_text_recovered"]) for row in rows),
        "uses_as_approval_or_protocol": sum(
            bool(row["uses_as_approval_or_protocol"]) for row in rows
        ),
        "transplant_related_context": sum(
            bool(row["contains_transplant_terms_nearby"]) for row in rows
        ),
        "date_attached_to_identifier": sum(
            bool(row["contains_date_nearby"]) for row in rows
        ),
        "retracted_records": sum(bool(row["is_retracted"]) for row in rows),
        "years": {},
        "limitations": [
            "Europe PMC coverage is incomplete; this is a lower-bound audit, not a census of every publication using the identifier.",
            "Automated context classification identifies reporting patterns, not whether a separate study-specific approval document actually existed.",
            "An IORG is an institutional/organizational IRB-registration identifier; its appearance does not itself prove that research lacked ethical review.",
        ],
    }
    for row in rows:
        year = str(row["publication_year"] or "unknown")
        summary["years"][year] = summary["years"].get(year, 0) + 1
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
