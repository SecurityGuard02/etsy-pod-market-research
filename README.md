# JARVIS — Etsy POD Asistanı

Telegram onayıyla çalışan, Türkçe kurulum ekranına sahip bir uygulama. Pazartesi, çarşamba ve cuma araştırması; beş özgün tasarım; Printify ürün taslağı; Etsy taslağı ve son yayın onayı; ayrı Instagram ve Pinterest onayları içerir. Türkçe sesli özetlerde başlangıç sesi `onyx` kullanılır.

[![Render ile kur](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/SecurityGuard02/etsy-pod-market-research)

Kurulum bağlantısı Render hesabınızda ücretli sürekli çalışan bir servis ve kalıcı disk oluşturur. Yeni bir sunucu hazırlamak veya n8n akışları kurmak gerekmez. Kurulumdan sonra oluşturulan yönetici parolasıyla paneli açın, API bilgilerinizi girin ve platformların hesap yetkilerini tamamlayın. Ayrıntılar: [QUICKSTART.md](QUICKSTART.md).

Yerelde Docker ile: `bash install.sh`. Yerel bilgisayar kapandığında program durur; sürekli çalışma için bulut seçeneğini kullanın.

## İşleyiş

1. API bilgileri şifreli olarak kalıcı diskte saklanır. Telegram sahibini eşleştirme bağlantısıyla belirlersiniz.
2. Bağlantıları test eder, deneme modunu kapatır ve programı açarsınız.
3. Kaynaklı araştırma raporunu onaylayınca beş özgün görsel üretilir. Her görsel için ayrı ürün onayı verirsiniz.
4. Printify kataloğundan gerçek ürün/varyantlar seçilir; onayla yalnız yayınlanmamış ürün oluşturulur.
5. Etsy taslağı, gerçek giysi mockup'ı ve beden/renk varyantlarıyla hazırlanır. Gerçek taslak ve envanter geri okunur. Son onaydan hemen önce değişiklik kontrol edilir.
6. Etsy yayını doğrulanınca farklı yetişkin sentetik mankenli tanıtım görseli hazırlanabilir. Instagram ve Pinterest için ayrı yayın onayları gerekir.

## Doğrulama ve sınırlar

`python -m unittest discover -s tests -v` ile sağlayıcı istekleri, bütün onay akışı, kimlik doğrulama, CSRF, şifreli saklama, tekrar yayınlamama ve yeniden başlatma kontrolleri sınanır. Testler sahte HTTP yanıtları kullanır; canlı API hesaplarını veya gerçek yayın başarısını kanıtlamaz. API bilgileri girildikten sonra bağlantı ve tek ürün testi yapılmalıdır.

- Kesin rakip satış adetleri ve Pinterest'in genel beğeni sıralaması API erişimi olmadan uydurulmaz. Araştırma kaynakları ve belirsizlikler gösterilir.
- Üretim maliyeti katalogda sağlanmıyorsa en ucuz sağlayıcı veya kâr garantisi verilmez. Ayarlanan satış fiyatı ve eldeki maliyet bilgisi onaya sunulur.
- AI bütçe alanı bir hedef tutardır; gerçek fatura üst sınırı değildir. Kampanya başına çağrı ve görsel sayısı sınırlıdır. Sağlayıcınızın harcama kontrollerini ayrıca kullanın.
- Etsy taslakları doğrudan Etsy API ile açılır. Printify mağazaya `publish` çağrısı yapılmaz. **Siparişlerin otomatik üretime aktarımı/eşlemesi kurulmamıştır; satış sonrası sipariş takibi manueldir.**
- Baskı dosyası ve manken görseli insan onayından geçer. Kaynak çözünürlüğü ürün planında gösterilir; büyük baskı kalitesi garanti edilmez. Fiziksel numune önerilir.
- Geçerli Etsy üretim ortağı/kargo/üretim süresi ve hesap politikaları mağazada mevcut olmalıdır. Platform uygulama incelemeleri veya profesyonel hesap koşulları kurulumla aşılamaz.
- Tek örnek, kalıcı `/data` diski gerekir. Disk; veritabanı, görseller ve şifreleme anahtarıyla birlikte yedeklenmelidir.
