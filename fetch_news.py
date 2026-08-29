#!/usr/bin/env python3
"""Fetch Chinese news feeds and build news.json for the dashboard.

Runs on GitHub Actions. Each source is fault-tolerant: a failing source is
skipped, a failing category keeps the previous file's entries.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
PER_SOURCE = 40
PER_CATEGORY = 50
TIMEOUT = 25

RSSHUB_BASES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://hub.slarker.me",
    "https://rsshub.pseudoyu.com",
    "https://rss.owo.nz",
    "https://rsshub.ktachibana.party",
]

SOURCES = {
    "tech": [
        ("IT之家", "https://www.ithome.com/rss/"),
        ("36氪", "https://36kr.com/feed"),
        ("Solidot", "https://www.solidot.org/index.rss"),
        ("少数派", "https://sspai.com/feed"),
        ("机器之心", "rsshub://jiqizhijin"),
        ("开源中国", "https://www.oschina.net/news/rss"),
    ],
    "finance": [
        ("华尔街见闻", "https://dedicated.wallstreetcn.com/rss.xml"),
        ("雪球", "rsshub://xueqiu/today"),
        ("东方财富", "rsshub://eastmoney/report/stock"),
        ("新浪财经", "rsshub://newsin/finance"),
    ],
    "game": [
        ("小黑盒热榜", "special:xiaoheihe"),
        ("机核", "https://www.gcores.com/rss"),
        ("游民星空", "https://rss.gamersky.com/rss/news.xml"),
        ("3DM", "rsshub://3dm/news"),
        ("Steam", "https://store.steampowered.com/feeds/news/"),
    ],
    "product": [
        ("人人都是产品经理", "special:woshipm"),
    ],
}

IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
WOSHIPM_RE = re.compile(r'<a[^>]+href="(https://www\.woshipm\.com/[a-z\-]+/\d+\.html)"[^>]*>(.*?)</a>', re.S)


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


def entries_to_items(fp, name):
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
    return items


def fetch_rss(url, name):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    items = entries_to_items(feedparser.parse(r.content), name)
    if not items:
        raise RuntimeError("no items parsed")
    return items


def fetch_xiaoheihe(name, _url):
    attempts = [b + "/xiaoheihe/news" for b in RSSHUB_BASES]
    attempts.append("https://news.xiaoheihe.cn/")
    last_err = None
    for u in attempts:
        try:
            r = requests.get(u, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            if "<item" in r.text or "<entry" in r.text:
                items = entries_to_items(feedparser.parse(r.content), name)
                if items:
                    return items
                continue
            out, seen = [], set()
            for m in re.finditer(r'href="(https?://[^"]*xiaoheihe\.cn/[^"]+)"[^>]*>([^<]{4,60})<', r.text):
                link, title = m.group(1), m.group(2).strip()
                if link in seen or not title:
                    continue
                seen.add(link)
                out.append({"title": title, "link": link, "date": "", "img": "", "src": name})
            if out:
                return out[:PER_SOURCE]
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"xiaoheihe channels failed: {last_err}")


def fetch_woshipm(name, _url):
    pages = [
        "https://www.woshipm.com/",
        "https://www.woshipm.com/category/it",
        "https://www.woshipm.com/category/pd",
        "https://www.woshipm.com/category/operate",
        "https://www.woshipm.com/category/marketing",
        "https://www.woshipm.com/category/ucd",
        "https://www.woshipm.com/category/zhichang",
        "https://www.woshipm.com/category/pmd",
    ]
    items, seen = [], set()
    for u in pages:
        try:
            r = requests.get(u, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as exc:
            print(f"  [woshipm] {u}: {exc}", file=sys.stderr)
            continue
        for m in WOSHIPM_RE.finditer(r.text):
            link = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if link in seen or len(title) < 6:
                continue
            seen.add(link)
            items.append({"title": title, "link": link, "date": "", "img": "", "src": name})
            if len(items) >= PER_CATEGORY:
                return items
    if not items:
        raise RuntimeError("woshipm no items")
    return items[:PER_CATEGORY]


SPECIAL_FETCHERS = {
    "xiaoheihe": fetch_xiaoheihe,
    "woshipm": fetch_woshipm,
}


def fetch_source(src):
    name, url = src
    if url.startswith("special:"):
        fn = SPECIAL_FETCHERS.get(url.split(":", 1)[1])
        if not fn:
            raise RuntimeError(f"unknown special source {url}")
        return fn(name, url)
    if url.startswith("rsshub://"):
        last_err = None
        for b in RSSHUB_BASES:
            try:
                items = fetch_rss(b + url[len("rsshub://"):], name)
                if items:
                    return items
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"rsshub instances failed: {last_err}")
    return fetch_rss(url, name)


def fetch_category(srcs, prev_items):
    seen = set()
    if prev_items:
        for it in prev_items:
            seen.add(norm_title(it.get("title", "")))
    merged = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_source, s): s[0] for s in srcs}
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
        items = fetch_category(srcs, prev_cats.get(cat, []))
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
