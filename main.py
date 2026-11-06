import argparse
import datetime
import html
import json
import os
import smtplib
from dataclasses import asdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Set, Tuple

from rule_engine import SearchRule, load_rules
from search_collectors import FoundItem, collect_for_rule

STATE_FILE = "seen_urls.json"


def now_tr_str() -> str:
    now_tr = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    return now_tr.strftime("%d.%m.%Y %H:%M")


def load_state(filepath: str = STATE_FILE) -> dict:
    default_state = {
        "last_email_at": "Henüz gönderilmedi",
        "last_query_at": "Henüz sorgulanmadı",
        "emailed_urls": [],
        "pending_items": [],
        "history_reports": [],
    }
    if not os.path.exists(filepath):
        return default_state
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Eski format uyumluluğu
            if "urls" in data and "emailed_urls" not in data:
                default_state["emailed_urls"] = data["urls"]
                return default_state
            for k, v in default_state.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return default_state


def save_state(state: dict, filepath: str = STATE_FILE, max_keep: int = 5000) -> None:
    state["emailed_urls"] = list(dict.fromkeys(state.get("emailed_urls", [])))[-max_keep:]
    state["history_reports"] = state.get("history_reports", [])[-30:]
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def scan_since_last_email(
    csv_path: str = "rules.csv",
    limit_rules: int = 0,
    selected_konum: str = "",
    selected_kelime: str = "",
) -> Tuple[List[FoundItem], dict]:
    """
    Son gönderilen mailden itibaren çıkan tüm yeni içerikleri tarar.
    Sonuçları 'pending_items' içine kaydeder (böylece web arayüzünden tekrar tekrar
    incelenebilir ve bir sonraki 08:00 / 13:00 / 18:00 mailinde eksiksiz gönderilir).
    """
    rules = load_rules(csv_path)
    if selected_konum:
        rules = [r for r in rules if r.konum.upper() == selected_konum.upper()]
    if selected_kelime:
        rules = [r for r in rules if r.kelime.upper() == selected_kelime.upper()]
    if limit_rules > 0:
        rules = rules[:limit_rules]

    state = load_state()
    emailed_set: Set[str] = set(state.get("emailed_urls", []))

    # Mevcut bekleyen (son mailden beri birikmiş) sonuçları koru
    pending_by_url: Dict[str, dict] = {
        item["url"]: item
        for item in state.get("pending_items", [])
        if item.get("url") and item["url"] not in emailed_set
    }

    print(f"[TARAMA] Toplam {len(rules)} aktif kural son mailden itibaren taranıyor...")
    for idx, rule in enumerate(rules, start=1):
        print(f"  [{idx}/{len(rules)}] '{rule.kelime}' ({rule.dil} - {rule.konum})")
        found = collect_for_rule(rule)
        for item in found:
            norm_u = item.url.strip()
            if not norm_u or norm_u in emailed_set:
                continue
            if norm_u not in pending_by_url:
                item_dict = asdict(item)
                if not item_dict.get("tarih"):
                    item_dict["tarih"] = now_tr_str()
                pending_by_url[norm_u] = item_dict

    pending_list_dicts = list(pending_by_url.values())
    state["pending_items"] = pending_list_dicts
    state["last_query_at"] = now_tr_str()
    save_state(state)

    pending_objs = [FoundItem(**d) for d in pending_list_dicts]
    return pending_objs, state


def finalize_and_send_email(slot_label: str = "Planlı Rapor", csv_path: str = "rules.csv") -> Tuple[bool, int]:
    """
    Son mailden itibaren biriken ve yeni taranan sonuçları e-posta ile gönderir,
    ardından bu linkleri 'emailed_urls' listesine aktararak bir sonraki öğün için sıfırlar.
    """
    rules = load_rules(csv_path)
    items, state = scan_since_last_email(csv_path=csv_path)

    html_report = build_html_report(items, rules, slot_label, state.get("last_email_at", "-"))
    with open("latest_report.html", "w", encoding="utf-8") as f:
        f.write(html_report)

    subject = f"[CBS/GIS Raporu] {len(items)} Yeni İçerik ({now_tr_str()} - {slot_label})"
    sent = send_email_report(subject, html_report)

    # E-posta gönderildiyse (veya bulut/test çalışması tamamlandıysa) bekleyenleri gönderilmiş olarak işaretle
    if sent or os.environ.get("MARK_SEEN_WITHOUT_SMTP", "0") == "1":
        emailed_urls = state.get("emailed_urls", [])
        for it in items:
            emailed_urls.append(it.url)
        state["emailed_urls"] = emailed_urls
        state["last_email_at"] = now_tr_str()
        state["history_reports"].append({
            "tarih": state["last_email_at"],
            "slot": slot_label,
            "adet": len(items),
            "ornek_basliklar": [i.baslik for i in items[:5]],
        })
        state["pending_items"] = []
        save_state(state)

    return sent, len(items)


def build_html_report(
    items: List[FoundItem],
    rules: List[SearchRule],
    run_slot: str,
    last_email_at: str = "-",
) -> str:
    date_str = now_tr_str()
    by_location: Dict[str, List[FoundItem]] = {}
    for item in items:
        by_location.setdefault(item.konum, []).append(item)

    sections_html = ""
    if not items:
        sections_html = """
        <div style="padding:20px; background:#f8fafc; border-left:4px solid #64748b; border-radius:6px; margin:16px 0;">
            <strong>Son mailden itibaren kurallara uyan yeni bir içerik bulunmadı.</strong>
            <p style="margin:6px 0 0; color:#475569; font-size:13px;">
                Sistem aktif olarak tüm kaynakları (Web, Haberler, Reddit, X, LinkedIn, Instagram, Facebook) taramaya devam ediyor.
            </p>
        </div>
        """
    else:
        for konum, loc_items in by_location.items():
            sections_html += f"""
            <h2 style="margin-top:24px; padding-bottom:8px; border-bottom:2px solid #0284c7; color:#0f172a; font-size:18px;">
                🌍 Konum: {html.escape(konum)} ({len(loc_items)} yeni sonuç)
            </h2>
            """
            for it in loc_items:
                sections_html += f"""
                <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; padding:14px; margin-bottom:12px;">
                    <div style="font-size:12px; color:#0369a1; font-weight:bold; margin-bottom:4px;">
                        🎯 Kelime: <span style="background:#e0f2fe; padding:2px 6px; border-radius:4px;">{html.escape(it.kelime)}</span>
                        &nbsp;|&nbsp; 🗣️ Dil: {html.escape(it.dil)}
                        &nbsp;|&nbsp; 📡 Kaynak: <span style="background:#f1f5f9; padding:2px 6px; border-radius:4px; color:#334155;">{html.escape(it.kaynak_tipi)}</span>
                    </div>
                    <div style="font-size:15px; font-weight:600; margin:6px 0;">
                        <a href="{html.escape(it.url)}" style="color:#1d4ed8; text-decoration:none;" target="_blank">
                            {html.escape(it.baslik or "Başlıksız İçerik")}
                        </a>
                    </div>
                    <div style="font-size:13px; color:#334155; line-height:1.5; background:#f8fafc; padding:8px; border-radius:4px;">
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
<head>
<meta charset="UTF-8">
<title>CBS / GIS İnternet & Sosyal Medya Takip Raporu</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f1f5f9; margin:0; padding:20px;">
    <div style="max-width:760px; margin:0 auto; background:#ffffff; border-radius:12px; padding:24px; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
        <div style="background:#0f172a; color:#ffffff; padding:18px 20px; border-radius:8px;">
            <h1 style="margin:0; font-size:20px;">🛰️ CBS / GIS İnternet & Sosyal Medya Takip Raporu</h1>
            <p style="margin:6px 0 0; font-size:13px; color:#cbd5e1;">
                Rapor Saati (TSİ): <strong>{date_str}</strong> ({html.escape(run_slot)}) &nbsp;|&nbsp;
                Son Mail: <strong>{html.escape(last_email_at)}</strong> &nbsp;|&nbsp;
                Yeni Eşleşme: <strong>{len(items)}</strong>
            </p>
        </div>
        {sections_html}
        <hr style="border:none; border-top:1px solid #e2e8f0; margin:24px 0 12px;">
        <div style="font-size:12px; color:#64748b; text-align:center;">
            Bu rapor CBS/GIS Otomasyon Sistemi tarafından günde 3 kez (08:00, 13:00, 18:00 TSİ) otomatik oluşturulmaktadır.
        </div>
    </div>
</body>
</html>"""


def send_email_report(subject: str, html_body: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    to_emails_raw = os.environ.get("REPORT_TO_EMAIL", smtp_user).strip()

    if not smtp_user or not smtp_pass or not to_emails_raw:
        print("[BİLGİ] SMTP bilgileri (SMTP_USER / SMTP_PASS / REPORT_TO_EMAIL) henüz tanımlanmadı.")
        print("[BİLGİ] Rapor yerel olarak 'latest_report.html' dosyasına kaydedildi.")
        return False

    recipients = [e.strip() for e in to_emails_raw.split(",") if e.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"[BAŞARILI] Rapor e-postası gönderildi -> {', '.join(recipients)}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="CBS / GIS İnternet ve Sosyal Medya Takip Otomasyonu")
    parser.add_argument("--rules", default="rules.csv", help="Kural CSV dosyası yolu")
    parser.add_argument("--limit-rules", type=int, default=0, help="Test amaçlı sadece ilk N kuralı çalıştır")
    parser.add_argument("--slot", default="Günlük Tarama (08:00 / 13:00 / 18:00)", help="Tarama periyodu etiketi")
    parser.add_argument("--preview-only", action="store_true", help="Mail göndermeden sadece son mailden itibaren çıkanları sorgula")
    args = parser.parse_args()

    if args.preview_only:
        items, state = scan_since_last_email(csv_path=args.rules, limit_rules=args.limit_rules)
        print(f"[ÖNİZLEME TAMAMLANDI] Son mailden ({state.get('last_email_at')}) itibaren {len(items)} yeni sonuç mevcut.")
    else:
        finalize_and_send_email(slot_label=args.slot, csv_path=args.rules)


if __name__ == "__main__":
    main()
