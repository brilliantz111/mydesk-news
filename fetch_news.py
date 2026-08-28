#!/usr/bin/env python3
"""Fetch Chinese RSS feeds and build news.json for the dashboard.

Runs on GitHub Actions runners. Each source is fault-tolerant: a failing
source is skipped, a failing category keeps the previous file's entries.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
PER_SOURCE = 18
PER_CATEGORY = 45
TIMEOUT = 25

SOURCES = {
    "tech": [
        ("IT之家", "https://www.ithome.com/rss/"),
        ("36氪", "https://36kr.com/feed"),
        ("Solidot", "https://www.solidot.org/index.rss"),
        ("少数派", "https://sspai.com/feed"),
        ("机器之心", "https://rsshub.app/jiqizhijin"),
        ("开源中国", "https://www.oschina.net/news/rss"),
    ],
    "finance": [
        ("华尔街见闻", "https://dedicated.wallstreetcn.com/rss.xml"),
        ("雪球", "https://rsshub.app/xueqiu/today"),
        ("东方财富", "https://rsshub.app/eastmoney/report/stock"),
        ("新浪财经", "https://rsshub.app/newsin/finance"),
    ],
    "game": [
        ("Steam", "https://store.steampowered.com/feeds/news/"),
        ("游民星空", "https://rss.gamersky.com/rss/news.xml"),
        ("小黑盒", "https://rsshub.app/xiaoheihe"),
        ("3DM", "https://rsshub.app/3dm/news"),
    ],
}

IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def norm_title(t):
    return re.sub(r"\s+", "", t or "").lower()


def first_img(entry):
    for m in entry.get("media_content", []) or []:
        if m.get("url"):
            return m["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href", "")
    html = entry.get("summary", "") or entry.get("description", "") or ""
    m = IMG_RE.search(html)
    return m.group(1) if m else ""


def entry_date(entry):
    for key in ("published", "updated"):
        v = entry.get(key, "")
        if v:
            return v[:16]
    return ""


def fetch_source(src):
    name, url = src
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    fp = feedparser.parse(r.content)
    items = []
    for e in fp.entries[:PER_SOURCE]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "date": entry_date(e),
            "img": first_img(e),
            "src": name,
        })
    if not items:
        raise RuntimeError("no items parsed")
    return items


def fetch_category(prev_items):
    seen = set()
    if prev_items:
        for it in prev_items:
            seen.add(norm_title(it.get("title", "")))
    merged = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_source, s): s[0] for s in sources}
        for fut in as_completed(futures):
            src_name = futures[fut]
            try:
                items = fut.result()
            except Exception as exc:
                print(f"  [skip] {src_name}: {exc}", file=sys.stderr)
                continue
            print(f"  [ok]   {src_name}: {len(items)} items", file=sys.stderr)
            merged.extend(items)
    merged.sort(key=lambda it: it.get("date", ""), reverse=True)
    out, out_seen = [], set()
    for it in merged:
        k = norm_title(it["title"])
        if not k or k in seen or k in out_seen:
            continue
        out_seen.add(k)
        out.append(it)
        if len(out) >= PER_CATEGORY:
            break
    return out


def main():
    try:
        with open("news.json", "r", encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = {}
    prev_cats = (prev or {}).get("categories", {})

    cats = {}
    for cat, srcs in SOURCES.items():
        globals()["sources"] = srcs
        items = fetch_category(prev_cats.get(cat, []))
        if items:
            cats[cat] = items
        elif prev_cats.get(cat):
            cats[cat] = prev_cats[cat]
            print(f"[keep] {cat}: kept {len(prev_cats[cat])} previous items", file=sys.stderr)

    doc = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(time.time() + 8 * 3600)), "categories": cats}
    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    counts = {k: len(v) for k, v in cats.items()}
    print(f"[done] {counts}")


if __name__ == "__main__":
    main()
