import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

JOURNAL = "Radiology"
YEAR = 2026
MONTH = 8
RETMAX = 200
OUT = Path("data/latest.json")

# 这些 PubMed 文章类型通常不是原创研究，先排除。
NON_ORIGINAL_TYPES = {
    "Editorial",
    "Comment",
    "Letter",
    "Review",
    "Systematic Review",
    "Meta-Analysis",
    "Case Reports",
    "Guideline",
    "Practice Guideline",
    "Consensus Development Conference",
    "Consensus Development Conference, NIH",
    "News",
    "Published Erratum",
    "Corrected and Republished Article",
    "Retracted Publication",
}


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get_xml(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return ET.fromstring(r.read())


def text(el, path, default=""):
    node = el.find(path)
    if node is None:
        return default
    value = "".join(node.itertext()).strip()
    return value or default


def publication_date(record):
    article_date = record.find("MedlineCitation/Article/ArticleDate")
    if article_date is not None:
        y = text(article_date, "Year")
        m = text(article_date, "Month")
        d = text(article_date, "Day")
        if y and m and d:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    pub_date = record.find("MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pub_date is not None:
        y = text(pub_date, "Year")
        m = text(pub_date, "Month")
        d = text(pub_date, "Day")
        if y:
            parts = [y]
            if m:
                parts.append(m)
            if d:
                parts.append(d)
            return "-".join(parts)
        medline_date = text(pub_date, "MedlineDate")
        if medline_date:
            return medline_date
    return ""


start_date = f"{YEAR}/{MONTH:02d}/01"
end_date = f"{YEAR}/{MONTH:02d}/31"
term = f'"{JOURNAL}"[jour] AND {start_date}:{end_date}[dp]'

search_url = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
    + urllib.parse.urlencode({
        "db": "pubmed",
        "term": term,
        "retmax": RETMAX,
        "sort": "pub date",
        "retmode": "json",
    })
)
search = get_json(search_url)
pmids = search.get("esearchresult", {}).get("idlist", [])

if not pmids:
    raise SystemExit(f"No PubMed records found for {JOURNAL}, {YEAR}-{MONTH:02d}.")

fetch_url = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
    + urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    })
)
root = get_xml(fetch_url)

papers = []
excluded = []

for record in root.findall(".//PubmedArticle"):
    citation = record.find("MedlineCitation")
    art = citation.find("Article") if citation is not None else None
    if art is None:
        continue

    title = text(art, "ArticleTitle", "Untitled")
    journal = text(art, "Journal/Title", JOURNAL)
    pmid = text(citation, "PMID")
    date = publication_date(record)

    pub_types = [
        "".join(node.itertext()).strip()
        for node in art.findall("PublicationTypeList/PublicationType")
        if "".join(node.itertext()).strip()
    ]

    # 排除明确的非原创文章类型。
    if any(t in NON_ORIGINAL_TYPES for t in pub_types):
        excluded.append({"pmid": pmid, "title": title, "reason": ", ".join(pub_types)})
        continue

    abstract_parts = []
    for node in art.findall("Abstract/AbstractText"):
        label = node.attrib.get("Label")
        content = "".join(node.itertext()).strip()
        if content:
            abstract_parts.append(f"{label}: {content}" if label else content)
    abstract = " ".join(abstract_parts)

    # 月报主要面向科研论文：没有摘要的内容先不纳入。
    if len(abstract) < 100:
        excluded.append({"pmid": pmid, "title": title, "reason": "No substantial abstract"})
        continue

    papers.append({
        "journal": journal,
        "title": title,
        "date": date,
        "disease": "",
        "tags": [],
        "question": "",
        "methods": abstract[:600],
        "finding": "",
        "pmid": pmid,
        "publication_types": pub_types,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps({
        "month": f"{YEAR} 年 {MONTH} 月 · {JOURNAL} · 原创研究筛选",
        "papers": papers
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print(f"Search: {term}")
print(f"PubMed records found: {len(pmids)}")
print(f"Original-study candidates kept: {len(papers)}")
print(f"Excluded: {len(excluded)}")
for item in excluded:
    print(f"EXCLUDED PMID {item['pmid']}: {item['reason']} | {item['title']}")
print(f"Saved {len(papers)} papers to {OUT}")
