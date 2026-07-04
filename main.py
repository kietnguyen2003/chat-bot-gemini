import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from markdownify import markdownify as md
from slugify import slugify


# =========================
# Config
# =========================

BASE_URL = "https://support.optisigns.com/api/v2/help_center/en-us/articles.json"

DATA_DIR = Path("output/data/articles")
STATE_FILE = Path("output/state/articles.json")
LAST_RUN_FILE = Path("output/last_run.json")

PER_PAGE = 100

MODEL = "gemini-flash-latest"

SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.
Tone: helpful, factual, concise.
Only answer using the uploaded docs.
Max 5 bullet points; else link to the doc.
Cite up to 3 "Article URL:" lines per reply.
"""


# =========================
# Basic helpers
# =========================

def load_state():
    if not STATE_FILE.exists():
        return {}

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def save_last_run(summary):
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, ensure_ascii=False)


def create_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY in .env")

    return genai.Client(api_key=api_key)


# =========================
# Scraper
# =========================

def fetch_articles():
    articles = []
    url = f"{BASE_URL}?per_page={PER_PAGE}"

    while url:
        print(f"Fetching: {url}")

        response = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()
        articles.extend(data.get("articles", []))
        url = data.get("next_page")
    print("number of articles: ", len(articles))

    return articles[:103]


def clean_html(html):
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "iframe", "noscript"]):
        tag.decompose()

    for tag in soup.find_all(True):
        allowed_attrs = {}

        if tag.name == "a" and tag.get("href"):
            allowed_attrs["href"] = tag.get("href")

        if tag.name == "img" and tag.get("src"):
            allowed_attrs["src"] = tag.get("src")
            if tag.get("alt"):
                allowed_attrs["alt"] = tag.get("alt")

        tag.attrs = allowed_attrs

    return str(soup)


def html_to_markdown(html):
    cleaned_html = clean_html(html)

    markdown = md(
        cleaned_html,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )

    lines = []
    blank_count = 0

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()

        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                lines.append("")
        else:
            blank_count = 0
            lines.append(line)

    return "\n".join(lines).strip()


def build_markdown(article):
    title = article.get("title") or "Untitled"
    article_url = article.get("html_url") or ""
    updated_at = article.get("updated_at") or ""
    body_html = article.get("body") or ""

    body_markdown = html_to_markdown(body_html)

    return f"""---
title: "{title}"
article_url: "{article_url}"
updated_at: "{updated_at}"
source: "OptiSigns Support"
---

# {title}

Article URL: {article_url}

{body_markdown}
""".strip() + "\n"


def save_article(article):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    article_id = str(article.get("id"))
    title = article.get("title") or f"article-{article_id}"
    slug = slugify(title) or f"article-{article_id}"

    file_path = DATA_DIR / f"{slug}.md"

    markdown = build_markdown(article)
    markdown_hash = create_hash(markdown)

    file_path.write_text(markdown, encoding="utf-8")

    return {
        "article_id": article_id,
        "title": title,
        "article_url": article.get("html_url") or "",
        "updated_at": article.get("updated_at") or "",
        "filename": str(file_path),
        "hash": markdown_hash,
    }


# =========================
# Delta detection
# =========================

def scrape_and_detect_changes():
    old_state = load_state()
    new_state = dict(old_state)

    added = []
    updated = []
    skipped = []

    articles = fetch_articles()

    for article in articles:
        result = save_article(article)
        article_id = result["article_id"]

        old_article = old_state.get(article_id)

        if old_article is None:
            status = "added"
            added.append(result)

        elif old_article.get("hash") != result["hash"]:
            status = "updated"
            updated.append(result)

            # Keep old document name so we can delete old version before re-uploading.
            if old_article.get("document_name"):
                result["document_name"] = old_article["document_name"]

        else:
            status = "skipped"
            skipped.append(result)

            # Keep document name for unchanged articles.
            if old_article.get("document_name"):
                result["document_name"] = old_article["document_name"]

        new_state[article_id] = result
        print(f"[{status}] {result['title']}")

    save_state(new_state)

    delta_items = added + updated

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_fetched": len(articles),
        "added": len(added),
        "updated": len(updated),
        "skipped": len(skipped),
        "delta_files": [item["filename"] for item in delta_items],
    }

    return summary


# =========================
# Gemini upload
# =========================

def wait_for_operation(client, operation):
    while not operation.done:
        print("Indexing file...")
        time.sleep(5)
        operation = client.operations.get(operation)

    return operation


def get_document_name(operation):
    response = getattr(operation, "response", None)

    if response is None:
        return None

    document = getattr(response, "document", None)

    if document is not None:
        return getattr(document, "name", None)

    return getattr(response, "name", None)


def delete_old_document(client, document_name):
    if not document_name:
        return

    try:
        client.file_search_stores.documents.delete(
            name=document_name,
            config={"force": True},
        )
        print(f"Deleted old document: {document_name}")
    except Exception as error:
        print(f"Could not delete old document: {error}")


def upload_file(client, store_name, file_path):
    operation = client.file_search_stores.upload_to_file_search_store(
        file=str(file_path),
        file_search_store_name=store_name,
        config={
            "display_name": file_path.name,
            "mime_type": "text/markdown",
        },
    )

    operation = wait_for_operation(client, operation)
    document_name = get_document_name(operation)

    print(f"Uploaded: {file_path.name}")

    return document_name


def upload_delta_files(summary):
    store_name = os.getenv("GEMINI_FILE_SEARCH_STORE_NAME")

    if not store_name:
        raise RuntimeError("Missing GEMINI_FILE_SEARCH_STORE_NAME in .env")

    delta_files = summary["delta_files"]

    if len(delta_files) == 0:
        print("No new or updated files. Nothing to upload.")
        return {
            "uploaded_files": 0,
            "store_name": store_name,
        }

    client = get_gemini_client()
    state = load_state()

    uploaded_count = 0

    for article_id, article in state.items():
        filename = article.get("filename")

        if filename not in delta_files:
            continue

        file_path = Path(filename)

        if not file_path.exists():
            print(f"File not found, skipped: {file_path}")
            continue

        old_document_name = article.get("document_name")
        delete_old_document(client, old_document_name)

        new_document_name = upload_file(client, store_name, file_path)

        if new_document_name:
            article["document_name"] = new_document_name
            state[article_id] = article

        uploaded_count += 1

    save_state(state)

    return {
        "uploaded_files": uploaded_count,
        "store_name": store_name,
    }


def main():
    load_dotenv()

    print("Starting daily job...")

    summary = scrape_and_detect_changes()
    upload_result = upload_delta_files(summary)

    summary["gemini_upload"] = upload_result

    save_last_run(summary)

    print("\nJob finished.")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
