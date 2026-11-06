# 🛰️ CBS / GIS İnternet & Sosyal Medya Takip Otomasyonu

Bu proje; **Türkiye, Azerbaycan ve Özbekistan** hedef bölgelerinde, belirlenen **CBS / GIS** anahtar kelimelerini ve cümle kalıplarını kurallara uygun şekilde tarayıp her gün **08:00, 13:00 ve 18:00 (TSİ)** saatlerinde bilgisayarınız kapalıyken bile e-posta raporu gönderen bulut tabanlı otomasyon sistemidir.

---

## 📌 Özellikler

1. **Tablonuzdaki 18 Kural Hazır Tanımlı (`rules.csv`):**
   - **Tam Eşleştirme (`TAM_ESLESTIRME`):** `"CBS UZMANI"`, `"COĞRAFİ BİLGİ SİSTEMLERİ"`, `"CİS MÜTƏXƏSSİSİ"`, `"GEOGRAFIK AXBOROT TIZIMLARI"` gibi çok kelimeli ifadelerde kelimelerin metin içinde rastgele dağılmış olmasını reddeder; **yalnızca yan yana (bitişik ifade olarak)** geçtiği içerikleri kabul eder.
   - **Bağlamsal Kısaltma Filtresi (`KISALTMA_BAGLAMSAL`):** Tek başına `CBS` veya `GIS` aramalarında *CBS News, CBS Sports, Paramount, TV* gibi alakasız sonuçları otomatik eler; yalnızca Coğrafi Bilgi Sistemleri / Geographic Information Systems bağlamındaki sonuçları geçirir.
2. **Google İndeksi Dışı & Sosyal Medya Son Gönderiler:**
   - **Reddit Uygulama İçi Arama (`sort=new`):** Google indeksini beklemeden doğrudan Reddit'in kendi iç arama motorundaki en yeni gönderileri çeker.
   - **Çoklu Arama Motoru & Haber Akışları:** Bing Bağımsız İndeksi, DuckDuckGo ve yerel RSS haber akışlarını paralel tarar.
   - **Sosyal Medya Hedefli Tarama:** **X (Twitter), LinkedIn, Instagram ve Facebook** üzerindeki son paylaşımları platform bazlı sorgularla yakalar.
3. **Tekrar Gönderim Engelleme (`seen_urls.json`):**
   - Sabah 08:00 mailinde gönderilen bir içerik hafızaya kaydedilir; 13:00 veya 18:00 mailinde size tekrar gönderilmez.
4. **Kolay Kelime Ekleme / Çıkarma:**
   - Dilerseniz doğrudan `rules.csv` dosyasını düzenleyerek,
   - Dilerseniz **Google E-Tablolar (Google Sheets)** bağlantısı vererek (tablonuza satır eklediğiniz anda sistem otomatik algılar),
   - Dilerseniz `python manage_rules.py` komutuyla saniyeler içinde kelime ekleyip çıkarabilirsiniz.

---

## ☁️ Bilgisayar Kapalıyken Çalışması İçin Kurulum (GitHub Actions - Ücretsiz)

Bilgisayarınız kapalıyken günde 3 kez (**08:00, 13:00, 18:00**) mail gelmesi için `.github/workflows/cbs_gis_monitor.yml` dosyası hazırdır:

1. Bu klasörü ücretsiz bir **GitHub** deposuna (repository) yükleyin.
2. GitHub deponuzda **Settings -> Secrets and variables -> Actions -> New repository secret** bölümüne girip şu bilgileri ekleyin:
   - `SMTP_USER`: Gönderici e-posta adresiniz (örn. `sizinadresiniz@gmail.com`)
   - `SMTP_PASS`: Gmail **Uygulama Şifreniz** (Google Hesabı -> Güvenlik -> 2 Adımlı Doğrulama -> Uygulama Şifreleri kısmından alınan 16 haneli şifre)
   - `REPORT_TO_EMAIL`: Raporun geleceği e-posta adresi (birden fazla adres için virgülle ayırabilirsiniz)
   - *(Opsiyonel)* `GOOGLE_SHEET_CSV_URL`: Kelimeleri Google E-Tablolar üzerinden yönetmek isterseniz tablonuzun CSV dışa aktarma linki.
3. **Actions** sekmesinden iş akışını etkinleştirin. Sistem her gün **08:00, 13:00 ve 18:00**'de otomatik çalışıp raporunuzu mail atacaktır.
