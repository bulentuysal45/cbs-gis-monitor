import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List

from rule_engine import COUNTRY_SETTINGS, LANGUAGE_CODES, SearchRule, build_search_query, verify_content_against_rule


@dataclass
class FoundItem:
    kelime: str
    dil: str
    konum: str
    kaynak_tipi: str
    baslik: str
    ozet: str
    url: str
    tarih: str = ""


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def fetch_url(url: str, headers: dict = None, data: bytes = None, timeout: int = 12) -> str:
    req_headers = {"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def search_reddit_latest(rule: SearchRule, max_results: int = 10) -> List[FoundItem]:
    results: List[FoundItem] = []
    q = f'"{rule.kelime}"'
    params = urllib.parse.urlencode({
        "q": q,
        "sort": "new",
        "t": "week",
        "limit": max_results,
    })
    url = f"https://www.reddit.com/search.json?{params}"
    try:
        raw = fetch_url(url, headers={"User-Agent": "CBSGISMonitorBot/1.0"})
        data = json.loads(raw)
        children = data.get("data", {}).get("children", [])
        for child in children:
            post = child.get("data", {})
            title = post.get("title", "")
            selftext = post.get("selftext", "")
            permalink = post.get("permalink", "")
            post_url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
            subreddit = post.get("subreddit_name_prefixed", "r/reddit")
            combined_snippet = f"[{subreddit}] {selftext[:400]}"

            ok, matched_ctx = verify_content_against_rule(rule, title, combined_snippet, post_url)
            if ok:
                results.append(
                    FoundItem(
                        kelime=rule.kelime,
                        dil=rule.dil,
                        konum=rule.konum,
                        kaynak_tipi="Reddit (Son Gönderiler)",
                        baslik=title,
                        ozet=matched_ctx,
                        url=post_url,
                        tarih="Son Gönderiler (Yeni)",
                    )
                )
    except Exception:
        pass
    return results


def search_google_news_rss(rule: SearchRule) -> List[FoundItem]:
    results: List[FoundItem] = []
    country_cfg = COUNTRY_SETTINGS.get(rule.konum, COUNTRY_SETTINGS["TÜRKİYE"])
    lang_code = LANGUAGE_CODES.get(rule.dil, "tr")
    gl = country_cfg["gl"].upper()

    base_q = build_search_query(rule, platform="news")
    params = urllib.parse.urlencode({
        "q": f"{base_q} when:7d",
        "hl": lang_code,
        "gl": gl,
        "ceid": f"{gl}:{lang_code}",
    })
    url = f"https://news.google.com/rss/search?{params}"
    try:
        xml_text = fetch_url(url)
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item")[:10]:
            title = strip_html_tags(item.findtext("title") or "")
            desc = strip_html_tags(item.findtext("description") or "")
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            if not link:
                continue

            ok, matched_ctx = verify_content_against_rule(rule, title, desc, link)
            if ok:
                results.append(
                    FoundItem(
                        kelime=rule.kelime,
                        dil=rule.dil,
                        konum=rule.konum,
                        kaynak_tipi="Haber / Duyuru (Yeni)",
                        baslik=title,
                        ozet=matched_ctx,
                        url=link,
                        tarih=pub_date,
                    )
                )
    except Exception:
        pass
    return results


def search_duckduckgo_html(
    rule: SearchRule, platform_filter: str = "", kaynak_etiketi: str = "Web (Son 7 Gün)", df: str = "w"
) -> List[FoundItem]:
    results: List[FoundItem] = []
    base_q = build_search_query(rule, platform="web")
    full_q = f"{platform_filter} {base_q}".strip() if platform_filter else base_q

    country_cfg = COUNTRY_SETTINGS.get(rule.konum, COUNTRY_SETTINGS["TÜRKİYE"])
    form_data = urllib.parse.urlencode({
        "q": full_q,
        "kl": country_cfg["ddg_region"],
        "df": df,
    }).encode("utf-8")

    try:
        raw_html = fetch_url(
            "https://html.duckduckgo.com/html/",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://html.duckduckgo.com/"},
            data=form_data,
        )
        blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?<a class="result__snippet"[^>]*>(.*?)</a>',
            raw_html,
            flags=re.DOTALL,
        )
        for href, raw_title, raw_snippet in blocks[:10]:
            link = clean_ddg_url(href)
            title = strip_html_tags(raw_title)
            snippet = strip_html_tags(raw_snippet)
            if not link:
                continue

            ok, matched_ctx = verify_content_against_rule(rule, title, snippet, link)
            if ok:
                results.append(
                    FoundItem(
                        kelime=rule.kelime,
                        dil=rule.dil,
                        konum=rule.konum,
                        kaynak_tipi=decide_source_label(link, kaynak_etiketi),
                        baslik=title,
                        ozet=matched_ctx,
                        url=link,
                        tarih="Son 7 Gün (Yeni İçerik)",
                    )
                )
    except Exception:
        pass
    return results


def clean_ddg_url(href: str) -> str:
    href = html.unescape(href)
    if "uddg=" in href:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if "uddg" in parsed:
            return parsed["uddg"][0]
    return href


def decide_source_label(url: str, default_label: str) -> str:
    u = url.lower()
    if "reddit.com" in u:
        return "Reddit (Son Gönderiler)"
    if "x.com/" in u or "twitter.com/" in u:
        return "X (Twitter)"
    if "instagram.com/" in u:
        return "Instagram"
    if "facebook.com/" in u:
        return "Facebook"
    if "linkedin.com/" in u:
        return "LinkedIn (İş İlanı / Paylaşım)"
    if any(k in u for k in ("kariyer.net", "ilan.gov.tr", "yenibiris.com", "boss.az", "hh.uz")):
        return "İş İlanı / Resmi Duyuru"
    return default_label


def collect_for_rule(rule: SearchRule) -> List[FoundItem]:
    collected: List[FoundItem] = []

    # 1. Google News RSS (Son 7 günde yayınlanan gerçek haber ve duyurular)
    collected.extend(search_google_news_rss(rule))

    # 2. DuckDuckGo Son 1 Hafta (df=w) - Sadece yeni içerikler (statik ana sayfalar elenir)
    collected.extend(search_duckduckgo_html(rule, kaynak_etiketi="Web (Son 7 Gün)", df="w"))

    # 3. Sosyal Medya ve İş İlanı Platformları (LinkedIn, X, Instagram, Facebook, Kariyer)
    social_filter = "(site:linkedin.com/posts OR site:linkedin.com/jobs OR site:x.com OR site:instagram.com OR site:facebook.com OR site:kariyer.net/is-ilani)"
    collected.extend(search_duckduckgo_html(rule, platform_filter=social_filter, kaynak_etiketi="Sosyal Medya / İlan", df="m"))

    # 4. Reddit Doğrudan Uygulama İçi Arama (sort=new)
    collected.extend(search_reddit_latest(rule))
    time.sleep(0.25)

    return collected
