# Kick VOD İndirici

Windows PC'nizde çalışır, telefondan kullanılır. Kick VOD linkini telefondan yapıştırırsınız;
PC videoyu indirir, seçtiğiniz sürelik parçalara böler ve her parça için telefona
**İndir** ve **Infuse** butonları verir.

## Kullanım

1. [Releases](https://github.com/Exquve/kick-vod-downloader/releases/latest) sayfasından **KickIndirici.exe**'yi indirin.
2. Çift tıklayın. Kurulum yok, Python ya da ffmpeg gerekmiyor.
3. SmartScreen uyarısı çıkarsa **Ek bilgi → Yine de çalıştır** deyin.
4. İlk açılışta güvenlik duvarı izni sorulursa **Evet** deyin (yalnızca Özel ağlar için).
5. Açılan sayfadaki **QR kodu telefonla okutun**. Bitti.

Uygulama saatin yanındaki tepside durur: **Telefondan aç (QR)**, **İndirme klasörünü aç**,
**Çıkış**. Çıkış'a basınca hiçbir şey arka planda çalışmaya devam etmez.

Dosyalar `Videolar\KickVODs` içine iner (Ayarlar'dan değiştirilebilir).

<details>
<invoke name="Bash">
<parameter name="command">cd /home/user/kick-vod-downloader && cat > README.md <<'EOF'
# Kick VOD İndirici

Windows PC'nizde çalışır, telefondan kullanılır. Kick VOD linkini telefondan yapıştırırsınız;
PC videoyu indirir, seçtiğiniz sürelik parçalara böler ve her parça için telefona
**İndir** ve **Infuse** butonları verir.

## Kullanım

1. [Releases](https://github.com/Exquve/kick-vod-downloader/releases/latest) sayfasından **KickIndirici.exe**'yi indirin.
2. Çift tıklayın. Kurulum yok, Python ya da ffmpeg gerekmiyor.
3. SmartScreen uyarısı çıkarsa **Ek bilgi → Yine de çalıştır** deyin.
4. İlk açılışta güvenlik duvarı izni sorulursa **Evet** deyin (yalnızca Özel ağlar için).
5. Açılan sayfadaki **QR kodu telefonla okutun**. Bitti.

Uygulama saatin yanındaki tepside durur: **Telefondan aç (QR)**, **İndirme klasörünü aç**,
**Çıkış**. Çıkış'a basınca hiçbir şey arka planda çalışmaya devam etmez.

Dosyalar `Videolar\KickVODs` içine iner (Ayarlar'dan değiştirilebilir).

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
| `kickindirici/ytdlp.py` | Resmi `yt-dlp.exe`'yi `%LOCALAPPDATA%\KickIndirici\bin` içine indirir, her açılışta `-U`; ilerleme ayrıştırma |
| `kickindirici/splitter.py` | ffmpeg segment muxer ile `-c copy` bölme (tek geçiş, anahtar karede kesim, `+faststart`) |
| `kickindirici/web.py` | Flask arayüzü/API, HTTP Range destekli dosya servisi, erişim anahtarı |
| `kickindirici/procs.py` | Alt süreç kaydı, `KILL_ON_JOB_CLOSE` Job Object |
| `kickindirici/tunnel.py` | İsteğe bağlı Cloudflare Quick Tunnel |

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
```

### Sürüm yayınlama

`kickindirici/__init__.py` içindeki sürümü artırıp `main`'e push etmeniz yeterli. GitHub Actions
exe'yi derler, testleri ve süreç kontrolünü çalıştırır; o sürümün etiketi (`v1.0.1` gibi) henüz yoksa
etiketi oluşturup exe'yi Release'e ekler. Elle `v*` etiketi push etmek de aynı işi yapar.
Her push'ta exe ayrıca artifact olarak yüklenir.
