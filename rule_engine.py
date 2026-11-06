import csv
import io
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class SearchRule:
    aktif: bool
    kelime: str
    kural_tipi: str  # "TAM_ESLESTIRME" veya "KISALTMA_BAGLAMSAL"
    dil: str         # "TÜRKÇE", "İNGİLİZCE", "AZERİCE", "ÖZBEKÇE"
    konum: str       # "TÜRKİYE", "AZERBAYCAN", "ÖZBEKİSTAN"
    baglam_kelimeleri: List[str] = field(default_factory=list)
    haric_kelimeler: List[str] = field(default_factory=list)


COUNTRY_SETTINGS = {
    "TÜRKİYE": {
        "gl": "tr",
        "ddg_region": "tr-tr",
        "location_signals": [".tr/", ".tr", "türkiye", "turkiye", "turkey", "istanbul", "ankara", "izmir", "bursa", "antalya", "bakanlı", "belediye", "duyuru", "ilan"],
        "query_Location_hint": {
            "TÜRKÇE": "",
            "İNGİLİZCE": '(Turkey OR Türkiye OR Istanbul OR Ankara)',
        },
    },
    "AZERBAYCAN": {
        "gl": "az",
        "ddg_region": "wt-wt",
        "location_signals": [".az/", ".az", "azerbaijan", "azərbaycan", "azerbaycan", "baku", "bakı", "baki", "sumqayıt", "gəncə", "vakansiya"],
        "query_Location_hint": {
            "AZERİCE": "",
            "İNGİLİZCE": '(Azerbaijan OR Azərbaycan OR Baku)',
        },
    },
    "ÖZBEKİSTAN": {
        "gl": "uz",
        "ddg_region": "wt-wt",
        "location_signals": [".uz/", ".uz", "uzbekistan", "o'zbekiston", "ozbekiston", "tashkent", "toshkent", "samarqand"],
        "query_Location_hint": {
            "ÖZBEKÇE": "",
            "İNGİLİZCE": '(Uzbekistan OR "O\'zbekiston" OR Tashkent OR Toshkent)',
        },
    },
}

LANGUAGE_CODES = {
    "TÜRKÇE": "tr",
    "İNGİLİZCE": "en",
    "AZERİCE": "az",
    "ÖZBEKÇE": "uz",
}


def normalize_turkic(text: str) -> str:
    if not text:
        return ""
    text = text.replace("İ", "i").replace("I", "ı").lower()
    text = text.replace("ı", "i")
    text = text.replace("ʻ", "'").replace("’", "'").replace("‘", "'").replace("`", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_static_or_homepage_url(url: str) -> bool:
    """
    Kurumların statik ana sayfalarını (örn: cbs.tkgm.gov.tr/, cbs.ibb.gov.tr/),
    portal giriş ekranlarını, Wikipedia ve genel 'What is GIS' tanıtım sayfalarını reddeder.
    Yalnızca gerçek alt içerik/paylaşım/duyuru/ilan URL'lerini kabul eder.
    """
    if not url:
        return True
    u = url.lower().strip()
    if "news.google.com/rss/articles/" in u:
        return False

    blocked_patterns = [
        r"wikipedia\.org",
        r"wikidata\.org",
        r"britannica\.com",
        r"/what-is-gis",
        r"/overview/?$",
        r"/about/?$",
        r"/hakkimizda",
        r"/iletisim",
        r"/contact",
        r"/anasayfa/?$",
        r"/portal/anasayfa",
        r"/home/?$",
        r"/index\.(html|php|aspx|jsp)$",
        r"/default\.(aspx|html)$",
    ]
    for pat in blocked_patterns:
        if re.search(pat, u):
            return True

    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return True
        segments = [s for s in path.split("/") if s]
        if len(segments) == 1 and re.match(r"^(cbs|gis|tr|en|az|uz|web|portal|harita|map|kurumsal|login|giris)$", segments[0], re.I):
            return True
    except Exception:
        return True
    return False


def load_rules(csv_path: str = "rules.csv") -> List[SearchRule]:
    sheet_url = os.environ.get("GOOGLE_SHEET_CSV_URL", "").strip()
    content = None

    if sheet_url:
        try:
            req = urllib.request.Request(sheet_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8-sig")
        except Exception as exc:
            print(f"[UYARI] Google Sheet okunamadı ({exc}), yerel {csv_path} kullanılıyor.")

    if content is None:
        with open(csv_path, mode="r", encoding="utf-8-sig") as f:
            content = f.read()

    rules: List[SearchRule] = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        kelime = (row.get("kelime") or "").strip()
        if not kelime:
            continue
        aktif_str = (row.get("aktif") or "1").strip().lower()
        aktif = aktif_str in ("1", "true", "evet", "aktif", "yes")
        kural_tipi = (row.get("kural_tipi") or "TAM_ESLESTIRME").strip().upper()
        dil = (row.get("dil") or "TÜRKÇE").strip().upper()
        konum = (row.get("konum") or "TÜRKİYE").strip().upper()

        baglam_raw = (row.get("baglam_kelimeleri") or "").strip()
        baglam = [normalize_turkic(x) for x in baglam_raw.split("|") if x.strip()]

        haric_raw = (row.get("haric_kelimeler") or "").strip()
        haric = [normalize_turkic(x) for x in haric_raw.split("|") if x.strip()]

        rules.append(
            SearchRule(
                aktif=aktif,
                kelime=kelime,
                kural_tipi=kural_tipi,
                dil=dil,
                konum=konum,
                baglam_kelimeleri=baglam,
                haric_kelimeler=haric,
            )
        )
    return [r for r in rules if r.aktif]


def is_strictly_in_target_country(konum: str, dil: str, title: str, snippet: str = "", url: str = "") -> bool:
    """
    Özellikle İNGİLİZCE aramalarda (GIS, GEOGRAPHIC INFORMATION SYSTEMS vb.)
    ABD, Meksika, Avrupa gibi alakasız ülkelerden gelen sonuçları kesin olarak engeller.
    Yalnızca TÜRKİYE, AZERBAYCAN ve ÖZBEKİSTAN kaynaklı veya bu ülkelerle doğrudan ilgili
    sonuçlara izin verir.
    """
    combined = normalize_turkic(f"{title} {snippet} {url}")
    url_low = (url or "").lower()
    norm_konum = normalize_turkic(konum)
    norm_dil = normalize_turkic(dil)

    # ABD / yabancı belediye ve üniversite bültenlerini kesin olarak engelle (.gov olup .gov.tr/.gov.az/.gov.uz olmayanlar vb.)
    if re.search(r"\.gov(/|$)", url_low) and not re.search(r"\.gov\.(tr|az|uz)", url_low):
        return False

    if norm_konum in ("türkiye", "turkiye"):
        if norm_dil in ("türkçe", "turkce") and not any(bad in combined for bad in ("vietnam.vn", "esri vietnam")):
            return True
        return bool(
            re.search(
                r"(\b(turkey|türkiye|turkiye|turkish|istanbul|ankara|izmir|antalya|bursa|adana|konya|gaziantep|kayseri|trabzon|anatolia|anadolu)\b|tr\.linkedin\.com|\.(gov|edu|com|org|bel|pol|tsk)\.tr|\b[a-z0-9\-]+\.tr/)",
                combined,
            )
        )

    if norm_konum == "azerbaycan":
        if norm_dil in ("azerice", "azərice") and not any(bad in combined for bad in ("vietnam.vn", "esri vietnam")):
            return True
        return bool(
            re.search(
                r"(\b(azerbaijan|azərbaycan|azerbaycan|azeri|baku|bakı|baki|sumqayit|sumqayıt|ganja|gəncə|karabakh|qarabağ|shusha|şuşa|nakhchivan|naxçıvan)\b|az\.linkedin\.com|\.(gov|edu|com|org)\.az|\b[a-z0-9\-]+\.az/)",
                combined,
            )
        )

    if norm_konum in ("özbekistan", "ozbekistan"):
        if norm_dil in ("özbekçe", "ozbekce") and not any(bad in combined for bad in ("vietnam.vn", "esri vietnam")):
            return True
        return bool(
            re.search(
                r"(\b(uzbekistan|uzbek|o'zbekiston|ozbekiston|tashkent|toshkent|samarkand|samarqand|bukhara|buxoro|khiva|xiva|fergana|andijan|namangan|karakalpak|qoraqalpog|nukus)\b|uz\.linkedin\.com|\.(gov|edu|com|org)\.uz|\b[a-z0-9\-]+\.uz/)",
                combined,
            )
        )

    return False


def verify_content_against_rule(
    rule: SearchRule, title: str, snippet: str, url: str = ""
) -> Tuple[bool, str]:
    if is_static_or_homepage_url(url):
        return False, "Statik ana sayfa / portal girişi elendi"

    if not is_strictly_in_target_country(rule.konum, rule.dil, title, snippet, url):
        return False, f"Hedef ülke ({rule.konum}) dışında içerik elendi"

    combined_raw = f"{title} — {snippet}"
    norm_text = normalize_turkic(combined_raw)
    norm_url = normalize_turkic(url)
    norm_kw = normalize_turkic(rule.kelime)

    for excluded in rule.haric_kelimeler:
        if excluded and excluded in norm_text:
            return False, f"Hariç tutulan ifade bulundu: {excluded}"

    if rule.kural_tipi == "TAM_ESLESTIRME":
        words = [re.escape(w) for w in norm_kw.split() if w]
        phrase_pattern = r"(?<![a-z0-9əöüçğş])" + r"[\s\-]+".join(words) + r"(?:['’]?[a-zəöüçğş]{0,6})?(?![a-z0-9əöüçğş])"
        match = re.search(phrase_pattern, norm_text)
        if not match:
            return False, "Kelimeler yan yana tam ifade olarak geçmiyor"
        return True, highlight_match(combined_raw, match.start(), match.end())

    acronym_pattern = r"(?<![a-z0-9əöüçğş])" + re.escape(norm_kw) + r"(?:['’][a-zəöüçğş]+)?(?![a-z0-9əöüçğş])"
    match = re.search(acronym_pattern, norm_text)
    if not match:
        return False, f"'{rule.kelime}' tek başına kelime olarak geçmiyor"

    if norm_kw == "cbs":
        bad_domains = ["cbsnews.com", "cbssports.com", "cbs.com", "paramountplus.com", "cbslocal.com"]
        if any(d in norm_url for d in bad_domains):
            return False, "ABD CBS medya kanalı kaynağı elendi"
        has_context = any(ctx in norm_text or ctx in norm_url for ctx in rule.baglam_kelimeleri)
        if not has_context:
            return False, "Coğrafi Bilgi Sistemleri bağlam kelimesi bulunamadı"
        return True, highlight_match(combined_raw, match.start(), match.end())

    if norm_kw == "gis":
        has_context = any(ctx in norm_text or ctx in norm_url for ctx in rule.baglam_kelimeleri)
        if not has_context:
            return False, "Geographic Information Systems bağlam kelimesi bulunamadı"
        return True, highlight_match(combined_raw, match.start(), match.end())

    return True, highlight_match(combined_raw, match.start(), match.end())


def highlight_match(text: str, start: int, end: int, window: int = 90) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{text[left:right].strip()}{suffix}"


def build_search_query(rule: SearchRule, platform: str = "web") -> str:
    kw = rule.kelime.strip()
    country_cfg = COUNTRY_SETTINGS.get(rule.konum, COUNTRY_SETTINGS["TÜRKİYE"])
    loc_hint = country_cfg["query_Location_hint"].get(rule.dil, "")

    if rule.kural_tipi == "TAM_ESLESTIRME":
        base_q = f'"{kw}"'
    elif kw.upper() == "CBS":
        base_q = '"CBS" ("Coğrafi Bilgi" OR "CBS Uzmanı" OR duyuru OR ihale OR ilan OR proje) -news -sports'
    elif kw.upper() == "GIS":
        base_q = '"GIS" ("Geographic Information" OR specialist OR engineer OR analyst OR project)'
    else:
        base_q = f'"{kw}"'

    if loc_hint and platform in ("web", "news", "linkedin", "x", "facebook", "instagram"):
        return f"{base_q} {loc_hint}".strip()
    return base_q
