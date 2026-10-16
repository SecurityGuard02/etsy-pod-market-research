from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .store import Store, SECRET_FIELDS, DEFAULTS
from .providers import (EtsyClient, InstagramClient, OpenAIClient, PinterestClient,
                        PrintifyClient, TelegramClient, ProviderError)
from .engine import Engine


ALLOWED_SETTINGS = set(DEFAULTS) | SECRET_FIELDS | {
    'telegram_chat_id', 'telegram_user_id', 'etsy_shop_id', 'instagram_account_id',
    'instagram_login_type', 'instagram_client_id', 'pinterest_board_id', 'pinterest_client_id',
    'printify_shop_id', 'printify_blueprint_id', 'printify_provider_id',
    'etsy_shipping_profile_id', 'etsy_readiness_state_id', 'etsy_taxonomy_id',
    'etsy_return_policy_id', 'production_partner_ids', 'colors', 'sizes',
    'pinterest_region', 'etsy_token_expires_at', 'pinterest_token_expires_at',
    'instagram_api_version', 'min_margin', 'etsy_fee_rate', 'fixed_fees',
}


def create_app(data_dir=None, workers=True):
    store = Store(data_dir or os.getenv('JARVIS_DATA_DIR', 'data'))
    engine = Engine(store)
    tasks = set()
    rate_limits = {}

    def launch(coro, label):
        async def guarded():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                store.add_event(f'{label}: {exc}', 'error')
        task = asyncio.create_task(guarded())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def refresh_etsy():
        cfg = store.get_settings()
        if cfg.get('etsy_refresh_token') and float(cfg.get('etsy_token_expires_at', 0)) < time.time() + 300:
            token = await EtsyClient(cfg).refresh_access_token()
            store.update_settings({'etsy_access_token': token['access_token'],
                                   'etsy_refresh_token': token.get('refresh_token', cfg['etsy_refresh_token']),
                                   'etsy_token_expires_at': time.time() + token.get('expires_in', 3600)})

    async def refresh_social_tokens():
        cfg = store.get_settings()
        if (cfg.get('pinterest_refresh_token') and
            float(cfg.get('pinterest_token_expires_at', 0)) < time.time() + 600):
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.post('https://api.pinterest.com/v5/oauth/token',
                    auth=(cfg.get('pinterest_client_id', ''), cfg.get('pinterest_client_secret', '')),
                    data={'grant_type': 'refresh_token', 'refresh_token': cfg['pinterest_refresh_token']})
            if r.status_code != 200:
                raise ValueError(f'Pinterest bağlantısı yenilenemedi (HTTP {r.status_code}); yeniden bağlayın.')
            token = r.json()
            store.update_settings({'pinterest_access_token': token['access_token'],
                'pinterest_refresh_token': token.get('refresh_token', cfg['pinterest_refresh_token']),
                'pinterest_token_expires_at': time.time() + token.get('expires_in', 2592000)})
        if (cfg.get('instagram_client_id') and cfg.get('instagram_access_token') and
            cfg.get('instagram_token_expires_at') and
            float(cfg['instagram_token_expires_at']) < time.time() + 7 * 86400):
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get('https://graph.instagram.com/refresh_access_token',
                    params={'grant_type': 'ig_refresh_token', 'access_token': cfg['instagram_access_token']})
            if r.status_code != 200:
                raise ValueError(f'Instagram bağlantısı yenilenemedi (HTTP {r.status_code}); yeniden bağlayın.')
            token = r.json()
            store.update_settings({'instagram_access_token': token['access_token'],
                'instagram_token_expires_at': time.time() + token.get('expires_in', 5184000)})

    async def telegram_loop():
        while True:
            cfg = store.get_settings()
            if not cfg.get('telegram_bot_token'):
                await asyncio.sleep(3)
                continue
            try:
                client = TelegramClient(cfg)
                # Do not silently delete a pre-existing webhook belonging to another app.
                if not store.get_meta('telegram_polling_checked'):
                    webhook = await client.webhook_info()
                    if webhook.get('url'):
                        raise ValueError('Bu botta webhook var. Jarvis için yeni BotFather botu kullanın.')
                    store.set_meta('telegram_polling_checked', True)
                updates = await client.updates(offset=store.get_meta('telegram_offset', 0), timeout=20)
                for update in updates:
                    store.set_meta('telegram_offset', update['update_id'] + 1)
                    message = update.get('message', {})
                    text = message.get('text', '')
                    chat = message.get('chat', {})
                    sender = message.get('from', {})
                    claim = store.get_meta('telegram_claim')
                    if (claim and claim['expires'] > time.time() and chat.get('type') == 'private'
                        and hmac.compare_digest(text, '/start ' + claim['code'])):
                        store.update_settings({'telegram_chat_id': str(chat['id']),
                                               'telegram_user_id': str(sender['id'])})
                        store.set_meta('telegram_claim', None)
                        await client.send_message('Bağlantı tamamlandı. Kararları yalnız bu özel sohbetten kabul edeceğim. '
                                                  'Sesli özetler yapay zekâ ile üretilir. /status /pause /resume', chat_id=chat['id'])
                        store.add_event('Telegram sahibi güvenli eşleştirmeyle bağlandı.')
                        continue
                    is_owner = (str(sender.get('id', '')) == str(cfg.get('telegram_user_id', cfg.get('telegram_chat_id', 'x')))
                                and str(chat.get('id', '')) == str(cfg.get('telegram_chat_id', 'x')))
                    if is_owner and text in ('/pause', '/durdur'):
                        store.update_settings({'paused': True})
                        store.invalidate_actions()
                        await client.send_message('Yeni işlemler durduruldu. Bekleyen onaylar iptal edildi. Gönderilmiş API çağrısı geri alınamaz.')
                    elif is_owner and text == '/resume':
                        store.update_settings({'paused': False})
                        await client.send_message('Devam edildi. İptal edilmiş onaylar tekrar kullanılmaz; yeni paket başlatabilirsiniz.')
                    elif is_owner and text == '/status':
                        await client.send_message(f"Durum: {'duraklatıldı' if cfg.get('paused') else 'çalışıyor'}. "
                                                  f"Program: {'açık' if cfg.get('scheduling_enabled') else 'kapalı'}. "
                                                  'Ayrıntılar yönetim ekranında.')
                    callback = update.get('callback_query')
                    if callback:
                        callback_chat = callback.get('message', {}).get('chat', {}).get('id')
                        sender_id = callback.get('from', {}).get('id')
                        expected_sender = cfg.get('telegram_user_id') or cfg.get('telegram_chat_id')
                        if (str(sender_id) != str(expected_sender) or
                            str(callback_chat) != str(cfg.get('telegram_chat_id'))):
                            await client.answer_callback(callback['id'], 'Bu işlem için yetkiniz yok.')
                            continue
                        raw = callback.get('data', '')
                        decision, _, token = raw.partition(':')
                        if decision not in ('approve', 'reject'):
                            await client.answer_callback(callback['id'], 'Geçersiz karar.')
                            continue
                        await client.answer_callback(callback['id'], 'Karar alındı; sonuç ayrıca bildirilecek.')
                        launch(engine.decide(token, decision, actor_chat=callback_chat), 'Telegram kararı')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = store.get_meta('telegram_last_error', '')
                error = str(exc)
                if last != error:
                    store.add_event('Telegram: ' + error, 'error')
                    store.set_meta('telegram_last_error', error)
                await asyncio.sleep(10)

    async def schedule_loop():
        while True:
            try:
                try:
                    await refresh_etsy()
                except Exception as exc:
                    if time.time() - store.get_meta('etsy_refresh_error_at', 0) > 3600:
                        store.add_event('Etsy bağlantısı yenilenemedi: ' + str(exc), 'warning')
                        store.set_meta('etsy_refresh_error_at', time.time())
                if time.time() - store.get_meta('social_refresh_check', 0) > 3600:
                    store.set_meta('social_refresh_check', time.time())
                    try:
                        await refresh_social_tokens()
                    except Exception as exc:
                        store.add_event('Sosyal bağlantı yenilenemedi: ' + str(exc), 'warning')
                cfg = store.get_settings()
                now = datetime.now(ZoneInfo(cfg['time_zone']))
                key = now.strftime('%Y-%m-%d')
                if (cfg.get('scheduling_enabled') and not cfg.get('paused') and not cfg.get('dry_run')
                    and now.weekday() in (0, 2, 4) and now.hour == int(cfg['hour'])
                    and store.get_meta('last_scheduled_date') != key):
                    store.set_meta('last_scheduled_date', key)
                    active = [c for c in store.list_campaigns() if c['status'] not in
                              ('completed', 'rejected', 'dismissed', 'failed', 'cancelled')]
                    if active:
                        store.add_event('Program saati: önceki paket beklediği için yeni ücretli paket başlatılmadı.')
                    else:
                        launch(engine.run_campaign(), 'Zamanlanmış araştırma')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                store.add_event('Zamanlayıcı: ' + str(exc), 'error')
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        for campaign in store.list_campaigns():
            if campaign['status'] == 'processing':
                store.update_campaign(campaign['id'], status='uncertain')
                store.add_event('Yeniden başlatma: yarım kalan iş otomatik tekrarlanmadı. Uzak kayıtları kontrol edin.',
                                'warning', campaign['id'])
        if workers:
            launch(telegram_loop(), 'Telegram işçisi')
            launch(schedule_loop(), 'Zamanlayıcı')
        yield
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

    app = FastAPI(title='Jarvis Etsy Asistanı', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.engine = engine

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        store.add_event(str(exc), 'error')
        return JSONResponse({'detail': str(exc)}, status_code=502)

    @app.middleware('http')
    async def headers(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            expected = store.get_settings()['public_url']
            actual = str(request.base_url).rstrip('/')
            if origin and origin not in (expected, actual):
                return JSONResponse({'detail': 'İstek kaynağı doğrulanamadı.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api') or request.url.path.startswith('/auth'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def authorized(request, csrf=False):
        session = store.session(request.cookies.get('jarvis_session'))
        if not session:
            raise HTTPException(401, 'Önce giriş yapın.')
        if csrf and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
            raise HTTPException(403, 'Güvenlik oturumu yenilenmeli; sayfayı yenileyin.')
        return session

    async def payload(request):
        if int(request.headers.get('content-length', '0') or 0) > 100_000:
            raise HTTPException(413, 'İstek çok büyük.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 100_000:
                raise HTTPException(413, 'İstek çok büyük.')
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'JSON biçimi geçersiz.')
        if not isinstance(data, dict):
            raise HTTPException(400, 'Nesne bekleniyor.')
        return data

    def clean_settings(data):
        if not isinstance(data, dict):
            raise ValueError('Ayarlar nesne olmalı.')
        result = {k: v for k, v in data.items() if k in ALLOWED_SETTINGS}
        for key in SECRET_FIELDS:
            if key in result and not isinstance(result[key], str):
                raise ValueError('API kimlik bilgileri metin olmalı.')
        for key in ('paused', 'scheduling_enabled', 'dry_run'):
            if key in result and not isinstance(result[key], bool):
                raise ValueError('Program seçenekleri doğru/yanlış olmalı.')
        for key in ('printify_variant_ids', 'production_partner_ids'):
            if isinstance(result.get(key), str):
                try:
                    result[key] = [int(x.strip()) for x in result[key].split(',') if x.strip()]
                except ValueError:
                    raise ValueError('Varyant ve üretim ortağı kimlikleri sayı olmalı.')
        for key in ('hour',):
            if key in result:
                result[key] = int(result[key])
                if not 0 <= result[key] <= 23:
                    raise ValueError('Saat 0–23 arasında olmalı.')
        for key in ('budget_per_run', 'retail_price'):
            if key in result:
                result[key] = float(result[key])
                if not math.isfinite(result[key]) or result[key] <= 0:
                    raise ValueError('Fiyat ve bütçe pozitif olmalı.')
        if 'time_zone' in result:
            try:
                ZoneInfo(result['time_zone'])
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('Geçersiz saat dilimi.')
        if (result.get('telegram_bot_token') and
            result['telegram_bot_token'] != store.get_settings().get('telegram_bot_token')):
            store.set_meta('telegram_polling_checked', False)
            store.set_meta('telegram_offset', 0)
            store.set_meta('telegram_claim', None)
            result['telegram_chat_id'] = ''
            result['telegram_user_id'] = ''
            store.invalidate_actions()
        return result

    def session_response(request, extra=None):
        token, csrf = store.new_session()
        response = JSONResponse({'ok': True, 'csrf_token': csrf, **(extra or {})})
        response.set_cookie('jarvis_session', token, httponly=True, samesite='lax',
                            secure=store.get_settings()['public_url'].startswith('https://'), max_age=86400)
        return response

    @app.get('/healthz')
    async def health():
        with store.db() as c:
            c.execute('SELECT 1').fetchone()
        return {'status': 'ok', 'service': 'jarvis', 'version': '1.0.0'}

    @app.get('/')
    async def index():
        return FileResponse(Path(__file__).parent / 'static' / 'index.html')

    @app.get('/api/state')
    async def state(request: Request):
        session = store.session(request.cookies.get('jarvis_session'))
        if not session:
            return {'authenticated': False, 'configured': bool(store.get_meta('password'))}
        settings, presence = store.public_settings()
        return {'authenticated': True, 'configured': True, 'settings': settings, 'presence': presence,
                'csrf_token': session['csrf'], 'campaigns': store.list_campaigns(), 'events': store.events(),
                'connections': store.get_meta('connections', {}),
                'oauth_redirects': {'etsy': settings['public_url'] + '/auth/etsy/callback',
                                    'instagram': settings['public_url'] + '/auth/instagram/callback',
                                    'pinterest': settings['public_url'] + '/auth/pinterest/callback'}}

    @app.post('/api/login')
    async def login(request: Request):
        key = request.client.host if request.client else 'unknown'
        failures = [t for t in rate_limits.get(key, []) if time.time() - t < 900]
        if len(failures) >= 10:
            raise HTTPException(429, 'Çok fazla deneme. 15 dakika sonra tekrar deneyin.')
        data = await payload(request)
        if not store.check_password(str(data.get('password', ''))):
            rate_limits[key] = failures + [time.time()]
            raise HTTPException(401, 'Parola yanlış.')
        rate_limits.pop(key, None)
        return session_response(request)

    @app.post('/api/setup')
    async def setup(request: Request):
        if store.get_meta('password'):
            raise HTTPException(409, 'Kurulum zaten yapılmış; giriş yapın.')
        expected = os.getenv('JARVIS_SETUP_TOKEN', '')
        if not expected or not hmac.compare_digest(expected, request.headers.get('x-setup-token', '')):
            raise HTTPException(403, 'Dağıtımın JARVIS_SETUP_TOKEN değeri gerekli.')
        data = await payload(request)
        store.set_password(str(data.get('password', '')))
        store.update_settings(clean_settings(data.get('settings', {})))
        return session_response(request)

    @app.post('/api/logout')
    async def logout(request: Request):
        authorized(request, True)
        store.delete_session(request.cookies['jarvis_session'])
        response = JSONResponse({'ok': True})
        response.delete_cookie('jarvis_session')
        return response

    @app.patch('/api/settings')
    async def settings(request: Request):
        authorized(request, True)
        data = await payload(request)
        updates = clean_settings(data.get('settings', {}))
        impact_fields = {'etsy_shop_id','printify_shop_id','retail_price','printify_blueprint_id',
                         'printify_provider_id','printify_variant_ids','etsy_taxonomy_id',
                         'etsy_shipping_profile_id','etsy_readiness_state_id','production_partner_ids',
                         'instagram_account_id','pinterest_board_id','currency','language'}
        old = store.get_settings()
        if any(k in updates and updates[k] != old.get(k) for k in impact_fields):
            store.invalidate_actions()
            store.add_event('Ürün/hesap ayarları değişti; eski onaylar iptal edildi.', 'warning')
        store.update_settings(updates)
        store.add_event('Ayarlar güvenli depoya kaydedildi.')
        return {'ok': True}

    @app.post('/api/telegram/discover')
    async def discover(request: Request):
        authorized(request, True)
        client = TelegramClient(store.get_settings())
        me = await client.me()
        code = secrets.token_urlsafe(12)
        store.set_meta('telegram_claim', {'code': code, 'expires': time.time() + 600})
        return {'message': 'Bağlantıyı açın ve Başlat’a dokunun. Eşleştirme 10 dakika geçerli.',
                'code': code, 'deep_link': f'https://t.me/{me["username"]}?start={code}'}

    @app.get('/api/connections/check')
    async def connections(request: Request):
        authorized(request)
        cfg = store.get_settings()
        checks = {}
        jobs = []
        async def check(name, operation):
            try:
                result = await operation()
                checks[name] = {'ok': True, 'message': 'API erişimi doğrulandı.', 'result': result}
            except Exception as exc:
                checks[name] = {'ok': False, 'message': str(exc)}
        if cfg.get('telegram_bot_token'):
            jobs.append(check('telegram', TelegramClient(cfg).me))
        if cfg.get('printify_token'):
            async def shops():
                result = await PrintifyClient(cfg).shops()
                if len(result) == 1 and not cfg.get('printify_shop_id'):
                    store.update_settings({'printify_shop_id': str(result[0]['id'])})
                return result
            jobs.append(check('printify', shops))
        if cfg.get('etsy_access_token'):
            jobs.append(check('etsy', EtsyClient(cfg).shop))
        if cfg.get('instagram_access_token'):
            jobs.append(check('instagram', InstagramClient(cfg).profile))
        if cfg.get('pinterest_access_token'):
            jobs.append(check('pinterest', PinterestClient(cfg).profile))
        if cfg.get('openai_api_key'):
            async def models():
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.get('https://api.openai.com/v1/models',
                                         headers={'Authorization': 'Bearer ' + cfg['openai_api_key']})
                    if r.status_code != 200:
                        raise ValueError(f'OpenAI erişimi başarısız (HTTP {r.status_code}).')
                return {'message': 'Anahtar çalışıyor; ücretli görsel/ses erişimi ayrı üretimle test edilir.'}
            jobs.append(check('openai', models))
        await asyncio.gather(*jobs)
        # Return account metadata, not entire upstream objects or credential-bearing responses.
        safe = {}
        for name, value in checks.items():
            safe[name] = {'ok': value['ok'], 'message': value['message']}
        store.set_meta('connections', safe)
        return {'connections': safe}

    @app.get('/api/products/catalog')
    async def catalog(request: Request, blueprint_id: int | None = None, provider_id: int | None = None):
        authorized(request)
        client = PrintifyClient(store.get_settings())
        if blueprint_id and provider_id:
            return {'variants': await client.variants(blueprint_id, provider_id)}
        if blueprint_id:
            return {'providers': await client.providers(blueprint_id)}
        return {'shops': await client.shops(), 'blueprints': await client.blueprints()}

    @app.get('/api/etsy/options')
    async def etsy_options(request: Request):
        authorized(request)
        client = EtsyClient(store.get_settings())
        options = await client.get_shop_options()
        options['taxonomy'] = await client.seller_taxonomy()
        return options

    @app.get('/api/pinterest/boards')
    async def pinterest_boards(request: Request):
        authorized(request)
        return await PinterestClient(store.get_settings()).boards()

    @app.post('/api/run')
    async def run(request: Request):
        authorized(request, True)
        body = await payload(request)
        cfg = store.get_settings()
        if cfg.get('paused'):
            raise ValueError('Asistan duraklatılmış.')
        if body.get('dry_run', cfg.get('dry_run', True)):
            store.add_event('DRY_RUN: üretim/yayın yapılmadı; yalnız bağlantıları test edin. Canlı üretim ayrıca başlatılır.')
            if cfg.get('telegram_bot_token') and cfg.get('telegram_chat_id'):
                await TelegramClient(cfg).send_message('Jarvis bağlantı denemesi. Bu işlem tasarım üretmedi veya ürün yayımlamadı.')
            return {'ok': True, 'message': 'Bağlantı denemesi tamamlandı; ücretli üretim ve yayın yapılmadı.'}
        if not cfg.get('openai_api_key'):
            raise ValueError('Önce OpenAI API anahtarını girin.')
        if cfg.get('dry_run', True):
            raise ValueError('Gerçek üretim için Tercihler bölümünde deneme modunu kapatın.')
        if not cfg.get('telegram_bot_token') or not cfg.get('telegram_chat_id'):
            raise ValueError('Önce Telegram botunu güvenli eşleştirmeyle bağlayın.')
        active = [c for c in store.list_campaigns() if c['status'] not in ('completed','rejected','failed','dismissed','cancelled')]
        if active:
            raise ValueError('Önceki paket bekliyor. Tamamlayın veya açıkça kapatın.')
        await refresh_etsy()
        launch(engine.run_campaign(), 'Elle başlatılan araştırma')
        return {'ok': True, 'message': 'Gerçek araştırma kuyruğa alındı; sonuçlar ekranda ve bağlı Telegram’da görünecek.'}

    @app.post('/api/actions/{token}')
    async def action(token: str, request: Request):
        authorized(request, True)
        body = await payload(request)
        if body.get('decision') not in ('approve', 'reject'):
            raise ValueError('Geçersiz karar.')
        action = store.get_action(token)
        if not action or action['status'] != 'pending' or action['expires'] < time.time():
            raise ValueError('Onay kullanılmış veya süresi dolmuş.')
        launch(engine.decide(token, body['decision']), 'Yönetim ekranı kararı')
        return {'ok': True, 'message': 'İşlem başladı; sonuç iş günlüğünde ve Telegram’da görünecek.'}

    @app.post('/api/campaigns/{campaign_id}/dismiss')
    async def dismiss(campaign_id: str, request: Request):
        authorized(request, True)
        campaign = store.get_campaign(campaign_id)
        if campaign['status'] == 'processing':
            raise ValueError('İşlem sürüyor. Önce duraklatın ve tamamlanmasını bekleyin.')
        store.invalidate_actions(campaign_id)
        store.update_campaign(campaign_id, status='dismissed')
        store.add_event('Paket kapatıldı; uzak ürünler kendiliğinden silinmedi.', campaign_id=campaign_id)
        return {'ok': True}

    @app.post('/api/pause')
    async def pause(request: Request):
        authorized(request, True)
        body = await payload(request)
        paused = bool(body.get('paused', True))
        store.update_settings({'paused': paused})
        if paused:
            store.invalidate_actions()
        return {'ok': True, 'paused': paused}

    @app.post('/api/voice-test')
    async def voice_test(request: Request):
        authorized(request, True)
        cfg = store.get_settings()
        audio = await OpenAIClient(cfg).speech('Merhaba. Ben Jarvis. Araştırma ve tasarımlarını hazırlayacağım. '
                                                'Onayın gelmeden ürün veya sosyal paylaşım yayımlamayacağım.')
        asset_id = store.save_asset(audio, 'audio/mpeg', 'jarvis-ses-testi.mp3')
        if cfg.get('telegram_chat_id') and cfg.get('telegram_bot_token'):
            await TelegramClient(cfg).send_audio(audio, caption='Jarvis ses testi — yapay zekâ sesi')
        return {'ok': True, 'url': '/api/assets/' + asset_id, 'message': 'Ses oluşturuldu.'}

    @app.get('/api/assets/{asset_id}')
    async def asset(asset_id: str, request: Request):
        authorized(request)
        content, mime, filename = store.read_asset(asset_id)
        return Response(content, media_type=mime)

    @app.get('/media/{asset_id}')
    async def media(asset_id: str, expires: int, signature: str):
        if not store.verify_asset(asset_id, expires, signature):
            raise HTTPException(403, 'Dosya bağlantısı geçersiz veya süresi dolmuş.')
        content, mime, _ = store.read_asset(asset_id)
        return Response(content, media_type=mime, headers={'Cache-Control': 'private, max-age=60'})

    def oauth_state(request, provider, extra=None):
        session = authorized(request)
        state = secrets.token_urlsafe(24)
        data = {'provider': provider, 'expires': time.time() + 600, 'session': session['token'], **(extra or {})}
        store.set_meta('oauth:' + state, store.fernet.encrypt(json.dumps(data).encode()).decode())
        return state

    def validate_oauth(request, state, provider):
        session = authorized(request)
        encrypted = store.get_meta('oauth:' + state)
        if not encrypted:
            raise HTTPException(400, 'OAuth bağlantısı geçersiz veya kullanılmış.')
        data = json.loads(store.fernet.decrypt(encrypted.encode()))
        if data['expires'] < time.time() or data['provider'] != provider or data['session'] != session['token']:
            raise HTTPException(400, 'OAuth oturumu doğrulanamadı. Bağlantıyı yeniden başlatın.')
        store.set_meta('oauth:' + state, None)
        return data

    @app.get('/auth/etsy/start')
    async def etsy_start(request: Request):
        cfg = store.get_settings()
        if not cfg.get('etsy_key'):
            raise ValueError('Önce Etsy uygulama anahtarını kaydedin.')
        verifier = secrets.token_urlsafe(48)
        state = oauth_state(request, 'etsy', {'verifier': verifier})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {'response_type': 'code', 'client_id': cfg['etsy_key'],
                  'redirect_uri': cfg['public_url'] + '/auth/etsy/callback',
                  'scope': 'listings_r listings_w shops_r', 'state': state,
                  'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return RedirectResponse('https://www.etsy.com/oauth/connect?' + urlencode(params))

    @app.get('/auth/etsy/callback')
    async def etsy_callback(request: Request, state: str, code: str = '', error: str = ''):
        data = validate_oauth(request, state, 'etsy')
        if error or not code:
            raise ValueError('Etsy yetkilendirmesi tamamlanmadı. Yeniden bağlayın.')
        cfg = store.get_settings()
        tokens = await EtsyClient(cfg).exchange_code(code, cfg['public_url'] + '/auth/etsy/callback', data['verifier'])
        store.update_settings({'etsy_access_token': tokens['access_token'], 'etsy_refresh_token': tokens['refresh_token'],
                               'etsy_token_expires_at': time.time() + tokens.get('expires_in', 3600)})
        store.invalidate_actions()
        try:
            # Etsy tokens have a documented numerical user-id prefix; discover the shop from that account.
            user_id = tokens['access_token'].split('.')[0]
            if user_id.isdigit():
                shops = await EtsyClient(store.get_settings()).request('GET', f'/users/{user_id}/shops')
                items = shops.get('results', [shops] if shops.get('shop_id') else [])
                if len(items) == 1:
                    store.update_settings({'etsy_shop_id': str(items[0]['shop_id'])})
        except Exception:
            store.add_event('Etsy yetkilendirildi. Mağaza kimliği otomatik okunamadı; ayarlarda tamamlayın.', 'warning')
        store.add_event('Etsy OAuth bağlantısı tamamlandı.')
        return RedirectResponse('/?connected=etsy')

    @app.get('/auth/pinterest/start')
    async def pinterest_start(request: Request):
        cfg = store.get_settings()
        if not cfg.get('pinterest_client_id'):
            raise ValueError('Pinterest uygulama kimliğini önce kaydedin.')
        state = oauth_state(request, 'pinterest')
        params = {'response_type': 'code', 'client_id': cfg['pinterest_client_id'],
                  'redirect_uri': cfg['public_url'] + '/auth/pinterest/callback',
                  'scope': 'boards:read,boards:write,pins:read,pins:write,user_accounts:read', 'state': state}
        return RedirectResponse('https://www.pinterest.com/oauth/?' + urlencode(params))

    @app.get('/auth/instagram/start')
    async def instagram_start(request: Request):
        cfg = store.get_settings()
        if not cfg.get('instagram_client_id'):
            raise ValueError('Önce Instagram uygulama kimliğini kaydedin.')
        state = oauth_state(request, 'instagram')
        params = {'client_id': cfg['instagram_client_id'], 'response_type': 'code',
                  'redirect_uri': cfg['public_url'] + '/auth/instagram/callback',
                  'scope': 'instagram_business_basic,instagram_business_content_publish',
                  'state': state, 'enable_fb_login': '0', 'force_authentication': '1'}
        return RedirectResponse('https://www.instagram.com/oauth/authorize?' + urlencode(params))

    @app.get('/auth/instagram/callback')
    async def instagram_callback(request: Request, state: str, code: str = '', error: str = ''):
        validate_oauth(request, state, 'instagram')
        if error or not code:
            raise ValueError('Instagram yetkilendirmesi tamamlanmadı.')
        cfg = store.get_settings()
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post('https://api.instagram.com/oauth/access_token',
                data={'client_id': cfg.get('instagram_client_id',''),
                      'client_secret': cfg.get('instagram_client_secret',''),
                      'grant_type': 'authorization_code', 'redirect_uri': cfg['public_url'] + '/auth/instagram/callback',
                      'code': code})
            if r.status_code != 200:
                raise ValueError(f'Instagram token işlemi başarısız (HTTP {r.status_code}).')
            short = r.json()
            r = await client.get('https://graph.instagram.com/access_token',
                params={'grant_type': 'ig_exchange_token', 'client_secret': cfg.get('instagram_client_secret',''),
                        'access_token': short['access_token']})
            if r.status_code != 200:
                raise ValueError(f'Instagram uzun süreli token alınamadı (HTTP {r.status_code}).')
            token = r.json()
        store.update_settings({'instagram_access_token': token['access_token'],
            'instagram_account_id': str(short['user_id']), 'instagram_login_type': 'instagram',
            'instagram_token_expires_at': time.time() + token.get('expires_in', 5184000)})
        store.invalidate_actions()
        store.add_event('Instagram OAuth bağlantısı tamamlandı.')
        return RedirectResponse('/?connected=instagram')

    @app.get('/auth/pinterest/callback')
    async def pinterest_callback(request: Request, state: str, code: str = '', error: str = ''):
        validate_oauth(request, state, 'pinterest')
        if error or not code:
            raise ValueError('Pinterest yetkilendirmesi tamamlanmadı.')
        cfg = store.get_settings()
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post('https://api.pinterest.com/v5/oauth/token',
                    auth=(cfg.get('pinterest_client_id', ''), cfg.get('pinterest_client_secret', '')),
                    data={'grant_type': 'authorization_code', 'code': code,
                          'redirect_uri': cfg['public_url'] + '/auth/pinterest/callback'})
        if r.status_code != 200:
            raise ValueError(f'Pinterest token işlemi başarısız (HTTP {r.status_code}).')
        token = r.json()
        store.update_settings({'pinterest_access_token': token['access_token'],
                               'pinterest_refresh_token': token.get('refresh_token', ''),
                               'pinterest_token_expires_at': time.time() + token.get('expires_in', 0)})
        store.invalidate_actions()
        store.add_event('Pinterest OAuth bağlantısı tamamlandı.')
        return RedirectResponse('/?connected=pinterest')

    return app


app = create_app()
