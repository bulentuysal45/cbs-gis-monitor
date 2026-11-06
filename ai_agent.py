import argparse
import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Dict, List, Set

from main import send_email_report
from rule_engine import SearchRule, is_static_or_homepage_url, load_rules
from search_collectors import FoundItem, collect_for_rule

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


def call_gemini_api(prompt: str, use_google_search: bool = False) -> str:
    """
    Google Gemini API (gemini-2.5-flash) çağrısı yapar.
    use_google_search=True olduğunda Gemini'nin yerleşik Canlı Google Arama (Grounding) aracını kullanır.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return ""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if use_google_search:
        payload["tools"] = [{"google_search": {}}]

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        parts = raw.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text_out = "\n".join(p.get("text", "") for p in parts if "text" in p)
        return text_out.strip()


def gemini_autonomous_web_research(rules: List[SearchRule]) -> List[FoundItem]:
    """
    Gemini Yapay Zeka Ajanının Canlı Google Arama (Google Search Grounding) yeteneğini kullanarak
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
        kw_descriptions = "\n".join(
            f"- Kelime: \"{r.kelime}\" | Dil: {r.dil} | Kural: {r.kural_tipi}"
            for r in c_rules
        )
        prompt = f"""Sen uzman bir Coğrafi Bilgi Sistemleri (CBS / GIS) internet araştırma ajanısın.
Bugünün tarihi: {today_str}. Hedef ülke/konum: {konum}.

Aşağıdaki anahtar kelimeler ve kurallar için Google'da canlı arama yaparak SON GÜNLERDE yayınlanmış YENİ içerikleri (iş ilanları, kurum/belediye/bakanlık duyuruları, Resmi Gazete/mevzuat haberleri, ihaleler, LinkedIn/X/Reddit/sosyal medya paylaşımları, sektörel haberler) bul:
{kw_descriptions}

KESİN KURALLAR:
1. Kurumların yıllardır sabit duran kök ana sayfalarını (örn: cbs.tkgm.gov.tr/, cbs.ibb.gov.tr/, wikipedia.org, esri.com/what-is-gis) KESİNLİKLE DAHİL ETME. Sadece spesifik yeni haber, duyuru, ilan veya gönderi alt sayfalarını al.
2. TAM_ESLESTIRME olan kelimelerde tüm kelimeler metinde yan yana geçmelidir.
3. Tek başına 'CBS' veya 'GIS' kısaltmalarında yalnızca Coğrafi Bilgi Sistemleri / Geographic Information Systems anlamında olanları al (CBS News vb. hariç).
4. Sonuçları SADECE aşağıdaki JSON dizisi formatında döndür (başka açıklama yazma):
[
  {{
    "kelime": "Eşleşen kural kelimesi",
    "dil": "TÜRKÇE veya İNGİLİZCE veya AZERİCE veya ÖZBEKÇE",
    "kaynak_tipi": "🤖 AI Ajan (Google Web / Sosyal Medya / İlan)",
    "baslik": "İçeriğin başlığı",
    "ozet": "İçeriğin Türkçe 1-2 cümlelik net özeti ve neden önemli olduğu",
    "url": "https://...",
    "tarih": "Yayın tarihi"
  }}
]"""
        try:
            resp_text = call_gemini_api(prompt, use_google_search=True)
            json_match = re.search(r"\[.*\]", resp_text, flags=re.DOTALL)
            if json_match:
                arr = json.loads(json_match.group(0))
                for obj in arr:
                    u = (obj.get("url") or "").strip()
                    if not u or is_static_or_homepage_url(u):
                        continue
                    discovered.append(
                        FoundItem(
                            kelime=obj.get("kelime") or c_rules[0].kelime,
                            dil=obj.get("dil") or c_rules[0].dil,
                            konum=konum,
                            kaynak_tipi=obj.get("kaynak_tipi") or "🤖 AI Ajan Araştırması",
                            baslik=obj.get("baslik") or "Yeni İçerik",
                            ozet=obj.get("ozet") or "",
                            url=u,
                            tarih=obj.get("tarih") or today_str,
                        )
                    )
        except Exception as exc:
            print(f"[AI AJAN ARAMA UYARISI - {konum}] {exc}")

    return discovered


def gemini_filter_and_summarize_candidates(candidates: List[FoundItem]) -> List[FoundItem]:
    """
    Toplanan aday içerikleri Gemini Yapay Zeka Ajanına inceletir:
    - Alakasız veya sabit/eski ana sayfa olanları eler,
    - Kalan gerçek sonuçların özetini anlaşılır Türkçe yönetici özeti haline getirir.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not candidates:
        return candidates

    # Zaten AI Ajan tarafından özetlenmiş olanları ayır, diğerlerini topluca doğrulat/özetlet
    to_verify = [c for c in candidates if not c.kaynak_tipi.startswith("🤖")]
    already_ai = [c for c in candidates if c.kaynak_tipi.startswith("🤖")]

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
1. İçerik gerçekten Coğrafi Bilgi Sistemleri (CBS/GIS) ile ilgiliyse, hedef konum ({batch_input[0]['konum']} vb.) ile uyumluysa ve sabit bir kurum ana sayfası değilse "uygun": true yap; aksi halde "uygun": false yap.
2. Uygun olan her içerik için (İngilizce, Azerice veya Özbekçe olsa bile) 1-2 cümlelik çok net bir Türkçe yönetici özeti ("turkce_ajan_ozeti") yaz.
3. Yalnızca şu JSON formatında yanıt ver:
[
  {{"id": 0, "uygun": true, "turkce_ajan_ozeti": "🤖 Ajan Özeti: ..."}}
]"""

    try:
        resp_text = call_gemini_api(prompt, use_google_search=False)
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

    # Öğün saatini netleştir (08:00 / 13:00 / 18:00)
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

    # 2. Doğrudan Kaynak Taraması (Google News, LinkedIn, Reddit, DuckDuckGo)
    print(f"[AI AJAN] {len(rules)} kural için doğrudan kaynaklar taranıyor...")
    for rule in rules:
        for item in collect_for_rule(rule):
            u = item.url.strip()
            if u and u not in seen_urls and u not in candidates:
                candidates[u] = item

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
    # Son 45 öğün raporunu (15 gün x 3 öğün) sakla
    reports_data["reports"] = reports_list[:45]
    save_json_file(AGENT_REPORTS_FILE, reports_data)
    print(f"[AI AJAN TAMAMLANDI] {date_str} {time_str} ({ogun_etiketi}) -> {len(final_items)} sonuç arşivlendi.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CBS/GIS Arka Plan Yapay Zeka Ajanı (08:00 - 13:00 - 18:00)")
    parser.add_argument("--slot", default="Otomatik Ajan Öğünü", help="Öğün etiketi")
    parser.add_argument("--rules", default="rules.csv", help="Kural dosyası")
    args = parser.parse_args()
    run_agent_scheduled_job(slot_label=args.slot, csv_path=args.rules)
