import csv
import sys

RULES_FILE = "rules.csv"
FIELDNAMES = ["aktif", "kelime", "kural_tipi", "dil", "konum", "baglam_kelimeleri", "haric_kelimeler"]


def read_all():
    with open(RULES_FILE, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_all(rows):
    with open(RULES_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def list_rules():
    rows = read_all()
    print(f"\n{'NO':<4} {'DURUM':<7} {'KELİME / CÜMLE':<34} {'KURAL TİPİ':<20} {'DİL':<10} {'KONUM'}")
    print("-" * 92)
    for i, r in enumerate(rows, start=1):
        status = "AKTİF" if r.get("aktif", "1") == "1" else "PASİF"
        print(f"{i:<4} {status:<7} {r['kelime']:<34} {r['kural_tipi']:<20} {r['dil']:<10} {r['konum']}")
    print()


def add_rule(kelime: str, dil: str, konum: str, kural_tipi: str = "TAM_ESLESTIRME", baglam: str = "", haric: str = ""):
    rows = read_all()
    rows.append({
        "aktif": "1",
        "kelime": kelime.strip(),
        "kural_tipi": kural_tipi.strip().upper(),
        "dil": dil.strip().upper(),
        "konum": konum.strip().upper(),
        "baglam_kelimeleri": baglam.strip(),
        "haric_kelimeler": haric.strip(),
    })
    write_all(rows)
    print(f"[EKLENDİ] '{kelime}' ({dil} - {konum}) kurallara eklendi.")


def remove_rule(index_1based: int):
    rows = read_all()
    if index_1based < 1 or index_1based > len(rows):
        print(f"[HATA] Geçersiz satır numarası: {index_1based}")
        return
    removed = rows.pop(index_1based - 1)
    write_all(rows)
    print(f"[SİLİNDİ] #{index_1based}: '{removed['kelime']}' ({removed['dil']} - {removed['konum']}) listeden çıkarıldı.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        list_rules()
    elif sys.argv[1] == "add" and len(sys.argv) >= 5:
        kural_t = sys.argv[5] if len(sys.argv) >= 6 else "TAM_ESLESTIRME"
        add_rule(sys.argv[2], sys.argv[3], sys.argv[4], kural_t)
    elif sys.argv[1] == "remove" and len(sys.argv) >= 3:
        remove_rule(int(sys.argv[2]))
    else:
        print("Kullanım:")
        print("  Listele : python manage_rules.py list")
        print('  Ekle    : python manage_rules.py add "KELİME VEYA CÜMLE" "TÜRKÇE" "TÜRKİYE" [TAM_ESLESTIRME|KISALTMA_BAGLAMSAL]')
        print("  Çıkar   : python manage_rules.py remove <SATIR_NO>")
