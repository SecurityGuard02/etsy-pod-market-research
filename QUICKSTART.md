# JARVIS — çalıştırma

Bu paket, Python bağımlılıklarını ve uygulamayı Docker ile kurar. API bilgileri yönetim panelinden girilir ve kalıcı diskte şifrelenerek saklanır. API erişimleri, mağaza/hesap sahipliği ve platformların uygulama izinleri kullanıcı tarafından sağlanır. Hesap izin ekranları ve ilk yönetici girişi de gereklidir.

## Bilgisayar açık kalmadan: Render

Deponun `main` dalındaki `render.yaml`, uygulamayı `jarvis-ready` dalından derler. Hem kurulum tanımı hem uygulama kaynakları GitHub'a yüklenmiş ve genel erişimle doğrulanmıştır. Bu, sizin hesabınızda bir sunucunun kurulmuş olduğu anlamına gelmez; kurulum aşağıdaki bağlantıyla başlar.

[Render kurulumunu açın](https://render.com/deploy?repo=https://github.com/SecurityGuard02/etsy-pod-market-research). Render hesabınızla giriş yapıp kurulum ekranını onaylayın. Alternatif olarak **New → Blueprint** üzerinden `SecurityGuard02/etsy-pod-market-research` deposunun `main` dalını seçin. Render, Blueprint'in belirttiği `jarvis-ready` dalından uygulamayı kurar. Tek servis ve 1 GB kalıcı disk oluşturulur. **Starter ve kalıcı disk ücretlidir**; oluşturma ekranında güncel tutarı kontrol edin. Sürekli araştırma takvimi ve Telegram botu için servis açık kalmalıdır. Tek örnek kullanın; SQLite veritabanını birden fazla çalışan servis arasında paylaşmayın.

Render'ın **Environment** ekranında otomatik oluşturulan `APP_PASSWORD` değerini güvenli şekilde görüntüleyin. Servisin HTTPS adresini açın ve yönetici parolasıyla giriş yapın. Gerekli API bilgilerini panelden ekleyin; gerektiğinde platformun kendi yetki ekranını tamamlayın. `JARVIS_SETUP_TOKEN` ilk kurulum korumasıdır; güvenli saklayın. Uygulama Render'ın `RENDER_EXTERNAL_URL` adresini kullanır. Özel alan adı kullanırsanız `APP_PUBLIC_URL=https://alan-adiniz` ekleyin ve platformlardaki geri dönüş adreslerini güncelleyin.

`/data` dizini veritabanını, görselleri ve şifreleme anahtarını birlikte saklar. Diski silmeyin. Yedek alırken bu dizinin tamamını birlikte yedekleyin; yalnızca veritabanı, kaybolan şifreleme anahtarını geri getiremez. Render disk anlık görüntülerinin kapsamını ve saklama süresini hesabınızdan kontrol edin.

## Yerelde tek komut

Docker Desktop veya Docker Engine + Compose v2 çalışırken arşivi açın ve klasör içinde:

```bash
bash install.sh
```

Kurucu bağımlılıkları Docker imajına yükler, `.env` yoksa güçlü yönetici ve kurulum parolaları üretir, kalıcı veri alanını oluşturur ve uygulamayı başlatır. Mevcut `.env` dosyasını değiştirmez. İlk derleme internet bağlantısı gerektirir. Docker Hub ve PyPI erişimi gereklidir.

`http://127.0.0.1:8080` adresini açın. `.env` dosyasındaki `APP_PASSWORD` ile giriş yapın; API bilgilerini panelden ekleyin. `.env`, veri dizini ve yedekler gizlidir. Bunları GitHub'a yüklemeyin. Yerel adres varsayılan olarak yalnızca bu bilgisayardan erişilir. Instagram gibi hizmetlerin dışarıdan okuyacağı görseller ve OAuth geri dönüşleri için herkese açık HTTPS adresi gerekir; sürekli kullanımda Render seçeneğini kullanın.

Windows'ta Render seçeneği komut satırı gerektirmez. Yerel seçenek için Docker Desktop ve WSL terminalinde `bash install.sh` kullanın.

## Güncelleme ve bakım

```bash
docker compose logs --tail 100 jarvis
docker compose stop
docker compose up --build --detach --wait
```

Yeni sürümde kaynak dosyalarını güncelleyin; kendi `.env` dosyanızı koruyun. Docker verileri `jarvis-data` adlı kalıcı birimde tutar. `docker compose down --volumes` verileri siler; bakım için kullanmayın. Gerçek birim adı Docker Compose proje öneki içerir.

Takvim, API çağrıları ve yayınlar yalnızca servis çalışırken gerçekleşir. Tasarım üretimi ve ses API'leri ayrıca kullanım ücreti doğurabilir. Yayın izni isteyen aşamalar Telegram onayından sonra çalışır; API erişimi olmayan platform aşaması panelde eksik bağlantı olarak kalır. Hiçbir kurulum gerçek Etsy satış sayılarını veya Pinterest beğeni sıralamasını garanti edemez; araştırmanın veri kaynağı ve belirsizliği raporda gösterilmelidir.

## Ortam değişkenleri

| Değişken | İşlev |
| --- | --- |
| `APP_PASSWORD` | İlk yönetim parolası; Render ve yerel kurucu üretir. Mevcut veritabanındaki parolayı değiştirmez. |
| `JARVIS_SETUP_TOKEN` | İlk kurulumun yetkisiz kullanıcıya açılmasını önleyen gizli değer. |
| `JARVIS_DATA_DIR` | Kalıcı veri dizini; Docker/Render için `/data`. |
| `PORT` | İç servis portu; Compose `8080`, Render `10000`. |
| `APP_PUBLIC_URL` | İsteğe bağlı genel HTTPS adresi; Render adresinden otomatik bulunabilir. |
| `JARVIS_PORT` | Yalnızca Compose: bilgisayardaki port, varsayılan `8080`. |
| `JARVIS_BIND_HOST` | Yalnızca Compose: varsayılan `127.0.0.1`; uzaktan erişimi HTTPS vekiliyle yönetin. |
