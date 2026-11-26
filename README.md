# Kick VOD İndirici

Windows PC'nizde çalışır, telefondan kullanılır. Kick VOD linkini telefondan yapıştırırsınız,
hangi aralığı istediğinizi ve parça süresini seçersiniz. PC **yalnızca o aralığı, parça parça**
indirir; her parça biter bitmez telefona **İndir** ve **Infuse** butonları gelir. Tüm VOD'un
inmesini beklemezsiniz.

## Kullanım

1. [Releases](https://github.com/Exquve/kick-vod-downloader/releases/latest) sayfasından **KickIndirici.exe**'yi indirin.
2. Çift tıklayın. Kurulum yok, Python ya da ffmpeg gerekmiyor.
3. SmartScreen uyarısı çıkarsa **Ek bilgi → Yine de çalıştır** deyin.
4. İlk açılışta güvenlik duvarı izni sorulursa **Evet** deyin (yalnızca Özel ağlar için).
5. Açılan sayfadaki **QR kodu telefonla okutun**. Bitti.

Telefonda: linki yapıştırın → başlık ve süre gelir → aralığı (ör. 1:00:00–3:30:00 ya da "Tümü")
ve parça süresini seçin → **İndirmeyi başlat**. İlk parça hazır olunca izlemeye başlayabilirsiniz.
İstediğiniz an **⏸ Durdur** deyip sonra **▶ Devam et** diyebilirsiniz; parçanın ortasından,
kaldığı yerden sürer. İnternet kesilse ya da bilgisayar kapansa bile inen kısım kaybolmaz.

Uygulama saatin yanındaki tepside durur: **Telefondan aç (QR)**, **İndirme klasörünü aç**,
**Çıkış**. Çıkış'a basınca hiçbir şey arka planda çalışmaya devam etmez.

Dosyalar `Videolar\KickVODs` içine iner (Ayarlar'dan değiştirilebilir).

**Güncellemeler otomatik:** Yeni sürüm çıkınca uygulama arka planda indirir, doğrular ve
devam eden indirme yoksa birkaç saniyede kendini yeniden başlatır (adres ve QR aynı kalır).
Ayarlar'dan kapatılabilir. exe'yi yazılabilir bir klasörde (ör. Belgeler, Masaüstü) tutun.

<details>
<summary><b>Farklı ağdan (mobil veri vb.) kullanmak</b></summary>

İki yol var:

- **Ayarlar → "Farklı ağlardan erişim"**: Cloudflare üzerinden geçici, şifreli bir adres açılır;
  modem ayarı gerekmez. QR, PC'deki "Telefondan aç" sayfasında görünür. Adres uygulama her
  açıldığında değişir. Büyük dosyalarda hız Cloudflare'e bağlıdır.
- **Tailscale** (PC'ye ve telefona kurulursa): sabit adres, daha hızlı. Tailscale varsa QR'ı
  "Telefondan aç" sayfasında otomatik görünür.

İki yolda da gizli bir erişim anahtarı gerekir (QR'da gömülüdür). Anahtarı aynı sayfadan
yenileyebilirsiniz; eski linkler çalışmaz olur.
</details>

<details>
<summary><b>Telefon bağlanamıyorsa</b></summary>

- Telefon ve PC aynı Wi-Fi'da mı?
- Windows'ta ağ profili **Özel** olmalı: Ayarlar → Ağ ve İnternet → Wi-Fi → ağınız → *Ağ profili türü: Özel*.
- Güvenlik duvarı izni reddedildiyse "Telefondan aç" sayfasında elle izin verme adımları yazar.
</details>

<details>
<summary><b>Abonelere özel VOD'lar</b></summary>

Ayarlar'da PC'deki tarayıcıyı seçin (Firefox önerilir). O tarayıcıda Kick hesabınız açık olmalı.
Chrome/Edge açıkken çerezleri okunamayabilir.
</details>

---

## Geliştirici

### Mimari

| Parça | Ne yapar |
|---|---|
| `kickindirici/main.py` | Tek örnek kilidi, Job Object, güvenlik duvarı, sunucu (waitress), tepsi |
| `kickindirici/jobs.py` | Kalıcı iş kuyruğu (sırayla, tek tek), iptal/silme/tekrar deneme |
| `kickindirici/ytdlp.py` | Resmi `yt-dlp.exe`'yi `%LOCALAPPDATA%\KickIndirici\bin` içine indirir, her açılışta `-U`; video bilgisini ve HLS adresini çözer |
| `kickindirici/hls.py` | Seçilen aralığın HLS segmentlerini 8 paralel bağlantıyla indirip ffmpeg'e (`-c copy`, `+faststart`) akıtır; parçalar segment sınırından kesilir, boşluk/çakışma olmaz |
| `kickindirici/splitter.py` | Parça planı (son parça 1 dk'dan kısaysa öncekine eklenir), dosya adları |

HLS olmayan ya da doğrudan okunamayan kaynaklarda (ör. şifreli) yedek olarak
`yt-dlp --download-sections` ile aynı parça parça akış kullanılır.
| `kickindirici/web.py` | Flask arayüzü/API, HTTP Range destekli dosya servisi, erişim anahtarı |
| `kickindirici/procs.py` | Alt süreç kaydı, `KILL_ON_JOB_CLOSE` Job Object |
| `kickindirici/tunnel.py` | İsteğe bağlı Cloudflare Quick Tunnel |
| `kickindirici/updater.py` | Son Release'i kontrol eder, exe'yi indirip SHA256 ile doğrular, iş yokken değiştirip yeniden başlatır |

Veriler: ayarlar `%LOCALAPPDATA%\KickIndirici\config.json`, iş listesi `jobs.json`, log `log.txt`.
Her iş indirme klasöründe `<başlık> [<id>]` alt klasöründedir.

### Derleme (Windows)

Gerekenler: Python 3.12+ ve PowerShell.

```powershell
git clone https://github.com/Exquve/kick-vod-downloader
cd kick-vod-downloader
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Komut sırasıyla şunları yapar: venv kurar, bağımlılıkları yükler, ffmpeg/ffprobe'u
(gyan.dev essentials) `vendor\` içine indirir, testleri çalıştırır ve `dist\KickIndirici.exe`'yi
üretir. Testleri atlamak için `-SkipTests` ekleyin. UPX kullanılmaz.

### Geliştirme ortamında çalıştırma

```bash
pip install -r requirements-dev.txt
python run.py --no-tray        # http://127.0.0.1:8765
python -m pytest -q
python scripts/exit_check.py python run.py --no-tray   # çıkış sonrası süreç kontrolü
# Windows'ta, derlenmiş exe ile:
python scripts/update_check.py dist/KickIndirici.exe    # otomatik güncelleme uçtan uca
```

### Sürüm yayınlama

`kickindirici/__init__.py` içindeki sürümü artırıp `main`'e push etmeniz yeterli. GitHub Actions
exe'yi derler, testleri ve süreç kontrolünü çalıştırır; o sürümün etiketi (`v1.0.1` gibi) henüz yoksa
etiketi oluşturup exe'yi Release'e ekler. Elle `v*` etiketi push etmek de aynı işi yapar.
Her push'ta exe ayrıca artifact olarak yüklenir.
