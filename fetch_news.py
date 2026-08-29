#!/usr/bin/env python3
"""Fetch Chinese news feeds and build news.json for the dashboard.

Runs on GitHub Actions. Each source is fault-tolerant: a failing source is
skipped, a failing category keeps the previous file's entries.
English titles (e.g. Steam) are machine-translated to Chinese server-side.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

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
        ("新浪7x24", "special:sina7x24"),
        ("雪球", "rsshub://xueqiu/today"),
        ("东方财富", "rsshub://eastmoney/report/stock"),
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
CJK_RE = re.compile(r"[一-鿿]")


def norm_title(t):
    return re.sub(r"\s+", "", t or "").lower()


def has_cjk(s):
    return bool(CJK_RE.search(s or ""))


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
                return out[:PER_CATEGORY]
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"xiaoheihe channels failed: {last_err}")


def fetch_woshipm(name, _url):
    items, seen = [], set()
    try:
        r = requests.get("https://www.woshipm.com/", headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for m in WOSHIPM_RE.finditer(r.text):
            link = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if link in seen or len(title) < 6:
                continue
            seen.add(link)
            items.append({"title": title, "link": link, "date": "", "img": "", "src": name})
    except Exception as exc:
        print(f"  [woshipm] homepage: {exc}", file=sys.stderr)
    if len(items) < PER_CATEGORY:
        # 分类页（SSR，与首页同构）补足
        for cat in ("it", "pd", "operate", "marketing", "ucd"):
            if len(items) >= PER_CATEGORY:
                break
            try:
                r = requests.get(f"https://www.woshipm.com/category/{cat}", headers=UA, timeout=TIMEOUT)
                r.raise_for_status()
                for m in WOSHIPM_RE.finditer(r.text):
                    link = m.group(1)
                    title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                    if link in seen or len(title) < 6:
                        continue
                    seen.add(link)
                    items.append({"title": title, "link": link, "date": "", "img": "", "src": name})
                    if len(items) >= PER_CATEGORY:
                        break
            except Exception as exc:
                print(f"  [woshipm] category {cat}: {exc}", file=sys.stderr)
    if not items:
        raise RuntimeError("woshipm no items")
    return items[:PER_CATEGORY]


def fetch_sina7x24(name, _url):
    items, seen = [], set()
    for page in (1, 2, 3):
        r = requests.get(
            f"https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=25&zhibo_id=152",
            headers=UA, timeout=TIMEOUT,
        )
        j = r.json()
        for it in j["result"]["data"]["feed"]["list"]:
            text = re.sub(r"<[^>]+>", "", it.get("rich_text") or "").strip()
            if len(text) < 8:
                continue
            title = text[:64]
            if title in seen:
                continue
            seen.add(title)
            items.append({
                "title": title,
                "link": "https://finance.sina.com.cn/7x24/",
                "date": (it.get("create_time") or "")[:16],
                "img": "",
                "src": name,
            })
        if len(items) >= PER_CATEGORY:
            break
    if not items:
        raise RuntimeError("sina7x24 no items")
    return items[:PER_CATEGORY]


SPECIAL_FETCHERS = {
    "xiaoheihe": fetch_xiaoheihe,
    "woshipm": fetch_woshipm,
    "sina7x24": fetch_sina7x24,
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


def translate_non_cjk(items):
    todo = [it for it in items if it.get("link", "").startswith("http") and not has_cjk(it["title"])]

    def tr(it):
        q = quote(it["title"][:300])
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=" + q,
                headers=UA, timeout=12,
            )
            segs = r.json()[0]
            zh = "".join(s[0] for s in segs)
            if zh:
                it["title"] = zh
                return
        except Exception:
            pass
        try:
            r = requests.get(
                "https://api.mymemory.translated.net/get?langpair=en-US|zh-CN&q=" + q,
                headers=UA, timeout=12,
            )
            zh = (r.json().get("responseData") or {}).get("translatedText") or ""
            if zh and has_cjk(zh):
                it["title"] = zh
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(tr, todo))
    return items


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
    merged = translate_non_cjk(merged)
    out, out_seen = [], set()
    for it in merged:
        k = norm_title(it["title"])
        if not k or k in seen or k in out_seen:
            continue
        out_seen.add(k)
        out.append(it)
        if len(out) >= PER_CATEGORY:
            break
    # 不足时用往期条目补足，保证栏目稳定满额
    if len(out) < PER_CATEGORY and prev_items:
        for it in prev_items:
            k = norm_title(it.get("title", ""))
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
