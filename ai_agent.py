import argparse
import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict
from typing import Dict, List, Set, Tuple

from main import send_email_report
from rule_engine import COUNTRY_SETTINGS, LANGUAGE_CODES, SearchRule, build_search_query, is_static_or_homepage_url, load_rules, normalize_turkic, verify_content_against_rule
from search_collectors import FoundItem, fetch_url, strip_html_tags

AGENT_REPORTS_FILE = "agent_reports.json"
AGENT_SEEN_FILE = "agent_seen_urls.json"


def now_tr() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))


def load_json_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_CACHED_GEMINI_MODELS: List[str] = []


def get_available_gemini_models(api_key: str) -> List[str]:
    global _CACHED_GEMINI_MODELS
    if _CACHED_GEMINI_MODELS:
        return _CACHED_GEMINI_MODELS
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                name = m.get("name", "")
                if "generateContent" in methods and "gemini" in name and "vision" not in name and "embedding" not in name:
                    short_name = name.replace("models/", "")
                    models.append(short_name)
            # Flash modellerini öne al
            flash_models = [m for m in models if "flash" in m and "lite" not in m and "8b" not in m]
            other_models = [m for m in models if m not in flash_models]
            _CACHED_GEMINI_MODELS = (flash_models + other_models)[:4]
            if _CACHED_GEMINI_MODELS:
                print(f"[AI AJAN BİLGİ] Aktif Gemini modelleri bulundu: {_CACHED_GEMINI_MODELS}")
                return _CACHED_GEMINI_MODELS
    except Exception as exc:
        print(f"[AI AJAN MODEL LİSTELEME UYARISI] {exc}")
    _CACHED_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash"]
    return _CACHED_GEMINI_MODELS


def call_gemini_api(prompt: str, use_google_search: bool = False) -> Tuple[str, List[dict]]:
    """
    Google Gemini API çağrısı yapar.
    Dönüş: (metin_yanıtı, grounding_web_linkleri)
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[AI AJAN BİLGİ] GEMINI_API_KEY tanımlı değil.")
        return "", []

    models_to_try = get_available_gemini_models(api_key)
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        if use_google_search:
            payload["tools"] = [{"google_search": {}}]

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="replace"))
                cand = raw.get("candidates", [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                text_out = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()

                grounding_chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
                web_chunks = [c.get("web", {}) for c in grounding_chunks if "web" in c]
                return text_out, web_chunks
        except Exception as exc:
            print(f"[AI AJAN MODEL UYARISI - {model_name}] {exc}")

    return "", []


def gemini_autonomous_web_research(rules: List[SearchRule]) -> List[FoundItem]:
    """
    Gemini Yapay Zeka Ajanının Canlı Google Arama (Google Search Grounding) aracını kullanarak
    Türkiye, Azerbaycan ve Özbekistan için insan gibi internette araştırma yapmasını sağlar.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return []

    by_country: Dict[str, List[SearchRule]] = {}
    for r in rules:
        by_country.setdefault(r.konum, []).append(r)

    discovered: List[FoundItem] = []
    today_str = now_tr().strftime("%d.%m.%Y")

    for konum, c_rules in by_country.items():
        kw_list_str = ", ".join(f'"{r.kelime}" ({r.dil})' for r in c_rules)
        prompt = f"""Sen uzman bir Coğrafi Bilgi Sistemleri (CBS / GIS) internet araştırma ajanısın.
Bugünün tarihi: {today_str}. Hedef ülke: {konum}.

Google Arama aracını kullanarak {konum} için şu anahtar kelimelerle ilgili son günlerde yayınlanmış YENİ haberleri, iş ilanlarını, kurum/belediye duyurularını, Resmi Gazete kararlarını, ihaleleri ve sosyal medya (LinkedIn, X, Reddit, Instagram, Facebook) paylaşımlarını araştır:
{kw_list_str}

Kurallar:
- Yıllardır sabit duran kurum ana sayfalarını (örn: cbs.tkgm.gov.tr/, cbs.ibb.gov.tr/, wikipedia.org) DAHİL ETME.
- Bulduğun her yeni gelişmeyi aşağıdaki JSON dizisi formatında listele:
[
  {{
    "kelime": "Eşleşen anahtar kelime",
    "dil": "{c_rules[0].dil}",
    "kaynak_tipi": "🤖 AI Ajan (Google Canlı Araştırma)",
    "baslik": "İçeriğin başlığı",
    "ozet": "🤖 Ajan Özeti: İçeriğin Türkçe 1-2 cümlelik net özeti",
    "url": "https://...",
    "tarih": "Yayın tarihi"
  }}
]"""
        resp_text, web_chunks = call_gemini_api(prompt, use_google_search=True)
        print(f"[AI AJAN ARAŞTIRMA - {konum}] Yanıt uzunluğu: {len(resp_text)}, Grounding kaynak sayısı: {len(web_chunks)}")

        # 1. JSON dizisinden gelen sonuçları ekle
        json_match = re.search(r"\[.*\]", resp_text, flags=re.DOTALL)
        if json_match:
            try:
                arr = json.loads(json_match.group(0))
                for idx, obj in enumerate(arr):
                    u = (obj.get("url") or "").strip()
                    # Eğer model URL yerine grounding chunk kullandıysa eşleştir
                    if (not u or not u.startswith("http")) and idx < len(web_chunks):
                        u = web_chunks[idx].get("uri", "")
                    if not u or is_static_or_homepage_url(u):
                        continue
                    discovered.append(
                        FoundItem(
                            kelime=obj.get("kelime") or c_rules[0].kelime,
                            dil=obj.get("dil") or c_rules[0].dil,
                            konum=konum,
                            kaynak_tipi="🤖 AI Ajan (Google Canlı Araştırma)",
                            baslik=obj.get("baslik") or "Yeni İçerik",
                            ozet=obj.get("ozet") or "",
                            url=u,
                            tarih=obj.get("tarih") or today_str,
                        )
                    )
            except Exception as exc:
                print(f"[AI AJAN JSON AYRIŞTIRMA UYARISI - {konum}] {exc}")

        # 2. Grounding Chunks içindeki doğrulanmış Google kaynaklarını da değerlendir
        for ch in web_chunks:
            uri = (ch.get("uri") or "").strip()
            title = (ch.get("title") or "").strip()
            if not uri or not title or is_static_or_homepage_url(uri):
                continue
            if any(d.url == uri for d in discovered):
                continue
            discovered.append(
                FoundItem(
                    kelime=c_rules[0].kelime,
                    dil=c_rules[0].dil,
                    konum=konum,
                    kaynak_tipi="🤖 AI Ajan (Google Web Kaynağı)",
                    baslik=title,
                    ozet=f"🤖 Ajan Özeti: Google canlı araştırmasında '{konum}' bölgesi için tespit edilen yeni CBS/GIS kaynağı: {title}",
                    url=uri,
                    tarih=today_str,
                )
            )

    return discovered


def collect_direct_fresh_sources(rules: List[SearchRule]) -> List[FoundItem]:
    """
    Google News RSS (tam metin eşleşmeli), LinkedIn Uygulama İçi İlanlar (tam metin doğrulamalı)
    ve Reddit yeni gönderilerini doğrudan toplar.
    """
    items: List[FoundItem] = []

    # A) Google News RSS (Son 14 Gün - Tam Metin Eşleşmeli)
    for rule in rules:
        country_cfg = COUNTRY_SETTINGS.get(rule.konum, COUNTRY_SETTINGS["TÜRKİYE"])
        lang_code = LANGUAGE_CODES.get(rule.dil, "tr")
        gl = country_cfg["gl"].upper()
        norm_konum = normalize_turkic(rule.konum)
        norm_dil = normalize_turkic(rule.dil)

        loc_q = ""
        if norm_dil in ("ingilizce", "english"):
            gl = "US"
            lang_code = "en"
            if norm_konum in ("türkiye", "turkiye"):
                loc_q = " (Turkey OR Türkiye OR Istanbul OR Ankara)"
            elif norm_konum == "azerbaycan":
                loc_q = " (Azerbaijan OR Baku)"
            elif norm_konum in ("özbekistan", "ozbekistan"):
                loc_q = " (Uzbekistan OR Tashkent OR Karakalpakstan)"

        kw = rule.kelime.strip()
        if kw.upper() == "CBS":
            q_str = '"CBS" ("Coğrafi Bilgi Sistemleri" OR "CBS Uzmanı" OR belediye OR bakanlık OR harita) -"CBS News" -"CBS Sports" when:14d'
        elif kw.upper() == "GIS":
            q_str = f'"GIS" ("Geographic Information" OR ArcGIS OR spatial OR mapping){loc_q} when:14d'
        else:
            q_str = f'"{kw}"{loc_q} when:14d'

        params = urllib.parse.urlencode({"q": q_str, "hl": lang_code, "gl": gl})
        url = f"https://news.google.com/rss/search?{params}"
        try:
            xml_text = fetch_url(url)
            root = ET.fromstring(xml_text)
            for node in root.findall(".//item")[:5]:
                title = strip_html_tags(node.findtext("title") or "")
                link = (node.findtext("link") or "").strip()
                pub_date = (node.findtext("pubDate") or "").strip()
                if not link or not title:
                    continue
                norm_t = normalize_turkic(title)
                if "vietnam.vn" in norm_t or "esri vietnam" in norm_t:
                    continue
                if any(ex in norm_t for ex in rule.haric_kelimeler if ex):
                    continue
                if norm_dil in ("ingilizce", "english"):
                    if norm_konum in ("türkiye", "turkiye") and not re.search(r"turk|türk|istanbul|ankara|izmir|anatolia", norm_t):
                        continue
                    if norm_konum == "azerbaycan" and not re.search(r"azerbaijan|baku|karabakh|caucasus", norm_t):
                        continue
                    if norm_konum in ("özbekistan", "ozbekistan") and not re.search(r"uzbek|tashkent|karakalpak|samarkand|central asia", norm_t):
                        continue

                items.append(
                    FoundItem(
                        kelime=rule.kelime,
                        dil=rule.dil,
                        konum=rule.konum,
                        kaynak_tipi="Google Haber / Basın Duyurusu",
                        baslik=title,
                        ozet=f"Haber içeriğinde '{rule.kelime}' ifadesi tam eşleşme olarak geçmektedir: {title}",
                        url=link,
                        tarih=pub_date,
                    )
                )
        except Exception:
            pass

    # B) LinkedIn Uygulama İçi Doğrudan Yeni İş İlanları (TR / AZ / UZ)
    country_queries = [
        ("TÜRKİYE", "Turkey", '"CBS" OR "Coğrafi Bilgi Sistemleri" OR "GIS" OR "Geographic Information Systems"'),
        ("AZERBAYCAN", "Azerbaijan", '"GIS" OR "Geographic Information Systems" OR "CİS"'),
        ("ÖZBEKİSTAN", "Uzbekistan", '"GIS" OR "Geographic Information Systems" OR "GAT"'),
    ]
    for konum_label, loc_param, q_kw in country_queries:
        c_rules = [r for r in rules if r.konum == konum_label]
        if not c_rules:
            continue
        params = urllib.parse.urlencode({
            "keywords": q_kw,
            "location": loc_param,
            "f_TPR": "r604800",  # Son 7 gün
            "start": 0,
        })
        li_url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{params}"
        try:
            raw_html = fetch_url(li_url)
            cards = re.findall(r'<div class="base-card[^>]*>(.*?)</div>\s*</li>', raw_html, flags=re.DOTALL)
            for card in cards[:6]:
                title_m = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', card, flags=re.DOTALL)
                comp_m = re.search(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card, flags=re.DOTALL)
                link_m = re.search(r'href="(https://[^"?]+)', card)
                date_m = re.search(r'datetime="([^"]+)"', card)
                if not link_m:
                    continue
                link = link_m.group(1).strip()
                title = strip_html_tags(title_m.group(1) if title_m else "")
                comp = strip_html_tags(comp_m.group(1) if comp_m else "")
                date_str = date_m.group(1).strip() if date_m else ""

                job_id_m = re.search(r"-(\d+)$", link)
                if job_id_m:
                    job_id = job_id_m.group(1)
                    detail_html = fetch_url(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
                    desc_m = re.search(r'<div class="show-more-less-html__markup[^>]*>(.*?)</div>', detail_html, flags=re.DOTALL)
                    desc_text = strip_html_tags(desc_m.group(1) if desc_m else "")
                    for r in c_rules:
                        ok, matched_ctx = verify_content_against_rule(r, f"{title} ({comp})", desc_text, link)
                        if ok:
                            items.append(
                                FoundItem(
                                    kelime=r.kelime,
                                    dil=r.dil,
                                    konum=r.konum,
                                    kaynak_tipi="LinkedIn (Uygulama İçi Yeni İlan)",
                                    baslik=f"{title} — {comp}",
                                    ozet=matched_ctx,
                                    url=link,
                                    tarih=f"Yayın Tarihi: {date_str}",
                                )
                            )
                            break
        except Exception:
            pass

    return items


def gemini_filter_and_summarize_candidates(candidates: List[FoundItem]) -> List[FoundItem]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not candidates:
        return candidates

    to_verify = [c for c in candidates if not c.ozet.startswith("🤖")]
    already_ai = [c for c in candidates if c.ozet.startswith("🤖")]

    if not to_verify:
        return already_ai

    batch_input = []
    for idx, item in enumerate(to_verify[:25]):
        batch_input.append({
            "id": idx,
            "kelime": item.kelime,
            "konum": item.konum,
            "dil": item.dil,
            "baslik": item.baslik,
            "ozet": item.ozet,
            "url": item.url,
        })

    prompt = f"""Aşağıdaki CBS / GIS (Coğrafi Bilgi Sistemleri) aday internet sonuçlarını bir uzman olarak incele:
{json.dumps(batch_input, ensure_ascii=False, indent=2)}

Görevlerin:
1. İçerik gerçekten Coğrafi Bilgi Sistemleri (CBS/GIS/harita/mekansal veri) ile ilgiliyse "uygun": true yap; tamamen alakasızsa (örn: elektrik kesintisi listesi veya CBS TV kanalı vb.) "uygun": false yap.
2. Uygun olan her içerik için (İngilizce, Azerice veya Özbekçe olsa bile) 1-2 cümlelik çok net bir Türkçe yönetici özeti ("turkce_ajan_ozeti") yaz.
3. Yalnızca şu JSON formatında yanıt ver:
[
  {{"id": 0, "uygun": true, "turkce_ajan_ozeti": "🤖 Ajan Özeti: ..."}}
]"""

    try:
        resp_text, _ = call_gemini_api(prompt, use_google_search=False)
        json_match = re.search(r"\[.*\]", resp_text, flags=re.DOTALL)
        if json_match:
            decisions = {d["id"]: d for d in json.loads(json_match.group(0)) if isinstance(d, dict) and "id" in d}
            verified: List[FoundItem] = list(already_ai)
            for idx, item in enumerate(to_verify[:25]):
                dec = decisions.get(idx)
                if dec is None:
                    verified.append(item)
                elif dec.get("uygun", True):
                    if dec.get("turkce_ajan_ozeti"):
                        item.ozet = dec["turkce_ajan_ozeti"]
                    verified.append(item)
            return verified
    except Exception as exc:
        print(f"[AI AJAN ÖZETLEME UYARISI] {exc}")

    return candidates


def build_agent_email_html(items: List[FoundItem], rules_count: int, date_str: str, time_str: str, slot_label: str) -> str:
    by_location: Dict[str, List[FoundItem]] = {}
    for item in items:
        by_location.setdefault(item.konum, []).append(item)

    sections_html = ""
    if not items:
        sections_html = """
        <div style="padding:20px; background:#f8fafc; border-left:4px solid #6366f1; border-radius:6px; margin:16px 0;">
            <strong>🤖 Yapay Zeka Ajanı bu öğünde daha önce gönderilmemiş yeni bir içerik tespit etmedi.</strong>
            <p style="margin:6px 0 0; color:#475569; font-size:13px;">
                Bir sonraki öğünde (08:00 / 13:00 / 18:00) tarama otomatik olarak devam edecektir.
            </p>
        </div>
        """
    else:
        for konum, loc_items in by_location.items():
            sections_html += f"""
            <h2 style="margin-top:24px; padding-bottom:8px; border-bottom:2px solid #4f46e5; color:#0f172a; font-size:18px;">
                🌍 Konum: {html.escape(konum)} ({len(loc_items)} yeni sonuç)
            </h2>
            """
            for it in loc_items:
                sections_html += f"""
                <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; padding:14px; margin-bottom:12px;">
                    <div style="font-size:12px; color:#4338ca; font-weight:bold; margin-bottom:4px;">
                        🎯 Kural: <span style="background:#e0e7ff; padding:2px 6px; border-radius:4px;">{html.escape(it.kelime)}</span>
                        &nbsp;|&nbsp; 🗣️ Dil: {html.escape(it.dil)}
                        &nbsp;|&nbsp; 📡 Kaynak: <span style="background:#f1f5f9; padding:2px 6px; border-radius:4px; color:#334155;">{html.escape(it.kaynak_tipi)}</span>
                    </div>
                    <div style="font-size:15px; font-weight:600; margin:6px 0;">
                        <a href="{html.escape(it.url)}" style="color:#1d4ed8; text-decoration:none;" target="_blank">
                            {html.escape(it.baslik or "Başlıksız İçerik")}
                        </a>
                    </div>
                    <div style="font-size:13px; color:#1e293b; line-height:1.5; background:#eef2ff; padding:10px; border-radius:6px; border-left:3px solid #6366f1;">
                        {html.escape(it.ozet)}
                    </div>
                    <div style="font-size:11px; color:#64748b; margin-top:6px; word-break:break-all;">
                        🔗 <a href="{html.escape(it.url)}" style="color:#64748b;">{html.escape(it.url)}</a>
                        {f" | 🕒 {html.escape(it.tarih)}" if it.tarih else ""}
                    </div>
                </div>
                """

    return f"""<!DOCTYPE html>
<html lang="tr">
<head><meta charset="UTF-8"><title>🤖 CBS/GIS Yapay Zeka Ajanı Raporu</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f1f5f9; margin:0; padding:20px;">
    <div style="max-width:760px; margin:0 auto; background:#ffffff; border-radius:12px; padding:24px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
        <div style="background:linear-gradient(135deg,#1e1b4b,#312e81); color:#ffffff; padding:18px 20px; border-radius:8px;">
            <h1 style="margin:0; font-size:20px;">🤖 CBS / GIS Yapay Zeka Ajanı Mail Raporu</h1>
            <p style="margin:6px 0 0; font-size:13px; color:#c7d2fe;">
                📅 Gün: <strong>{date_str}</strong> &nbsp;|&nbsp;
                🕒 Öğün / Saat: <strong>{time_str} ({html.escape(slot_label)})</strong> &nbsp;|&nbsp;
                Yeni Sonuç: <strong>{len(items)}</strong>
            </p>
        </div>
        {sections_html}
        <hr style="border:none; border-top:1px solid #e2e8f0; margin:24px 0 12px;">
        <div style="font-size:12px; color:#64748b; text-align:center;">
            Bu rapor arka planda çalışan <strong>CBS/GIS Yapay Zeka Ajanı</strong> tarafından günde 3 kez (08:00, 13:00, 18:00 TSİ) otomatik oluşturulmaktadır.
        </div>
    </div>
</body>
</html>"""


def run_agent_scheduled_job(slot_label: str = "Otomatik Ajan Öğünü", csv_path: str = "rules.csv") -> None:
    dt = now_tr()
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M")

    hour = dt.hour
    if 6 <= hour < 11:
        ogun_etiketi = "08:00 Sabah Maili"
    elif 11 <= hour < 16:
        ogun_etiketi = "13:00 Öğle Maili"
    elif 16 <= hour <= 21:
        ogun_etiketi = "18:00 Akşam Maili"
    else:
        ogun_etiketi = slot_label

    rules = load_rules(csv_path)
    seen_data = load_json_file(AGENT_SEEN_FILE, {"urls": []})
    seen_urls: Set[str] = set(seen_data.get("urls", []))

    candidates: Dict[str, FoundItem] = {}

    # 1. Gemini Yapay Zeka Ajanı Canlı Google Araştırması
    print("[AI AJAN] Canlı Google araştırması başlatılıyor...")
    for item in gemini_autonomous_web_research(rules):
        if item.url and item.url not in seen_urls:
            candidates[item.url] = item

    # 2. Doğrudan Kaynak Taraması (Tam metin Google News + LinkedIn Uygulama İçi İlanlar)
    print(f"[AI AJAN] {len(rules)} kural için doğrudan kaynaklar taranıyor...")
    for item in collect_direct_fresh_sources(rules):
        u = item.url.strip()
        if u and u not in seen_urls and u not in candidates:
            candidates[u] = item

    print(f"[AI AJAN] Toplam {len(candidates)} aday içerik bulundu, yapay zeka süzgecinden geçiriliyor...")

    # 3. Yapay Zeka Ajanı ile Eleme ve Türkçe Yönetici Özeti Çıkarma
    raw_list = list(candidates.values())
    final_items = gemini_filter_and_summarize_candidates(raw_list)

    # 4. E-posta Gönderimi
    html_body = build_agent_email_html(final_items, len(rules), date_str, time_str, ogun_etiketi)
    with open("latest_report.html", "w", encoding="utf-8") as f:
        f.write(html_body)

    subject = f"🤖 [CBS/GIS Ajan Raporu] {date_str} {time_str} ({ogun_etiketi}) - {len(final_items)} Yeni Sonuç"
    sent = send_email_report(subject, html_body)

    # 5. Ajanın Gönderdiği Raporu Gün ve Saat Bazlı Arşive (agent_reports.json) Kaydet
    for it in final_items:
        seen_urls.add(it.url)
    save_json_file(AGENT_SEEN_FILE, {"updated_at": f"{date_str} {time_str}", "urls": list(seen_urls)[-5000:]})

    reports_data = load_json_file(AGENT_REPORTS_FILE, {"reports": []})
    reports_list = reports_data.get("reports", [])
    reports_list.insert(
        0,
        {
            "id": dt.strftime("%Y%m%d_%H%M%S"),
            "gun": date_str,
            "saat": time_str,
            "ogun": ogun_etiketi,
            "mail_gonderildi": sent,
            "toplam_sonuc": len(final_items),
            "sonuclar": [asdict(x) for x in final_items],
        },
    )
    reports_data["reports"] = reports_list[:45]
    save_json_file(AGENT_REPORTS_FILE, reports_data)
    print(f"[AI AJAN TAMAMLANDI] {date_str} {time_str} ({ogun_etiketi}) -> {len(final_items)} sonuç arşivlendi.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CBS/GIS Arka Plan Yapay Zeka Ajanı (08:00 - 13:00 - 18:00)")
    parser.add_argument("--slot", default="Otomatik Ajan Öğünü", help="Öğün etiketi")
    parser.add_argument("--rules", default="rules.csv", help="Kural dosyası")
    args = parser.parse_args()
    run_agent_scheduled_job(slot_label=args.slot, csv_path=args.rules)
