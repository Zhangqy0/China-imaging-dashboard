import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

JOURNAL = "Radiology"
RETMAX = 5
OUT = Path("data/latest.json")


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get_xml(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return ET.fromstring(r.read())


def text(el, path, default=""):
    node = el.find(path)
    if node is None or node.text is None:
        return default
    return "".join(node.itertext()).strip()


# 1) Search PubMed for the latest papers from one journal
term = f'"{JOURNAL}"[jour]'
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
    raise SystemExit("No PubMed records found.")

# 2) Fetch full records
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
for article in root.findall(".//PubmedArticle"):
    citation = article.find("MedlineCitation")
    art = citation.find("Article") if citation is not None else None
    if art is None:
        continue

    title = text(art, "ArticleTitle", "Untitled")
    journal = text(art, "Journal/Title", JOURNAL)
    year = text(art, "Journal/JournalIssue/PubDate/Year")
    medline_date = text(art, "Journal/JournalIssue/PubDate/MedlineDate")
    date = year or medline_date or ""
    pmid = text(citation, "PMID")

    abstract_parts = []
    for node in art.findall("Abstract/AbstractText"):
        label = node.attrib.get("Label")
        content = "".join(node.itertext()).strip()
        if content:
            abstract_parts.append(f"{label}: {content}" if label else content)
    abstract = " ".join(abstract_parts)

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
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps({
        "month": "PubMed 最新示例",
        "papers": papers
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print(f"Saved {len(papers)} papers to {OUT}")
