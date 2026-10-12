# cn_extra_sources.py — Google News 中文替代源（免 key，2026-10-11 实测）
#
# 用法：把本文件丢进仓库 discovery_engine.py 旁边；
# fetch_all_cn_extra() 的结果和财联社/金十的一起做按 id 去重，
# 走同一套 10 大主题关键词打分 + CSV 入库管线即可。
# 只用标准库 urllib，requirements.txt 保持只有 curl-cffi 不变。

import json, re, time, urllib.request
from html import unescape

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}

def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")

def _clean(html):
    return unescape(re.sub(r"<[^>]+>", " ", html or "")).strip()

def fetch_wallstreetcn(channel="a-stock-channel", limit=100):
    """华尔街见闻快讯。channel: 'a-stock-channel' (A股) 或 'global-channel' (全球)。
    免 key；字段 id/title/content_text/content/display_time(unix)/related_themes；
    cursor=上一页末条 id 翻页。注意：实测 A 股频道 symbols 字段多为空，
    A 股关联仍主要靠财联社 stock_list + 关键词映射，别指望这个字段。"""
    items, cursor = [], ""
    for _ in range(3):
        url = (f"https://api-one-wscn.awtmt.com/apiv1/content/lives"
               f"?channel={channel}&client=pc&cursor={cursor}&limit={limit}")
        batch = json.loads(_get(url))["data"]["items"]
        if not batch:
            break
        for it in batch:
            items.append({"source": f"wscn-{channel}", "id": str(it["id"]),
                          "title": it.get("title") or "",
                          "text": _clean(it.get("content_text") or it.get("content")),
                          "published_at": it.get("display_time")})
        cursor = batch[-1]["id"]
        time.sleep(1)
    return items

def fetch_sina_7x24(pages=3, page_size=50):
    """新浪财经 7x24 快讯。免 key；字段 id/rich_text(HTML)/create_time/docurl；
    按 page 翻页。"""
    items = []
    for p in range(1, pages + 1):
        url = (f"https://zhibo.sina.com.cn/api/zhibo/feed?page={p}&pageSize={page_size}"
               f"&zhibo_id=152&tag_id=0&dire=f&dpc=1")
        lst = json.loads(_get(url))["result"]["data"]["feed"]["list"]
        if not lst:
            break
        for it in lst:
            items.append({"source": "sina-7x24", "id": str(it["id"]),
                          "title": "", "text": _clean(it.get("rich_text")),
                          "published_at": it.get("create_time"),
                          "url": it.get("docurl") or ""})
        time.sleep(1)
    return items

def fetch_36kr_feed():
    """36氪官方 RSS。标准 RSS，约 25KB，字段 guid/title/description/pubDate/link。"""
    import xml.etree.ElementTree as ET
    items = []
    for node in ET.fromstring(_get("https://36kr.com/feed")).iter("item"):
        items.append({"source": "36kr",
                      "id": node.findtext("guid") or node.findtext("link"),
                      "title": node.findtext("title") or "",
                      "text": _clean(node.findtext("description")),
                      "published_at": node.findtext("pubDate"),
                      "url": node.findtext("link") or ""})
    return items

def fetch_all_cn_extra():
    return fetch_wallstreetcn() + fetch_sina_7x24() + fetch_36kr_feed()
