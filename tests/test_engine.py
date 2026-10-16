"""Workflow tests use local doubles; they never bill or publish to live APIs."""
import asyncio
import copy
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from jarvis.engine import Engine
from jarvis.store import Store


PNG = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 1024, 1536) + b"test-png"


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.update_settings({"dry_run": False, "voice_enabled": False,
            "openai_api_key": "not-a-real-key", "telegram_bot_token": "not-a-real-token",
            "telegram_chat_id": "123", "printify_token": "not-a-real-printify-token",
            "printify_shop_id": "1", "etsy_key": "not-a-real-etsy-key",
            "etsy_access_token": "not-a-real-etsy-token", "etsy_shop_id": "5",
            "etsy_readiness_state_id": 7, "etsy_shipping_profile_id": 8, "etsy_taxonomy_id": 9,
            "instagram_access_token": "not-real", "instagram_user_id": "88",
            "pinterest_access_token": "not-real", "pinterest_board_id": "99",
            "public_url": "https://jarvis.example"})
        report = {"summary": "Açık kaynaklardan gözlenen baskı estetikleri.",
                  "sources": [{"url": "https://www.etsy.com/listing/1"}],
                  "limitations": ["Kesin satış miktarı bilinmiyor."],
                  "designs": [{"title": f"Original {i}", "rationale": "Özgün doğa geometrisi",
                               "prompt": f"Original transparent nature graphic {i}"} for i in range(5)]}
        self.ai = AsyncMock()
        self.ai.research.return_value = report
        self.ai.generate_image.return_value = PNG
        self.ai.edit_image.return_value = PNG
        self.ai.generate_json.side_effect = [
            {"title": "Original Nature Tee", "description": "Made-to-order original nature graphic tee.",
             "tags": ["nature tee", "original print"]},
            {"instagram_caption": "Original nature print. AI-generated model visual.",
             "pinterest_title": "Original nature tee", "pinterest_description": "AI-generated model visual.",
             "hashtags": ["naturetee", "originaldesign"]}]
        self.printify = AsyncMock()
        self.printify.blueprints.return_value = [{"id": 12, "title": "Unisex Jersey Short Sleeve Tee"}]
        self.printify.providers.return_value = [{"id": 3, "title": "Real Printer", "location": {"country": "US"}}]
        self.variant = {"id": 100, "title": "White / M", "options": {"color": "White", "size": "M"},
                        "placeholders": [{"position": "front", "width": 4500, "height": 5400}]}
        self.printify.variants.return_value = {"variants": [self.variant]}
        self.printify.upload_image.return_value = {"id": "upload-1"}
        self.printify.create_product.return_value = {"id": "product-1"}
        self.printify.get_product.return_value = {"id": "product-1", "images": [
            {"src": "https://images.printify.com/mockup.png", "is_default": True}]}
        self.etsy = AsyncMock()
        self.etsy.search_listings.return_value = {"results": [{"listing_id": 1}]}
        self.etsy.shop.return_value = {"currency_code": "USD"}
        self.etsy.return_policies.return_value = {"results": []}
        self.etsy.taxonomy_properties.return_value = {"results": [
            {"property_id": 200, "name": "Primary color", "supports_variations": True},
            {"property_id": 100, "name": "Size", "supports_variations": True}]}
        self.etsy.create_draft.return_value = {"listing_id": 700, "state": "draft"}
        self.etsy.upload_image.return_value = {"listing_image_id": 91}
        self.etsy.get_inventory.return_value = {"products": [{"sku": "approved-size-color", "offerings": [{"price": 29.9}]}]}
        self.actual_listing = {"listing_id": 700, "state": "draft", "title": "Original Nature Tee",
            "description": "Made-to-order original nature graphic tee.", "tags": ["nature tee", "original print"],
            "price": {"amount": 2990, "divisor": 100, "currency_code": "USD"},
            "taxonomy_id": 9, "shipping_profile_id": 8}
        async def listing(_):
            return copy.deepcopy(self.actual_listing)
        async def publish(_):
            self.actual_listing["state"] = "active"
            return copy.deepcopy(self.actual_listing)
        self.etsy.get_listing.side_effect = listing
        self.etsy.publish_listing.side_effect = publish
        self.telegram = AsyncMock()
        self.instagram = AsyncMock()
        self.instagram.create_container.return_value = {"id": "container-1"}
        self.instagram.container_status.return_value = {"status_code": "FINISHED"}
        self.instagram.publish.return_value = {"id": "media-1"}
        self.instagram.media.return_value = {"id": "media-1", "permalink": "https://instagram.com/p/example"}
        self.pinterest = AsyncMock()
        self.pinterest.create_pin.return_value = {"id": "pin-1"}
        self.pinterest.get_pin.return_value = {"id": "pin-1"}
        self.clients = {"ai": self.ai, "printify": self.printify, "etsy": self.etsy,
                        "telegram": self.telegram, "instagram": self.instagram, "pinterest": self.pinterest}
        self.engine = Engine(self.store, self.clients)
        self.download = patch("jarvis.engine.providers.download_image", new=AsyncMock(return_value=PNG))
        self.download.start()
        self.addCleanup(self.download.stop)

    def action(self, cid, kind, design_id=None):
        return next(a for a in self.store.list_actions(cid) if a["kind"] == kind
                    and (design_id is None or a["payload"].get("design_id") == design_id))

    async def approve(self, cid, kind, design_id=None):
        action = self.action(cid, kind, design_id)
        return await self.engine.decide(action["token"], "approve", actor_chat="123")

    async def to_printify_review(self):
        cid = (await self.engine.run_campaign())["id"]
        await self.approve(cid, "generate")
        await self.approve(cid, "choose_design", "1")
        await self.approve(cid, "printify_create", "1")
        return cid

    async def test_full_approval_chain_uses_mockup_and_never_early_publishes(self):
        cid = (await self.engine.run_campaign())["id"]
        self.ai.generate_image.assert_not_awaited()
        self.printify.create_product.assert_not_awaited()
        await self.approve(cid, "generate")
        self.assertEqual(self.ai.generate_image.await_count, 5)
        self.assertEqual(len(self.store.get_campaign(cid)["data"]["designs"]), 5)
        self.printify.create_product.assert_not_awaited()
        await self.approve(cid, "choose_design", "1")
        plan = self.store.get_campaign(cid)["data"]["designs"][0]["plan"]
        self.assertIsNone(plan["production_cost_cents"])
        self.assertIn("doğrulanamadı", plan["cost_note"])
        self.assertEqual(plan["source_dimensions"], [1024, 1536])
        self.assertLess(plan["print_scale"], 1)
        self.printify.create_product.assert_not_awaited()
        await self.approve(cid, "printify_create", "1")
        self.etsy.create_draft.assert_not_awaited()
        await self.approve(cid, "etsy_draft", "1")
        payload = self.etsy.create_draft.await_args.args[0]
        self.assertEqual(payload["state"], "draft")
        self.assertEqual(payload["readiness_state_id"], 7)
        inventory = self.etsy.update_inventory.await_args.args[1]
        self.assertEqual(len(inventory["products"]), 1)
        self.assertEqual(inventory["products"][0]["property_values"][0]["values"], ["White"])
        self.assertIn("-100", inventory["products"][0]["sku"])
        self.etsy.publish_listing.assert_not_awaited()
        await self.approve(cid, "etsy_publish", "1")
        self.etsy.publish_listing.assert_awaited_once_with(700)
        self.ai.edit_image.assert_not_awaited()
        await self.approve(cid, "social_prepare", "1")
        self.ai.edit_image.assert_awaited_once()
        self.assertEqual(self.ai.edit_image.await_args.args[1], PNG)
        self.instagram.publish.assert_not_awaited()
        self.pinterest.create_pin.assert_not_awaited()
        await self.approve(cid, "instagram_publish", "1")
        self.instagram.publish.assert_awaited_once_with("container-1")
        self.pinterest.create_pin.assert_not_awaited()
        await self.approve(cid, "pinterest_publish", "1")
        self.pinterest.get_pin.assert_awaited_once_with("pin-1")
        data = self.store.get_campaign(cid)["data"]["designs"][0]
        self.assertEqual(data["fulfillment"], "manual_unverified_mapping")
        self.assertEqual(data["social"]["instagram"]["id"], "media-1")
        self.assertEqual(data["social"]["pinterest"]["id"], "pin-1")

    async def test_repeated_approval_cannot_generate_again(self):
        cid = (await self.engine.run_campaign())["id"]
        token = self.action(cid, "generate")["token"]
        await self.engine.decide(token, "approve", "123")
        with self.assertRaises(ValueError):
            await self.engine.decide(token, "approve", "123")
        self.assertEqual(self.ai.generate_image.await_count, 5)

    async def test_wrong_telegram_chat_cannot_consume_action(self):
        cid = (await self.engine.run_campaign())["id"]
        token = self.action(cid, "generate")["token"]
        with self.assertRaisesRegex(ValueError, "yetkili"):
            await self.engine.decide(token, "approve", "attacker")
        self.assertEqual(self.store.get_action(token)["status"], "pending")
        self.ai.generate_image.assert_not_awaited()

    async def test_dry_run_and_pause_prevent_paid_work(self):
        self.store.update_settings({"dry_run": True})
        with self.assertRaisesRegex(ValueError, "Canlı"):
            await self.engine.run_campaign()
        self.ai.research.assert_not_awaited()
        self.store.update_settings({"dry_run": False})
        cid = (await self.engine.run_campaign())["id"]
        self.store.update_settings({"paused": True})
        with self.assertRaisesRegex(ValueError, "duraklat"):
            await self.approve(cid, "generate")
        self.ai.generate_image.assert_not_awaited()
        self.assertEqual(self.action(cid, "generate")["status"], "pending")

    async def test_missing_etsy_settings_leave_approval_pending(self):
        cid = await self.to_printify_review()
        self.store.update_settings({"etsy_readiness_state_id": None})
        self.etsy.readiness_states.return_value = {"results": []}
        with self.assertRaisesRegex(ValueError, "readiness_state_id"):
            await self.approve(cid, "etsy_draft", "1")
        self.assertEqual(self.action(cid, "etsy_draft", "1")["status"], "pending")
        self.etsy.create_draft.assert_not_awaited()

    async def test_external_listing_change_blocks_final_publication(self):
        cid = await self.to_printify_review()
        await self.approve(cid, "etsy_draft", "1")
        self.actual_listing["title"] = "Changed outside JARVIS"
        with self.assertRaisesRegex(ValueError, "değişmiş"):
            await self.approve(cid, "etsy_publish", "1")
        self.etsy.publish_listing.assert_not_awaited()

    async def test_external_inventory_change_blocks_final_publication(self):
        cid = await self.to_printify_review()
        await self.approve(cid, "etsy_draft", "1")
        self.etsy.get_inventory.return_value = {"products": [{"sku": "wrong-product"}]}
        with self.assertRaisesRegex(ValueError, "envanteri"):
            await self.approve(cid, "etsy_publish", "1")
        self.etsy.publish_listing.assert_not_awaited()

    async def test_instagram_account_id_alias_is_accepted(self):
        self.store.update_settings({"instagram_user_id": None, "instagram_account_id": "actual-account"})
        self.assertEqual(self.engine.settings()["instagram_user_id"], "actual-account")

    async def test_ambiguous_product_creation_not_replayed_after_restart(self):
        cid = (await self.engine.run_campaign())["id"]
        await self.approve(cid, "generate")
        await self.approve(cid, "choose_design", "1")
        token = self.action(cid, "printify_create", "1")["token"]
        self.printify.create_product.side_effect = TimeoutError("unknown remote outcome")
        with self.assertRaises(TimeoutError):
            await self.engine.decide(token, "approve", "123")
        self.assertEqual(self.store.get_campaign(cid)["status"], "uncertain")
        restarted = Engine(self.store, self.clients)
        with self.assertRaises(ValueError):
            await restarted.decide(token, "approve", "123")
        self.printify.create_product.assert_awaited_once()

    async def test_delayed_printify_mockup_polls_same_product_without_recreating(self):
        cid = (await self.engine.run_campaign())["id"]
        await self.approve(cid, "generate")
        await self.approve(cid, "choose_design", "1")
        ready = self.printify.get_product.return_value
        self.printify.get_product.side_effect = [{"id": "product-1", "images": []}, ready]
        with patch("jarvis.engine.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.approve(cid, "printify_create", "1")
        sleep.assert_awaited_once_with(2)
        self.assertEqual(self.printify.get_product.await_count, 2)
        self.assertEqual([call.args for call in self.printify.get_product.await_args_list],
                         [("product-1",), ("product-1",)])
        self.printify.create_product.assert_awaited_once()
        self.printify.upload_image.assert_awaited_once()
        self.assertEqual(self.action(cid, "etsy_draft", "1")["status"], "pending")

    async def test_pause_during_inflight_work_suppresses_new_approval(self):
        cid = self.store.create_campaign("in-flight campaign", {"designs": []})
        self.store.update_settings({"paused": True})
        token = await self.engine._notify(self.store.get_campaign(cid), "Araştırma hazır",
                                           kind="generate", label="Görsel üret")
        self.assertIsNone(token)
        self.assertEqual(self.store.list_actions(cid), [])
        self.assertIn("duraklatıldı", self.telegram.send_message.await_args.args[0])

    async def test_research_rejection_never_generates_images(self):
        cid = (await self.engine.run_campaign())["id"]
        token = self.action(cid, "generate")["token"]
        await self.engine.decide(token, "reject", "123")
        self.ai.generate_image.assert_not_awaited()
        self.assertEqual(self.store.get_campaign(cid)["status"], "rejected")

    async def test_concurrent_research_runs_are_bounded(self):
        results = await asyncio.gather(self.engine.run_campaign(), self.engine.run_campaign(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.ai.research.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
