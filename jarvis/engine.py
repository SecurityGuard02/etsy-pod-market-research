"""Durable, approval-gated POD workflow.

Every remote mutation follows a single-use action. An interrupted or failed
mutation is never automatically retried: its remote outcome may be uncertain.
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from . import providers


class Engine:
    def __init__(self, store, clients: dict[str, Any] | None = None):
        self.store = store
        self.processing = asyncio.Lock()
        self._clients = clients

    def client(self, name: str):
        if self._clients is not None:
            return self._clients[name]
        classes = {"ai": providers.OpenAIClient, "etsy": providers.EtsyClient,
                   "printify": providers.PrintifyClient, "telegram": providers.TelegramClient,
                   "instagram": providers.InstagramClient, "pinterest": providers.PinterestClient}
        return classes[name](self.store.get_settings())

    def settings(self) -> dict:
        config = dict(self.store.get_settings())
        for canonical, alias in (("etsy_api_key", "etsy_key"), ("etsy_api_secret", "etsy_secret"),
                                 ("printify_api_token", "printify_token"),
                                 ("instagram_user_id", "instagram_account_id"),
                                 ("etsy_production_partner_ids", "production_partner_ids")):
            if not config.get(canonical) and config.get(alias):
                config[canonical] = config[alias]
        return config

    def _live(self):
        if self.settings().get("paused"):
            raise ValueError("Asistan duraklatılmış; önce panelden devam ettirin.")
        if self.settings().get("dry_run", True):
            raise ValueError("Canlı çalışma kapalı. Bağlantı testinden sonra ayarlardan kuru denemeyi kapatın.")

    def _save(self, campaign: dict, stage: str | None = None, status: str | None = None):
        self.store.update_campaign(campaign["id"], data=campaign["data"],
                                   **({"stage": stage} if stage else {}),
                                   **({"status": status} if status else {}))

    def _require(self, *keys: str):
        missing = [k for k in keys if not self.settings().get(k)]
        if missing:
            raise ValueError("Ayarlar bölümünde tamamlayın: " + ", ".join(missing))

    def _ai_use(self, campaign: dict, kind: str):
        self._live()
        usage = campaign["data"].setdefault("usage", {})
        ceiling = min(int(self.settings().get("ai_call_limit", 24)), 40)
        if usage.get("ai_calls", 0) >= ceiling:
            raise ValueError("Bu araştırmanın yapay zekâ çağrı sınırına ulaşıldı.")
        if kind == "design" and usage.get("design", 0) >= min(int(self.settings().get("image_generation_limit", 5)), 5):
            raise ValueError("Bu araştırma için beş özgün görsel sınırına ulaşıldı.")
        if kind == "social" and usage.get("social", 0) >= 5:
            raise ValueError("Bu araştırma için beş sosyal görsel sınırına ulaşıldı.")
        # Count before sending: even an ambiguous timeout may have incurred cost.
        usage["ai_calls"] = usage.get("ai_calls", 0) + 1
        usage[kind] = usage.get(kind, 0) + 1
        self._save(campaign)

    async def _notify(self, campaign: dict, text: str, *, kind: str | None = None,
                      payload: dict | None = None, label: str = "", image: bytes | None = None):
        token = None
        markup = None
        if kind and self.settings().get("paused"):
            self.store.add_event("Asistan duraklatıldığı için yeni onay oluşturulmadı: " + kind,
                                 level="warning", campaign_id=campaign["id"])
            text += "\n\nAsistan duraklatıldı; bu sonuç için yeni onay bağlantısı oluşturulmadı."
            kind = None
        if kind:
            payload = dict(payload or {})
            payload["preview_text"] = text
            if payload.get("design_id"):
                design = self._design(campaign, payload["design_id"])
                if kind in {"etsy_draft", "etsy_publish"}:
                    payload["preview_asset_id"] = design.get("mockup_asset_id")
                elif kind in {"instagram_publish", "pinterest_publish"}:
                    payload["preview_asset_id"] = design.get("social", {}).get("asset_id")
                else:
                    payload["preview_asset_id"] = design.get("asset_id")
            token = self.store.create_action(campaign["id"], kind, payload, label or text[:100])
            markup = {"inline_keyboard": [[{"text": "Onayla", "callback_data": "approve:" + token},
                                           {"text": "Reddet", "callback_data": "reject:" + token}]]}
        # The dashboard remains usable even when Telegram delivery fails. Never
        # run the approved operation merely because its notification failed.
        try:
            telegram = self.client("telegram")
            if image is not None:
                await telegram.send_photo(image, caption=text[:1000], reply_markup=markup)
                if len(text) > 1000:
                    await telegram.send_message(text[1000:4900])
            else:
                for start in range(0, len(text), 3900):
                    await telegram.send_message(text[start:start + 3900],
                                                reply_markup=markup if start == 0 else None)
        except Exception as exc:
            self.store.add_event("Telegram teslim edilemedi; onay panelde bekliyor. " + self._error(exc),
                                 level="warning", campaign_id=campaign["id"])
        return token

    def _error(self, exc: Exception) -> str:
        message = str(exc)
        for key, value in self.settings().items():
            if value and any(s in key.lower() for s in ("token", "secret", "api_key", "password")):
                message = message.replace(str(value), "[gizli]")
        return message[:700]

    async def _failed(self, campaign: dict, exc: Exception, *, uncertain: bool = False):
        message = self._error(exc)
        campaign["data"]["last_error"] = message
        self._save(campaign, status="uncertain" if uncertain else "blocked")
        self.store.add_event(message, level="error", campaign_id=campaign["id"])
        suffix = (" Uzak işlem tamamlanmış olabilir. Platformdaki kaydı kontrol edin; "
                  "aynı işlem otomatik tekrarlanmaz." if uncertain else " Onay panelindeki ayrıntıları kontrol edin.")
        await self._notify(campaign, "JARVIS işlemi durdurdu: " + message + suffix)

    async def run_campaign(self):
        async with self.processing:
            self._live()
            active = [c for c in self.store.list_campaigns()
                      if c.get("status") not in {"completed", "rejected", "dismissed"}]
            if active:
                raise ValueError("Önce devam eden araştırmayı tamamlayın veya panelden kapatın.")
            self._require("openai_api_key", "telegram_bot_token", "telegram_chat_id")
            if int(self.settings().get("image_generation_limit", 5)) < 5:
                raise ValueError("Beş tasarım için image_generation_limit en az 5 olmalı.")
            title = "POD araştırması · " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            cid = self.store.create_campaign(title, {"designs": [], "usage": {}, "notices": []})
            campaign = self.store.get_campaign(cid)
            self._save(campaign, stage="research", status="processing")
            try:
                query = str(self.settings().get("niche", "original minimalist apparel"))
                observations = []
                if self.settings().get("etsy_api_key"):
                    try:
                        observations = await self.client("etsy").search_listings(query, limit=25)
                    except Exception as exc:
                        campaign["data"]["notices"].append("Etsy API gözlemi alınamadı: " + self._error(exc))
                self._ai_use(campaign, "research")
                report = await self.client("ai").research(query, etsy_listings=observations)
                if len(report.get("designs", [])) != 5:
                    raise ValueError("Araştırma tam beş özgün tasarım fikri içermeli.")
                campaign["data"]["research"] = report
                self._save(campaign, stage="research_review", status="awaiting_approval")
                sources = report.get("web_sources") or report.get("sources") or []
                source_text = "\n".join(str(s.get("url", "")) for s in sources[:8] if isinstance(s, dict))
                ideas = "\n".join(f"{i+1}. {d.get('title', 'Tasarım')} — {d.get('rationale', '')}"
                                  for i, d in enumerate(report["designs"]))
                limitations = "\n".join(map(str, report.get("limitations", [])))
                text = ("Araştırma hazır\n\n" + str(report.get("summary", "")) + "\n\n" + ideas +
                        "\n\nKaynaklar:\n" + source_text + "\n\nVeri sınırları:\n" + limitations +
                        "\n\nOnayınızla beş özgün görsel üretilecek. Görsel ve API ücretleri oluşur. "
                        "Görseller basılmadan önce baskı kalitesini ve haklarını inceleyin.")
                await self._notify(campaign, text, kind="generate", label="Araştırmayı onayla; beş görsel üret")
                if self.settings().get("voice_enabled", True):
                    try:
                        self._ai_use(campaign, "speech")
                        speech = await self.client("ai").speech("Yapay zekâ sesli özet. " + str(report.get("summary", ""))[:1600])
                        await self.client("telegram").send_audio(speech, caption="JARVIS — yapay zekâ sesli özet")
                    except Exception as exc:
                        self.store.add_event("Sesli özet alınamadı: " + self._error(exc), "warning", cid)
                return self.store.get_campaign(cid)
            except Exception as exc:
                await self._failed(campaign, exc)
                raise

    async def decide(self, token: str, decision: str, actor_chat=None):
        if decision not in {"approve", "reject"}:
            raise ValueError("Karar approve veya reject olmalı.")
        async with self.processing:
            action = self.store.get_action(token)
            if not action:
                raise ValueError("Onay bulunamadı veya artık geçerli değil.")
            campaign = self.store.get_campaign(action["campaign_id"])
            if campaign.get("status") in {"dismissed", "rejected", "completed"}:
                raise ValueError("Bu araştırma kapatılmış.")
            kind = action["kind"]
            preflight = None
            # Check local prerequisites without using up an approval token.
            if decision == "approve":
                self._live()
                if actor_chat is not None and str(actor_chat) != str(self.settings().get("telegram_chat_id")):
                    raise ValueError("Bu sohbet onay vermeye yetkili değil.")
                if kind in {"choose_design", "printify_create"}:
                    self._require("printify_api_token")
                if kind in {"etsy_draft", "etsy_publish"}:
                    self._require("etsy_api_key", "etsy_access_token", "etsy_shop_id")
                if kind == "etsy_draft":
                    preflight = await self._etsy_preflight()
                if kind == "instagram_publish":
                    self._require("instagram_access_token", "instagram_user_id")
                    if not self.store.asset_url(action["payload"]["asset_id"]).startswith("https://"):
                        raise ValueError("Instagram için uygulamanın public_url ayarı herkese açık HTTPS olmalı.")
                if kind == "pinterest_publish":
                    self._require("pinterest_access_token", "pinterest_board_id")
            action = self.store.consume_action(token, decision, actor_chat=actor_chat)
            if decision == "reject":
                design_id = action.get("payload", {}).get("design_id")
                if design_id:
                    self._design(campaign, design_id)[kind + "_decision"] = "rejected"
                self._save(campaign, status="awaiting_approval" if self.store.list_actions(campaign["id"]) else "rejected")
                await self._notify(campaign, "İşlem reddedildi: " + str(action.get("label", kind)))
                return {"status": "rejected"}
            self._save(campaign, stage=kind, status="processing")
            mutations = {"printify_create", "etsy_draft", "etsy_publish", "instagram_publish", "pinterest_publish"}
            try:
                if kind == "generate":
                    await self._generate(campaign)
                elif kind == "choose_design":
                    await self._plan(campaign, action["payload"]["design_id"])
                elif kind == "printify_create":
                    await self._printify(campaign, action["payload"]["design_id"])
                elif kind == "etsy_draft":
                    await self._etsy_draft(campaign, action["payload"]["design_id"], preflight)
                elif kind == "etsy_publish":
                    await self._etsy_publish(campaign, action["payload"]["design_id"])
                elif kind == "social_prepare":
                    await self._social(campaign, action["payload"]["design_id"])
                elif kind in {"instagram_publish", "pinterest_publish"}:
                    await self._publish_social(campaign, action, kind)
                else:
                    raise ValueError("Bilinmeyen onay türü.")
                pending = self.store.list_actions(campaign["id"])
                self._save(campaign, status="awaiting_approval" if pending else "completed")
                return {"status": "ok", "campaign_id": campaign["id"]}
            except Exception as exc:
                await self._failed(campaign, exc, uncertain=kind in mutations)
                raise

    @staticmethod
    def _design(campaign: dict, design_id: str) -> dict:
        for design in campaign["data"]["designs"]:
            if design["id"] == design_id:
                return design
        raise ValueError("Tasarım bulunamadı.")

    async def _generate(self, campaign: dict):
        for index, concept in enumerate(campaign["data"]["research"]["designs"]):
            self._ai_use(campaign, "design")
            image = await self.client("ai").generate_image(concept["prompt"])
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 24:
                raise ValueError("Görsel gerçek PNG değil; ürün aşamasına aktarılmadı.")
            aid = self.store.save_asset(image, "image/png", f"design-{index+1}.png")
            width, height = struct.unpack(">II", image[16:24])
            design = {"id": str(index + 1), "concept": concept, "asset_id": aid,
                      "source_dimensions": [width, height], "title": concept.get("title", ""),
                      "rationale": concept.get("rationale", ""), "palette": concept.get("palette", [])}
            campaign["data"]["designs"].append(design)
            self._save(campaign, stage="design_review")
            await self._notify(campaign, f"Tasarım {index+1}/5: {concept.get('title', '')}\n"
                               f"{concept.get('rationale', '')}\n\nOnayınız bu tasarım için ürün önerisi hazırlatır.",
                               kind="choose_design", payload={"design_id": design["id"]},
                               label=f"{concept.get('title', '')}: tasarımı seç", image=image)

    async def _plan(self, campaign: dict, design_id: str):
        design = self._design(campaign, design_id)
        catalog = self.client("printify")
        config = self.settings()
        if not config.get("printify_shop_id"):
            shops = await catalog.shops()
            if len(shops) != 1:
                raise ValueError("Printify mağazasını ayarlardan seçin (printify_shop_id); mağaza sayısı: " + str(len(shops)))
            self.store.update_settings({"printify_shop_id": str(shops[0]["id"])})
            catalog = self.client("printify")
        blueprints = await catalog.blueprints()
        selected = config.get("printify_blueprint_id")
        if selected:
            blueprint = next((b for b in blueprints if str(b["id"]) == str(selected)), None)
        else:
            def rank(b):
                title = b.get("title", "").lower()
                return ("unisex" in title, "jersey" in title, "short sleeve" in title)
            shirts = [b for b in blueprints if re.search(r"t[- ]?shirt|\btee\b", b.get("title", ""), re.I)
                      and "kid" not in b.get("title", "").lower() and "youth" not in b.get("title", "").lower()]
            blueprint = sorted(shirts, key=rank, reverse=True)[0] if shirts else None
        if not blueprint:
            raise ValueError("Gerçek katalogda uygun yetişkin tişörtü bulunamadı; printify_blueprint_id seçin.")
        provider_list = await catalog.providers(blueprint["id"])
        if config.get("printify_provider_id"):
            provider_list = [p for p in provider_list if str(p["id"]) == str(config["printify_provider_id"])]
        country = str(config.get("target_country", "US")).upper()
        provider_list.sort(key=lambda p: str(p.get("location", {}).get("country", "")).upper() == country, reverse=True)
        candidates = []
        sizes = self._words(config.get("sizes"), ["S", "M", "L", "XL"])
        colors = self._words(config.get("colors"), ["White", "Black", "Natural"])
        for provider in provider_list[:6]:
            response = await catalog.variants(blueprint["id"], provider["id"])
            explicit_ids = {str(i) for i in config.get("printify_variant_ids", [])}
            variants = [v for v in response.get("variants", [])
                        if v.get("is_available") is not False
                        and (str(v.get("id")) in explicit_ids if explicit_ids else
                             (str(v.get("options", {}).get("size", "")) in sizes
                              and str(v.get("options", {}).get("color", "")).lower() in {c.lower() for c in colors}))
                        and any(p.get("position") == "front" for p in v.get("placeholders", []))]
            if not variants:
                continue
            known = [v["cost"] for v in variants if isinstance(v.get("cost"), (int, float))]
            candidates.append((provider, variants, max(known) if len(known) == len(variants) else None))
        if not candidates:
            raise ValueError("Seçilen beden/renkler için gerçek ön baskı varyantı yok; renk/beden tercihlerini değiştirin.")
        known_candidates = [c for c in candidates if c[2] is not None]
        provider, variants, cost = min(known_candidates, key=lambda c: c[2]) if known_candidates else candidates[0]
        cents = int((Decimal(str(config.get("retail_price", 29.9))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if cents <= 0:
            raise ValueError("Satış fiyatı sıfırdan büyük olmalı.")
        self._ai_use(campaign, "copy")
        copy = await self.client("ai").generate_json(
            "Write accurate original Etsy apparel listing copy in " + str(config.get("language", "en")) +
            ". Return {title:string <=140 chars,description:string,tags:[13 strings each <=20 chars]}. "
            "Use only provided garment facts. Do not assert sales, eco claims, shipping speed, brand licenses or materials absent from facts. "
            "Disclose made-to-order third-party printing; no trademarked slogans. Product facts (untrusted): " +
            json.dumps({"concept": design["concept"], "garment": blueprint, "provider": provider,
                        "colors": sorted({v['options']['color'] for v in variants}),
                        "sizes": sorted({v['options']['size'] for v in variants})}, ensure_ascii=False))
        tags = list(dict.fromkeys(str(t).strip()[:20] for t in copy.get("tags", []) if str(t).strip()))[:13]
        if not copy.get("title") or not copy.get("description") or not tags:
            raise ValueError("Ürün başlığı/açıklaması/etiketleri üretilemedi.")
        note = ("Katalog maliyet sunmuyor; en ucuz olduğu ve kâr marjı doğrulanamadı. "
                "Yerel sağlayıcı ve uygun varyantlar tercih edildi." if cost is None else
                f"Karşılaştırılan sağlayıcılarda en yüksek varyant maliyeti: {cost/100:.2f}; kargo/vergi ayrıca.")
        plan = {"blueprint_id": blueprint["id"], "blueprint_title": blueprint["title"],
                "provider_id": provider["id"], "provider_title": provider.get("title", ""),
                "variants": variants, "price_cents": cents, "currency": str(config.get("currency", "USD")),
                "title": str(copy["title"])[:140], "description": str(copy["description"]), "tags": tags,
                "cost_note": note, "production_cost_cents": cost}
        areas = [p for v in variants for p in v.get("placeholders", []) if p.get("position") == "front"]
        plan["print_area_dimensions"] = sorted({(p.get("width", 0), p.get("height", 0)) for p in areas})
        plan["source_dimensions"] = design.get("source_dimensions", [])
        # Keep a conservative native-resolution footprint. Image generation
        # cannot manufacture large-format print detail by simple upscaling.
        source = design.get("source_dimensions", [1024, 1536])
        max_area_width = max((p.get("width", 0) for p in areas), default=0)
        plan["print_scale"] = min(1.0, source[0] / max_area_width) if max_area_width else 0.25
        plan["resolution_note"] = (f"Kaynak: {source[0]}×{source[1]} piksel. Ön baskı alanları: "
                                   f"{plan['print_area_dimensions']}. Büyük baskı ayrıntısı üretilmedi; "
                                   "doğal çözünürlüğe yakın küçük merkez baskı uygulanacak. Mockup ve fiziksel örnekle kaliteyi kontrol edin.")
        design["plan"] = plan
        self._save(campaign, stage="product_review")
        await self._notify(campaign, "Printify ürün önerisi\n" + plan["title"] + "\n" + blueprint["title"] +
                           " · " + provider.get("title", "") + "\n" + f"Fiyat: {cents/100:.2f} {plan['currency']}\n" +
                           "Varyantlar: " + ", ".join(v.get("title", str(v["id"])) for v in variants) +
                           "\nEtiketler: " + ", ".join(tags) + "\n\n" + plan["description"] + "\n\n" + note +
                           "\n\n" + plan["resolution_note"] +
                           "\nOnayınız görseli Printify'a yükler ve yayınlanmamış ürün oluşturur.",
                           kind="printify_create", payload={"design_id": design_id}, label="Printify ürününü oluştur")

    @staticmethod
    def _words(value, default):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [v.strip() for v in str(value).split(",") if v.strip()] if value else default

    async def _printify(self, campaign: dict, design_id: str):
        design = self._design(campaign, design_id)
        plan = design["plan"]
        image, _, filename = self.store.read_asset(design["asset_id"])
        catalog = self.client("printify")
        self._live()
        upload = await catalog.upload_image(filename, image)
        design["printify_upload_id"] = upload["id"]
        self._save(campaign)
        self._live()
        variants = [{"id": int(v["id"]), "price": plan["price_cents"], "is_enabled": True}
                    for v in plan["variants"]]
        product = await catalog.create_product({"title": plan["title"], "description": plan["description"],
            "blueprint_id": int(plan["blueprint_id"]), "print_provider_id": int(plan["provider_id"]),
            "tags": plan["tags"], "variants": variants,
            "print_areas": [{"variant_ids": [v["id"] for v in variants], "placeholders": [{"position": "front",
                "images": [{"id": upload["id"], "x": 0.5, "y": 0.5, "scale": plan.get("print_scale", 0.25), "angle": 0}]}]}]})
        design["printify_id"] = product["id"]
        self._save(campaign)
        product_id = product["id"]
        mockups = []
        # Printify prepares mockups asynchronously. Poll only the already
        # persisted product; never repeat upload/create on a normal delay.
        for attempt in range(5):
            self._live()
            product = await catalog.get_product(product_id)
            design["printify_product"] = product
            self._save(campaign)
            mockups = [m for m in product.get("images", []) if m.get("src", "").startswith("https://")]
            if mockups:
                break
            if attempt < 4:
                self._live()
                await asyncio.sleep(2)
        if not mockups:
            raise ValueError("Printify ürün oluşturdu ancak giysi mockup'ı henüz yok. Ürün kimliği kaydedildi; tekrar oluşturmayın.")
        design["mockup_url"] = next((m["src"] for m in mockups if m.get("is_default")), mockups[0]["src"])
        image = await providers.download_image(design["mockup_url"])
        design["mockup_asset_id"] = self.store.save_asset(image, "image/png", f"mockup-{design_id}.png")
        self._save(campaign, stage="printify_review")
        await self._notify(campaign, "Printify taslağı hazır: " + str(product["id"]) + "\n" + plan["title"] +
                           "\nGiysi baskısı, renkler ve konumu kontrol edin.\n\n" +
                           "Onayınız Etsy'de yayınlanmamış taslak ve beden/renk varyantlarını oluşturur. "
                           "Etsy ücretleri oluşabilir. Printify sipariş eşlemesi otomatik doğrulanmadı; "
                           "sipariş üretim/gönderim takibi manuel yapılmalıdır.",
                           kind="etsy_draft", payload={"design_id": design_id}, label="Etsy taslağını oluştur", image=image)

    async def _etsy_options(self) -> dict:
        config = self.settings()
        etsy = self.client("etsy")
        options = {}
        if config.get("etsy_readiness_state_id"):
            options["readiness_state_id"] = int(config["etsy_readiness_state_id"])
        else:
            try:
                states = (await etsy.readiness_states()).get("results", [])
            except Exception as exc:
                raise ValueError("Etsy üretim süresi tanımı alınamadı; etsy_readiness_state_id ayarını tamamlayın.") from exc
            if len(states) != 1:
                raise ValueError("Etsy üretim süresi tanımını ayarlardan seçin: etsy_readiness_state_id.")
            options["readiness_state_id"] = int(states[0]["readiness_state_id"])
        if config.get("etsy_shipping_profile_id"):
            options["shipping_profile_id"] = int(config["etsy_shipping_profile_id"])
        else:
            profiles = await etsy.shipping_profiles()
            profiles = profiles.get("results", [])
            if len(profiles) != 1:
                raise ValueError("Etsy kargo profilini ayarlardan seçin: etsy_shipping_profile_id.")
            options["shipping_profile_id"] = int(profiles[0]["shipping_profile_id"])
        if config.get("etsy_taxonomy_id"):
            options["taxonomy_id"] = int(config["etsy_taxonomy_id"])
        else:
            nodes = (await etsy.seller_taxonomy()).get("results", [])
            found = []
            def visit(items):
                for node in items:
                    if node.get("name", "").lower() in {"t-shirts", "t shirts", "tshirts"}:
                        found.append(node["id"])
                    visit(node.get("children", []))
            visit(nodes)
            if len(found) != 1:
                raise ValueError("Etsy tişört kategorisini ayarlardan seçin: etsy_taxonomy_id.")
            options["taxonomy_id"] = int(found[0])
        if config.get("etsy_return_policy_id"):
            options["return_policy_id"] = int(config["etsy_return_policy_id"])
        else:
            policies = (await etsy.return_policies()).get("results", [])
            if len(policies) == 1:
                options["return_policy_id"] = int(policies[0]["return_policy_id"])
        partner_ids = self._words(config.get("etsy_production_partner_ids"), [])
        if partner_ids:
            options["production_partner_ids"] = [int(p) for p in partner_ids]
        shop = await etsy.shop()
        if shop.get("currency_code") and shop["currency_code"].upper() != str(config.get("currency", "USD")).upper():
            raise ValueError("Etsy mağaza para birimi ile currency ayarı farklı; fiyatı ve para birimini doğrulayın.")
        return options

    async def _etsy_preflight(self):
        options = await self._etsy_options()
        props = (await self.client("etsy").taxonomy_properties(options["taxonomy_id"])).get("results", [])
        color_prop = next((p for p in props if p.get("name", "").lower() in {"primary_color", "primary color", "color"}), None)
        size_prop = next((p for p in props if p.get("name", "").lower() in {"size", "clothing_size", "clothing size"}), None)
        if not color_prop or not size_prop:
            raise ValueError("Etsy kategori API'si renk/beden özelliklerini sağlamadı; taslak açılmadı.")
        if color_prop.get("supports_variations") is False or size_prop.get("supports_variations") is False:
            raise ValueError("Etsy kategorisi renk/beden varyasyonlarını desteklemiyor.")
        return options, color_prop, size_prop

    async def _etsy_draft(self, campaign: dict, design_id: str, preflight=None):
        design = self._design(campaign, design_id)
        plan = design["plan"]
        etsy = self.client("etsy")
        options, color_prop, size_prop = preflight or await self._etsy_preflight()
        # Etsy validates taxonomy-specific scale/value constraints. We never
        # silently discard the garment variations to make a request succeed.
        self._live()
        listing = await etsy.create_draft({"quantity": 999, "title": plan["title"], "description": plan["description"],
            "price": plan["price_cents"] / 100, "who_made": "someone_else", "when_made": "made_to_order",
            "is_supply": False, "type": "physical", "tags": plan["tags"], "state": "draft", **options})
        design["etsy_listing_id"] = listing["listing_id"]
        self._save(campaign)
        mockup, _, filename = self.store.read_asset(design["mockup_asset_id"])
        self._live()
        design["etsy_uploaded_image"] = await etsy.upload_image(listing["listing_id"], mockup, filename)
        self._save(campaign)
        products = []
        for variant in plan["variants"]:
            values = []
            for prop, text in ((color_prop, variant["options"]["color"]), (size_prop, variant["options"]["size"])):
                pv = {"property_id": int(prop["property_id"]), "property_name": prop.get("display_name", prop["name"]),
                      "values": [str(text)]}
                if prop.get("supports_variations") is False:
                    raise ValueError("Etsy kategorisi seçilen varyasyonu desteklemiyor; mevcut taslağı kontrol edin.")
                values.append(pv)
            products.append({"sku": f"JV-{campaign['id'][:6]}-{design_id}-{variant['id']}",
                "property_values": values, "offerings": [{"price": plan["price_cents"] / 100,
                "quantity": 999, "is_enabled": True, "readiness_state_id": options["readiness_state_id"]}]})
        property_ids = [int(color_prop["property_id"]), int(size_prop["property_id"])]
        self._live()
        await etsy.update_inventory(listing["listing_id"], {"products": products,
            "price_on_property": property_ids, "quantity_on_property": property_ids, "sku_on_property": property_ids})
        design["etsy_inventory_snapshot"] = await etsy.get_inventory(listing["listing_id"])
        actual = await etsy.get_listing(listing["listing_id"])
        if actual.get("state") != "draft":
            raise ValueError("Etsy kaydı beklenen draft durumunda değil; yayın onayı oluşturulmadı.")
        design["etsy_draft"] = actual
        design["fulfillment"] = "manual_unverified_mapping"
        self._save(campaign, stage="etsy_final_review")
        await self._notify(campaign, "SON YAYIN ONAYI\nEtsy taslak #" + str(actual["listing_id"]) +
                           "\n" + str(actual.get("title", plan["title"])) + "\n" +
                           "Fiyat: " + str(actual.get("price", plan["price_cents"] / 100)) +
                           "\nEtiketler: " + ", ".join(actual.get("tags", plan["tags"])) +
                           "\n\n" + str(actual.get("description", plan["description"])) +
                           "\n\nBeden/renkler ve başlık panelde kayıtlıdır. Onayınız ürünü Etsy'de herkese açar; "
                           "ilan ücretleri uygulanabilir. Üretim ortağı beyanınızı doğrulayın. "
                           "Otomatik sipariş aktarımı kurulmadı; siparişleri manuel yönetin.",
                           kind="etsy_publish", payload={"design_id": design_id}, label="Son kontrol: Etsy'de yayınla", image=mockup)

    async def _etsy_publish(self, campaign: dict, design_id: str):
        design = self._design(campaign, design_id)
        etsy = self.client("etsy")
        # Refuse to publish a listing changed outside this approved draft.
        actual = await etsy.get_listing(design["etsy_listing_id"])
        approved = design["etsy_draft"]
        for key in ("title", "description", "tags", "price", "shipping_profile_id", "taxonomy_id",
                    "readiness_state_id", "return_policy_id", "production_partner_ids", "who_made",
                    "when_made", "is_supply", "type", "images", "Images", "inventory", "Inventory"):
            if key in approved and actual.get(key) != approved[key]:
                raise ValueError("Etsy taslağı onaydan sonra değişmiş: " + key + ". Yeni inceleme gerekir.")
        inventory = await etsy.get_inventory(design["etsy_listing_id"])
        if inventory != design.get("etsy_inventory_snapshot"):
            raise ValueError("Etsy beden/renk/fiyat envanteri onaydan sonra değişmiş. Yeni inceleme gerekir.")
        if actual.get("state") != "draft":
            raise ValueError("Yayınlanacak kayıt draft durumunda değil; tekrar yayınlanmadı.")
        self._live()
        await etsy.publish_listing(design["etsy_listing_id"])
        actual = await etsy.get_listing(design["etsy_listing_id"])
        if actual.get("state") != "active":
            raise ValueError("Etsy active durumu doğrulanamadı; uzak kaydı kontrol edin.")
        design["etsy_live"] = actual
        design["etsy_url"] = actual.get("url") or f"https://www.etsy.com/listing/{int(design['etsy_listing_id'])}"
        self._save(campaign, stage="live")
        await self._notify(campaign, "Etsy ürünü yayında: " + design["etsy_url"] +
                           "\nOnayınızla aynı giysi mockup'ından farklı, yetişkin sentetik mankenli tanıtım görseli "
                           "ve sosyal başlıklar hazırlanır. Görsel üretim ücreti oluşur. Son görseli yayınlamadan onaylayacaksınız.",
                           kind="social_prepare", payload={"design_id": design_id}, label="Sosyal görselleri hazırla")

    async def _social(self, campaign: dict, design_id: str):
        design = self._design(campaign, design_id)
        mockup, _, filename = self.store.read_asset(design["mockup_asset_id"])
        used = self.store.get_meta("model_counter") or 0
        try:
            used = int(used)
        except (TypeError, ValueError):
            used = 0
        self.store.set_meta("model_counter", used + 1)
        appearances = ["adult male, warm brown skin, short curly black hair", "adult female, light olive skin, shoulder-length auburn hair",
                       "adult male, medium olive skin, wavy dark hair", "adult female, dark brown skin, natural coiled hair",
                       "adult nonbinary model, fair skin, short blonde hair"]
        prompt = ("Create a photorealistic editorial apparel advertisement using this actual garment mockup as the strict reference. "
                  "An entirely fictional adult human model, " + appearances[used % len(appearances)] +
                  f", unique identity seed {campaign['id']}-{design_id}-{used}. Preserve the garment's exact cut, color, "
                  "front print artwork, lettering, placement and proportions. Do not redraw, add, remove or misspell print elements. "
                  "No real person or celebrity likeness. Natural studio light and plain warm background; show garment front clearly. "
                  "No overlays, logos, watermarks, slogans or text outside the garment.")
        self._ai_use(campaign, "social")
        image = await self.client("ai").edit_image(prompt, mockup, filename)
        aid = self.store.save_asset(image, "image/png", f"social-{design_id}.png")
        self._ai_use(campaign, "social_copy")
        copy = await self.client("ai").generate_json("Write truthful social copy in " + str(self.settings().get("language", "en")) +
            ". JSON {instagram_caption:string,pinterest_title:string,pinterest_description:string,hashtags:[string]}. "
            "Disclose image as AI-generated model visual. No invented sales, eco claims or shipping promises. "
            "Use 5-10 relevant non-trademark hashtags. Product facts: " + json.dumps(design["plan"], ensure_ascii=False)[:30000])
        design["social"] = {"asset_id": aid, "copy": copy, "model_sequence": used}
        self._save(campaign, stage="social_review")
        tags = " ".join("#" + str(t).lstrip("#").replace(" ", "") for t in copy.get("hashtags", [])[:10])
        caption = str(copy.get("instagram_caption", "")) + "\n" + tags + "\n" + design["etsy_url"]
        design["social"]["instagram_caption"] = caption[:2200]
        self._save(campaign)
        warning = "\n\nYapay zekâ manken görseli: baskının giysiyle birebir uyuşmasını kontrol edin. Her platform için ayrı onay gerekir."
        await self._notify(campaign, "Instagram yayın önizlemesi\n" + caption + warning,
                           kind="instagram_publish", payload={"design_id": design_id, "asset_id": aid},
                           label="Instagram'da paylaş", image=image)
        await self._notify(campaign, "Pinterest yayın önizlemesi\n" + str(copy.get("pinterest_title", "")) +
                           "\n" + str(copy.get("pinterest_description", "")) + "\n" + tags + warning,
                           kind="pinterest_publish", payload={"design_id": design_id, "asset_id": aid},
                           label="Pinterest'te paylaş", image=image)

    async def _publish_social(self, campaign: dict, action: dict, kind: str):
        design = self._design(campaign, action["payload"]["design_id"])
        social = design["social"]
        if action["payload"]["asset_id"] != social["asset_id"]:
            raise ValueError("Onaylanan sosyal görsel değişmiş; yayın durduruldu.")
        if kind == "instagram_publish":
            instagram = self.client("instagram")
            self._live()
            container = await instagram.create_container(self.store.asset_url(social["asset_id"]), social["instagram_caption"])
            social["instagram_container_id"] = container["id"]
            self._save(campaign)
            ready = False
            for _ in range(12):
                state = await instagram.container_status(container["id"])
                if state.get("status_code") == "FINISHED":
                    ready = True
                    break
                if state.get("status_code") in {"ERROR", "EXPIRED"}:
                    break
                await asyncio.sleep(3)
            if not ready:
                raise ValueError("Instagram görseli yayına hazır değil. Konteyner kimliği kaydedildi; panelden uzak durumu kontrol edin.")
            self._live()
            published = await instagram.publish(container["id"])
            social["instagram_media_id"] = published["id"]
            self._save(campaign)
            verified = await instagram.media(published["id"])
            if not verified.get("id"):
                raise ValueError("Instagram yayını doğrulanamadı.")
            social["instagram"] = verified
        else:
            copy = social["copy"]
            image, _, _ = self.store.read_asset(social["asset_id"])
            pinterest = self.client("pinterest")
            self._live()
            pin = await pinterest.create_pin(str(copy.get("pinterest_title", design["plan"]["title"])),
                str(copy.get("pinterest_description", "")) + "\n" + " ".join(map(str, copy.get("hashtags", []))),
                design["etsy_url"], image)
            social["pinterest_pin_id"] = pin["id"]
            self._save(campaign)
            verified = await pinterest.get_pin(pin["id"])
            if not verified.get("id"):
                raise ValueError("Pinterest yayını doğrulanamadı.")
            social["pinterest"] = verified
        self._save(campaign, stage="social_published")
        await self._notify(campaign, ("Instagram" if kind == "instagram_publish" else "Pinterest") + " paylaşımı API'den doğrulandı.")
