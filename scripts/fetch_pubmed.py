import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

JOURNAL = "Radiology"
YEAR = 2026
MONTH = 8
RETMAX = 200
OUT = Path("data/latest.json")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"

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


def load_summary_cache():
    if not OUT.exists():
        return {}
    try:
        old = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}

    cache = {}
    for paper in old.get("papers", []):
        pmid = str(paper.get("pmid", "")).strip()
        if not pmid:
            continue
        if all(str(paper.get(k, "")).strip() for k in ("title_zh", "question", "methods", "finding")):
            cache[pmid] = {
                "title_zh": paper.get("title_zh", ""),
                "question": paper.get("question", ""),
                "methods": paper.get("methods", ""),
                "finding": paper.get("finding", ""),
            }
    return cache


def parse_json_object(raw_text):
    raw_text = (raw_text or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.I)
    raw_text = re.sub(r"\s*```$", "", raw_text)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def deepseek_chinese_summary(title, abstract):
    system_prompt = """你是一名医学影像学科研文献编辑。请严格依据用户提供的英文论文题目和PubMed摘要生成中文科研月报内容。不得补充摘要中没有的信息，不得臆测结果。必须输出合法JSON。"""

    user_prompt = f"""请输出以下JSON结构：
{{
  "title_zh": "专业、忠实、自然的中文论文题目",
  "question": "用1句话概括研究问题",
  "methods": "用1到2句话概括研究设计、研究对象/样本、影像技术与关键分析方法",
  "finding": "用1到2句话概括最重要结果和结论；摘要有关键数字、AUC、敏感度等时优先保留"
}}

英文题目：
{title}

PubMed摘要：
{abstract}
"""

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 900,
    }

    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=90) as r:
        response_json = json.loads(r.read().decode("utf-8"))

    raw = response_json["choices"][0]["message"]["content"]
    result = parse_json_object(raw)
    return {
        "title_zh": str(result.get("title_zh", "")).strip(),
        "question": str(result.get("question", "")).strip(),
        "methods": str(result.get("methods", "")).strip(),
        "finding": str(result.get("finding", "")).strip(),
    }


summary_cache = load_summary_cache()
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

print(f"DeepSeek Chinese summaries enabled: {'yes' if DEEPSEEK_API_KEY else 'no'}")
print(f"DeepSeek model: {DEEPSEEK_MODEL}")

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
    if not china_affiliations:
        excluded_non_china.append({"pmid": pmid, "title": title})
        continue

    summary = summary_cache.get(pmid, {
        "title_zh": "",
        "question": "",
        "methods": "",
        "finding": "",
    })

    if pmid in summary_cache:
        print(f"Using cached Chinese summary for PMID {pmid}")
    elif DEEPSEEK_API_KEY:
        try:
            print(f"DeepSeek summarizing PMID {pmid}...")
            summary = deepseek_chinese_summary(title, abstract)
            time.sleep(0.2)
        except Exception as e:
            print(f"WARNING: DeepSeek summary failed for PMID {pmid}: {type(e).__name__}: {e}")

    papers.append({
        "journal": journal,
        "title": title,
        "title_zh": summary["title_zh"],
        "date": date,
        "disease": "",
        "tags": [],
        "question": summary["question"],
        "methods": summary["methods"],
        "finding": summary["finding"],
        "abstract": abstract,
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
        "ai_summary": bool(DEEPSEEK_API_KEY),
        "ai_provider": "DeepSeek" if DEEPSEEK_API_KEY else "",
        "ai_model": DEEPSEEK_MODEL if DEEPSEEK_API_KEY else "",
        "papers": papers
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print(f"Search: {term}")
print(f"PubMed records found: {len(pmids)}")
print(f"Excluded as non-original/no abstract: {len(excluded_non_original)}")
print(f"Excluded because no China affiliation: {len(excluded_non_china)}")
print(f"China-affiliated original studies kept: {len(papers)}")
print(f"Saved {len(papers)} papers to {OUT}")
