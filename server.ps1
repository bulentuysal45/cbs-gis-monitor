$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$WorkDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $WorkDir

$RulesPath = Join-Path $WorkDir "rules.csv"
$StatePath = Join-Path $WorkDir "seen_urls.json"
$EmailCfgPath = Join-Path $WorkDir "email_config.json"
$HtmlPath = Join-Path $WorkDir "templates\index.html"

function Normalize-Turkic([string]$t) {
    if ([string]::IsNullOrWhiteSpace($t)) { return "" }
    $s = $t.Replace([char]0x0130, [char]'i').Replace([char]'I', [char]'i').ToLowerInvariant()
    $s = $s.Replace([char]0x0131, [char]'i').Replace([char]0x02BB, [char]"'").Replace([char]0x2019, [char]"'").Replace([char]0x2018, [char]"'")
    $s = [regex]::Replace($s, "\s+", " ").Trim()
    return $s
}

function Strip-Html([string]$h) {
    if ([string]::IsNullOrWhiteSpace($h)) { return "" }
    $s = [regex]::Replace($h, "<[^>]+>", " ")
    $s = [System.Net.WebUtility]::HtmlDecode($s)
    return [regex]::Replace($s, "\s+", " ").Trim()
}

function Is-StaticOrHomepageUrl([string]$url) {
    if ([string]::IsNullOrWhiteSpace($url)) { return $true }
    $u = $url.ToLowerInvariant().Trim()
    if ($u.Contains("news.google.com/rss/articles/")) { return $false }

    $blockedPatterns = @(
        "wikipedia\.org",
        "wikidata\.org",
        "britannica\.com",
        "vietnam\.vn",
        "/what-is-gis",
        "/overview/?$",
        "/about/?$",
        "/hakkimizda",
        "/iletisim",
        "/contact",
        "/anasayfa/?$",
        "/portal/anasayfa",
        "/home/?$",
        "/index\.(html|php|aspx|jsp)$",
        "/default\.(aspx|html)$"
    )
    foreach ($pat in $blockedPatterns) {
        if ($u -match $pat) { return $true }
    }

    try {
        $uri = [uri]$url
        $path = $uri.AbsolutePath.Trim('/')
        if ([string]::IsNullOrWhiteSpace($path)) { return $true }
        $segments = @($path -split '/' | Where-Object { $_ -ne "" })
        if ($segments.Count -eq 1 -and $segments[0] -match "^(cbs|gis|tr|en|az|uz|web|portal|harita|map|kurumsal|login|giris)$") {
            return $true
        }
    } catch {
        return $true
    }
    return $false
}

function Verify-FullTextMatch($rule, [string]$fullText, [string]$url) {
    if (Is-StaticOrHomepageUrl $url) { return $null }

    $normText = Normalize-Turkic $fullText
    $normUrl  = Normalize-Turkic $url
    $normKw   = Normalize-Turkic $rule.kelime

    if ($normText -match "vietnam\.vn|esri vietnam") { return $null }

    if (![string]::IsNullOrWhiteSpace($rule.haric_kelimeler)) {
        foreach ($ex in ($rule.haric_kelimeler -split "\|")) {
            $exNorm = Normalize-Turkic $ex
            if ($exNorm -and $normText.Contains($exNorm)) { return $null }
        }
    }

    if ($rule.kural_tipi -eq "TAM_ESLESTIRME") {
        $words = ($normKw -split "\s+") | ForEach-Object { [regex]::Escape($_) }
        $pattern = "(?<![\w])" + ($words -join "[\s\-]+")
        $m = [regex]::Match($normText, $pattern)
        if (!$m.Success) { return $null }
        $start = [Math]::Max(0, $m.Index - 80)
        $len   = [Math]::Min($fullText.Length - $start, 240)
        return "..." + $fullText.Substring($start, $len).Trim() + "..."
    } else {
        $pattern = "(?<![\w])" + [regex]::Escape($normKw) + "(?![\w])"
        $m = [regex]::Match($normText, $pattern)
        if (!$m.Success) { return $null }
        if ($normKw -eq "cbs") {
            foreach ($bad in @("cbsnews.com","cbssports.com","cbs.com","paramount","cbslocal")) {
                if ($normUrl.Contains($bad)) { return $null }
            }
        }
        $start = [Math]::Max(0, $m.Index - 80)
        $len   = [Math]::Min($fullText.Length - $start, 240)
        return "..." + $fullText.Substring($start, $len).Trim() + "..."
    }
}

function Search-GoogleNewsByRule($rule, [int]$days = 3) {
    $results = @()
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Encoding = [System.Text.Encoding]::UTF8
        $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")

        $kw = $rule.kelime
        $normKonum = Normalize-Turkic $rule.konum
        $normDil   = Normalize-Turkic $rule.dil

        $hl = "tr"; $gl = "TR"; $locQuery = ""
        if ($normKonum -eq "turkiye") {
            if ($normDil -eq "ingilizce") { $hl = "en"; $gl = "US"; $locQuery = " (Turkey OR Turkiye OR Istanbul OR Ankara)" }
            else { $hl = "tr"; $gl = "TR" }
        }
        elseif ($normKonum -eq "azerbaycan") {
            $hl = "en"; $gl = "US"
            if ($normDil -eq "ingilizce") { $locQuery = " (Azerbaijan OR Baku)" }
        }
        elseif ($normKonum -eq "ozbekistan") {
            $hl = "en"; $gl = "US"
            if ($normDil -eq "ingilizce") { $locQuery = " (Uzbekistan OR Tashkent OR Karakalpakstan)" }
        }

        if ($kw -eq "CBS") {
            $q = "`"CBS`" (`"Cografi Bilgi Sistemleri`" OR `"CBS Uzmani`" OR belediye OR bakanlik OR harita) -`"CBS News`" -`"CBS Sports`" when:${days}d"
        }
        elseif ($kw -eq "GIS") {
            $q = "`"GIS`" (`"Geographic Information`" OR ArcGIS OR spatial OR mapping)$locQuery when:${days}d"
        }
        else {
            $q = "`"$kw`"$locQuery when:${days}d"
        }

        $rssUrl = "https://news.google.com/rss/search?q=" + [uri]::EscapeDataString($q) + "&hl=$hl&gl=$gl"
        $xmlStr = $wc.DownloadString($rssUrl)
        [xml]$xml = $xmlStr
        $cutoff = (Get-Date).AddDays(-($days + 1))

        foreach ($item in $xml.rss.channel.item) {
            if ($null -eq $item) { continue }
            $title = Strip-Html ([string]$item.title)
            $link  = ([string]$item.link).Trim()
            $pubRaw = ([string]$item.pubDate).Trim()
            if ([string]::IsNullOrWhiteSpace($title) -or [string]::IsNullOrWhiteSpace($link)) { continue }

            $isRecent = $true
            try {
                $dt = [datetime]::Parse($pubRaw)
                if ($dt -lt $cutoff) { $isRecent = $false }
            } catch {}
            if (!$isRecent) { continue }

            $normTitle = Normalize-Turkic $title
            if ($normTitle -match "vietnam\.vn|esri vietnam") { continue }

            $blocked = $false
            if (![string]::IsNullOrWhiteSpace($rule.haric_kelimeler)) {
                foreach ($ex in ($rule.haric_kelimeler -split "\|")) {
                    $exNorm = Normalize-Turkic $ex
                    if ($exNorm -and $normTitle.Contains($exNorm)) { $blocked = $true; break }
                }
            }
            if ($blocked) { continue }

            if ($normDil -eq "ingilizce") {
                if ($normKonum -eq "turkiye" -and $normTitle -notmatch "turk|istanbul|ankara|izmir|anatolia") { continue }
                if ($normKonum -eq "azerbaycan" -and $normTitle -notmatch "azerbaijan|baku|karabakh|caucasus") { continue }
                if ($normKonum -eq "ozbekistan" -and $normTitle -notmatch "uzbek|tashkent|karakalpak|samarkand|central asia") { continue }
            }

            $results += @{
                kelime      = $rule.kelime
                dil         = $rule.dil
                konum       = $rule.konum
                kaynak_tipi = "Google Haber / Basın Duyurusu"
                baslik      = $title
                ozet        = "Haber içeriğinde '$($rule.kelime)' ifadesi tam eşleşme olarak yer almaktadır. Kaynak: $title"
                url         = $link
                tarih       = $pubRaw
            }
            if ($results.Count -ge 4) { break }
        }
    } catch {}
    return $results
}

function Search-LinkedInJobsDirect($rules, [int]$days = 3) {
    $results = @()
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [System.Text.Encoding]::UTF8
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")

    $seconds = [Math]::Max(86400, $days * 86400)
    $cutoffDate = (Get-Date).AddDays(-($days + 1))

    $countryQueries = @(
        @{ konum = "TÜRKİYE"; locParam = "Turkey"; q = '"CBS" OR "Coğrafi Bilgi Sistemleri" OR "GIS" OR "Geographic Information Systems"' },
        @{ konum = "AZERBAYCAN"; locParam = "Azerbaijan"; q = '"GIS" OR "Geographic Information Systems" OR "CİS"' },
        @{ konum = "ÖZBEKİSTAN"; locParam = "Uzbekistan"; q = '"GIS" OR "Geographic Information Systems" OR "GAT"' }
    )

    foreach ($cq in $countryQueries) {
        $countryRules = @($rules | Where-Object { (Normalize-Turkic $_.konum) -eq (Normalize-Turkic $cq.konum) })
        if ($countryRules.Count -eq 0) { continue }

        try {
            $liUrl = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=" + [uri]::EscapeDataString($cq.q) + "&location=" + [uri]::EscapeDataString($cq.locParam) + "&f_TPR=r$seconds&start=0"
            $html = $wc.DownloadString($liUrl)
            $cards = [regex]::Matches($html, '<div class="base-card[^>]*>(.*?)</div>\s*</li>', [System.Text.RegularExpressions.RegexOptions]::Singleline)

            foreach ($c in ($cards | Select-Object -First 6)) {
                $b = $c.Groups[1].Value
                $title = Strip-Html ([regex]::Match($b, 'base-search-card__title[^>]*>(.*?)</h3>', [System.Text.RegularExpressions.RegexOptions]::Singleline).Groups[1].Value)
                $comp  = Strip-Html ([regex]::Match($b, 'base-search-card__subtitle[^>]*>(.*?)</h4>', [System.Text.RegularExpressions.RegexOptions]::Singleline).Groups[1].Value)
                $link  = ([regex]::Match($b, 'href="(https://[^"?]+)', [System.Text.RegularExpressions.RegexOptions]::Singleline).Groups[1].Value).Trim()
                $date  = ([regex]::Match($b, 'datetime="([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Singleline).Groups[1].Value).Trim()

                try {
                    if ($date -and ([datetime]::Parse($date) -lt $cutoffDate)) { continue }
                } catch {}

                if ($link -match "-(\d+)$") {
                    $jobId = $Matches[1]
                    try {
                        $detailHtml = $wc.DownloadString("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/$jobId")
                        $descRaw = Strip-Html ([regex]::Match($detailHtml, '<div class="show-more-less-html__markup[^>]*>(.*?)</div>', [System.Text.RegularExpressions.RegexOptions]::Singleline).Groups[1].Value)
                        $fullJobText = "$title ($comp) - $descRaw"

                        foreach ($r in $countryRules) {
                            $matchedSnippet = Verify-FullTextMatch $r $fullJobText $link
                            if ($null -ne $matchedSnippet) {
                                $results += @{
                                    kelime      = $r.kelime
                                    dil         = $r.dil
                                    konum       = $r.konum
                                    kaynak_tipi = "LinkedIn (Uygulama İçi Yeni İlan)"
                                    baslik      = "$title — $comp"
                                    ozet        = $matchedSnippet
                                    url         = $link
                                    tarih       = "Yayın Tarihi: $date"
                                }
                                break
                            }
                        }
                    } catch {}
                }
            }
        } catch {}
    }
    return $results
}

function Search-RedditDirect($rules, [int]$days = 3) {
    $results = @()
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [System.Text.Encoding]::UTF8
    $wc.Headers.Add("User-Agent", "CBSGISMonitorBot/1.0")
    $tParam = if ($days -le 1) { "day" } elseif ($days -le 7) { "week" } else { "month" }

    foreach ($r in $rules) {
        if ($r.kural_tipi -ne "TAM_ESLESTIRME") { continue }
        try {
            $u = "https://www.reddit.com/search.json?q=" + [uri]::EscapeDataString("`"$($r.kelime)`"") + "&sort=new&t=$tParam&limit=4"
            $raw = $wc.DownloadString($u)
            $json = $raw | ConvertFrom-Json
            foreach ($child in $json.data.children) {
                $p = $child.data
                if ($null -eq $p) { continue }
                $link = if ($p.permalink) { "https://www.reddit.com" + $p.permalink } else { [string]$p.url }
                $fullText = "$([string]$p.title) - [$([string]$p.subreddit_name_prefixed)] $([string]$p.selftext)"
                $matchedSnippet = Verify-FullTextMatch $r $fullText $link
                if ($null -ne $matchedSnippet) {
                    $results += @{
                        kelime      = $r.kelime
                        dil         = $r.dil
                        konum       = $r.konum
                        kaynak_tipi = "Reddit (Uygulama İçi Son Gönderiler)"
                        baslik      = [string]$p.title
                        ozet        = $matchedSnippet
                        url         = $link
                        tarih       = "Reddit Yeni Gönderi"
                    }
                }
            }
        } catch {}
    }
    return $results
}

function Load-Rules {
    if (!(Test-Path $RulesPath)) { return @() }
    return @(Import-Csv -Path $RulesPath -Encoding UTF8)
}

function Save-Rules($rows) {
    $rows | Export-Csv -Path $RulesPath -NoTypeInformation -Encoding UTF8
}

function Load-State {
    if (Test-Path $StatePath) {
        try {
            $raw = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
            return @{
                last_email_at = if ($raw.last_email_at) { $raw.last_email_at } else { "Henüz gönderilmedi" }
                last_query_at = if ($raw.last_query_at) { $raw.last_query_at } else { "Henüz sorgulanmadı" }
                emailed_urls  = @($raw.emailed_urls)
                pending_items = @($raw.pending_items)
            }
        } catch {}
    }
    return @{
        last_email_at = "Henüz gönderilmedi"
        last_query_at = "Henüz sorgulanmadı"
        emailed_urls  = @()
        pending_items = @()
    }
}

function Save-State($st) {
    $st | ConvertTo-Json -Depth 6 | Set-Content -Path $StatePath -Encoding UTF8
}

function Load-EmailConfig {
    if (Test-Path $EmailCfgPath) {
        try { return (Get-Content $EmailCfgPath -Raw -Encoding UTF8 | ConvertFrom-Json) } catch {}
    }
    return @{ to_email = ""; smtp_user = ""; smtp_pass = "" }
}

function Run-Scan([string]$filterKonum, [int]$days = 3) {
    if ($days -le 0) { $days = 3 }
    $rules = @(Load-Rules | Where-Object { $_.aktif -eq "1" })
    if (![string]::IsNullOrWhiteSpace($filterKonum)) {
        $rules = @($rules | Where-Object { (Normalize-Turkic $_.konum) -eq (Normalize-Turkic $filterKonum) })
    }

    $state = Load-State
    $emailedSet = New-Object System.Collections.Generic.HashSet[string]
    foreach ($u in $state.emailed_urls) { if ($u) { [void]$emailedSet.Add([string]$u) } }

    # Seçilen gün aralığına göre yeniden filtrelemek için pendingMap'i taze oluştur
    $pendingMap = @{}

    foreach ($rule in $rules) {
        foreach ($it in (Search-GoogleNewsByRule $rule $days)) {
            if ($it.url -and !$emailedSet.Contains($it.url) -and !$pendingMap.ContainsKey($it.url)) {
                $pendingMap[$it.url] = $it
            }
        }
    }

    foreach ($it in (Search-LinkedInJobsDirect $rules $days)) {
        if ($it.url -and !$emailedSet.Contains($it.url) -and !$pendingMap.ContainsKey($it.url)) {
            $pendingMap[$it.url] = $it
        }
    }

    foreach ($it in (Search-RedditDirect $rules $days)) {
        if ($it.url -and !$emailedSet.Contains($it.url) -and !$pendingMap.ContainsKey($it.url)) {
            $pendingMap[$it.url] = $it
        }
    }

    $state.pending_items = @($pendingMap.Values)
    $state.last_query_at = (Get-Date).ToString("dd.MM.yyyy HH:mm") + " (Son $days Gün)"
    Save-State $state
    return $state
}

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:5000/")
$listener.Start()
Write-Host "CBS/GIS Yerel Web Sunucusu baslatildi: http://localhost:5000/"

while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    $req = $ctx.Request
    $res = $ctx.Response
    $res.Headers.Add("Access-Control-Allow-Origin", "*")
    $res.Headers.Add("Access-Control-Allow-Headers", "Content-Type")
    $res.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    if ($req.HttpMethod -eq "OPTIONS") {
        $res.StatusCode = 200
        $res.Close()
        continue
    }

    $path = $req.Url.AbsolutePath
    $bodyText = ""
    if ($req.HasEntityBody) {
        $reader = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
        $bodyText = $reader.ReadToEnd()
        $reader.Close()
    }

    try {
        if ($path -eq "/" -or $path -eq "/index.html") {
            $htmlBytes = [System.IO.File]::ReadAllBytes($HtmlPath)
            $res.ContentType = "text/html; charset=utf-8"
            $res.OutputStream.Write($htmlBytes, 0, $htmlBytes.Length)
            $res.Close()
            continue
        }

        $outObj = @{ ok = $true }

        if ($path -eq "/api/state") {
            $st = Load-State
            $outObj = @{
                ok            = $true
                last_email_at = $st.last_email_at
                last_query_at = $st.last_query_at
                items         = @($st.pending_items)
                rules         = @(Load-Rules)
                email_config  = (Load-EmailConfig)
            }
        }
        elseif ($path -eq "/api/scan") {
            $payload = if ($bodyText) { $bodyText | ConvertFrom-Json } else { @{} }
            $d = if ($payload.days) { [int]$payload.days } else { 3 }
            $st = Run-Scan ([string]$payload.konum) $d
            $outObj = @{
                ok            = $true
                count         = $st.pending_items.Count
                last_email_at = $st.last_email_at
                last_query_at = $st.last_query_at
                items         = @($st.pending_items)
            }
        }
        elseif ($path -eq "/api/mark-seen") {
            $st = Load-State
            foreach ($it in $st.pending_items) {
                if ($it.url) { $st.emailed_urls += $it.url }
            }
            $st.pending_items = @()
            $st.last_email_at = (Get-Date).ToString("dd.MM.yyyy HH:mm") + " (Sıfırlandı)"
            Save-State $st
            $outObj = @{ ok = $true }
        }
        elseif ($path -eq "/api/rules/add") {
            $p = $bodyText | ConvertFrom-Json
            $rows = [System.Collections.ArrayList]@(Load-Rules)
            [void]$rows.Add([PSCustomObject]@{
                aktif             = "1"
                kelime            = [string]$p.kelime
                kural_tipi        = [string]$p.kural_tipi
                dil               = [string]$p.dil
                konum             = [string]$p.konum
                baglam_kelimeleri = ""
                haric_kelimeler   = ""
            })
            Save-Rules $rows
            $outObj = @{ ok = $true; rules = @(Load-Rules) }
        }
        elseif ($path -match "^/api/rules/toggle/(\d+)$") {
            $idx = [int]$Matches[1] - 1
            $rows = @(Load-Rules)
            if ($idx -ge 0 -and $idx -lt $rows.Count) {
                $rows[$idx].aktif = if ($rows[$idx].aktif -eq "1") { "0" } else { "1" }
                Save-Rules $rows
            }
            $outObj = @{ ok = $true; rules = @(Load-Rules) }
        }
        elseif ($path -match "^/api/rules/delete/(\d+)$") {
            $idx = [int]$Matches[1] - 1
            $rows = [System.Collections.ArrayList]@(Load-Rules)
            if ($idx -ge 0 -and $idx -lt $rows.Count) {
                $rows.RemoveAt($idx)
                Save-Rules $rows
            }
            $outObj = @{ ok = $true; rules = @(Load-Rules) }
        }
        elseif ($path -eq "/api/email-config") {
            $p = $bodyText | ConvertFrom-Json
            $p | ConvertTo-Json | Set-Content -Path $EmailCfgPath -Encoding UTF8
            $outObj = @{ ok = $true }
        }
        elseif ($path -eq "/api/send-email-now") {
            $st = Load-State
            $cfg = Load-EmailConfig
            if ([string]::IsNullOrWhiteSpace($cfg.to_email) -or [string]::IsNullOrWhiteSpace($cfg.smtp_user) -or [string]::IsNullOrWhiteSpace($cfg.smtp_pass)) {
                $outObj = @{
                    ok      = $true
                    sent    = $false
                    count   = $st.pending_items.Count
                    message = "E-posta gönderebilmek için lütfen sayfanın en altındaki 'E-posta Bildirim Ayarları' bölümünden Alıcı E-posta ve Gmail Uygulama Şifrenizi kaydedin."
                }
            } else {
                $smtp = New-Object System.Net.Mail.SmtpClient("smtp.gmail.com", 587)
                $smtp.EnableSsl = $true
                $smtp.Credentials = New-Object System.Net.NetworkCredential($cfg.smtp_user, $cfg.smtp_pass)
                $mail = New-Object System.Net.Mail.MailMessage($cfg.smtp_user, $cfg.to_email)
                $mail.Subject = "[CBS/GIS Raporu] $($st.pending_items.Count) Yeni İçerik ($((Get-Date).ToString('dd.MM.yyyy HH:mm')))"
                $mail.IsBodyHtml = $true
                $bodyHtml = "<h2>CBS / GIS Yeni Sonuçlar ($($st.pending_items.Count) Adet)</h2><ul>"
                foreach ($it in $st.pending_items) {
                    $bodyHtml += "<li><strong>[$($it.konum) - $($it.kelime)] ($($it.kaynak_tipi))</strong>: <a href='$($it.url)'>$($it.baslik)</a><br/><small>$($it.ozet)</small></li><br/>"
                    $st.emailed_urls += $it.url
                }
                $bodyHtml += "</ul>"
                $mail.Body = $bodyHtml
                $smtp.Send($mail)

                $st.last_email_at = (Get-Date).ToString("dd.MM.yyyy HH:mm")
                $st.pending_items = @()
                Save-State $st
                $outObj = @{
                    ok      = $true
                    sent    = $true
                    count   = $st.emailed_urls.Count
                    message = "Rapor e-postası ($($cfg.to_email)) adresine başarıyla gönderildi!"
                }
            }
        }

        $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes(($outObj | ConvertTo-Json -Depth 6))
        $res.ContentType = "application/json; charset=utf-8"
        $res.OutputStream.Write($jsonBytes, 0, $jsonBytes.Length)
    } catch {
        $errBytes = [System.Text.Encoding]::UTF8.GetBytes((@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json))
        $res.ContentType = "application/json; charset=utf-8"
        $res.OutputStream.Write($errBytes, 0, $errBytes.Length)
    }
    $res.Close()
}
