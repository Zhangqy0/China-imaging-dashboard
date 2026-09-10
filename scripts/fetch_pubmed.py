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

# 第一版“中国机构”判断：只根据 PubMed 作者单位地址，不判断作者国籍。
CHINA_AFFILIATION_KEYWORDS = (
    "china",
    "hong kong",
    "macao",
    "macau",
)


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


def extract_authors_and_affiliations(art):
    authors = []
    affiliations = []

    for author in art.findall("AuthorList/Author"):
        collective = text(author, "CollectiveName")
        if collective:
            authors.append(collective)
        else:
            last = text(author, "LastName")
            initials = text(author, "Initials")
            name = " ".join(x for x in [last, initials] if x)
            if name:
                authors.append(name)

        for aff_node in author.findall("AffiliationInfo/Affiliation"):
            aff = "".join(aff_node.itertext()).strip()
            if aff and aff not in affiliations:
                affiliations.append(aff)

    return authors, affiliations


def is_china_affiliation(affiliation):
    a = affiliation.lower()
    return any(keyword in a for keyword in CHINA_AFFILIATION_KEYWORDS)


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
excluded_non_original = []
excluded_non_china = []

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

    if any(t in NON_ORIGINAL_TYPES for t in pub_types):
        excluded_non_original.append({"pmid": pmid, "title": title, "reason": ", ".join(pub_types)})
        continue

    abstract_parts = []
    for node in art.findall("Abstract/AbstractText"):
        label = node.attrib.get("Label")
        content = "".join(node.itertext()).strip()
        if content:
            abstract_parts.append(f"{label}: {content}" if label else content)
    abstract = " ".join(abstract_parts)

    if len(abstract) < 100:
        excluded_non_original.append({"pmid": pmid, "title": title, "reason": "No substantial abstract"})
        continue

    authors, affiliations = extract_authors_and_affiliations(art)
    china_affiliations = [aff for aff in affiliations if is_china_affiliation(aff)]

    # 只保留至少有一个中国机构作者单位的论文。
    if not china_affiliations:
        excluded_non_china.append({"pmid": pmid, "title": title})
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
        "authors": authors,
        "china_affiliations": china_affiliations,
        "publication_types": pub_types,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps({
        "month": f"{YEAR} 年 {MONTH} 月 · {JOURNAL} · 中国机构原创研究",
        "papers": papers
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print(f"Search: {term}")
print(f"PubMed records found: {len(pmids)}")
print(f"Excluded as non-original/no abstract: {len(excluded_non_original)}")
print(f"Excluded because no China affiliation: {len(excluded_non_china)}")
print(f"China-affiliated original studies kept: {len(papers)}")
for p in papers:
    print(f"KEPT PMID {p['pmid']}: {p['title']}")
    for aff in p['china_affiliations']:
        print(f"  China affiliation: {aff}")
print(f"Saved {len(papers)} papers to {OUT}")
