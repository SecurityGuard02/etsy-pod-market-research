import base64
import io
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch

from jarvis.providers import (
    ApiTransport, EtsyClient, InstagramClient, OpenAIClient, PinterestClient,
    PrintifyClient, ProviderError, TelegramClient, _SameHostRedirect,
    download_image,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_credential_blocks_before_network(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(ProviderError, "openai_api_key"):
            await OpenAIClient({}, transport).generate_image("original graphic")
        self.assertEqual(transport.calls, [])

    async def test_image_generation_requests_transparent_png_and_decodes(self):
        transport = FakeTransport({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        client = OpenAIClient({"openai_api_key": "secret"}, transport)
        self.assertEqual(await client.generate_image("original design"), PNG)
        payload = transport.calls[0][2]["json_body"]
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["output_format"], "png")
        self.assertEqual(payload["n"], 1)

    async def test_image_rejects_invalid_provider_payload(self):
        transport = FakeTransport({"data": [{"b64_json": base64.b64encode(b"not png").decode()}]})
        with self.assertRaisesRegex(ProviderError, "PNG"):
            await OpenAIClient({"openai_api_key": "secret"}, transport).generate_image("graphic")

    async def test_edit_attaches_original_design_instead_of_text_only(self):
        transport = FakeTransport({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        client = OpenAIClient({"openai_api_key": "secret"}, transport)
        await client.edit_image("keep graphic identical on adult model", PNG)
        self.assertEqual(transport.calls[0][2]["files"]["image"][1], PNG)
        self.assertEqual(transport.calls[0][2]["form"]["size"], "1024x1024")  # Instagram-compatible aspect ratio
        self.assertTrue(transport.calls[0][1].endswith("/images/edits"))

    async def test_research_uses_live_tool_sources_and_drops_invented_urls(self):
        report = {"summary": "Research", "sources": [{"url": "https://invented.invalid", "observation": "false"}],
                  "limitations": [], "designs": [{"title": str(i), "prompt": "new graphic"} for i in range(5)]}
        response = {"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://www.etsy.com/listing/123"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(report)}]},
        ]}
        transport = FakeTransport(response)
        result = await OpenAIClient({"openai_api_key": "secret"}, transport).research("botanical tees")
        self.assertEqual(len(result["designs"]), 5)
        self.assertEqual(result["sources"][0]["url"], "https://www.etsy.com/listing/123")
        self.assertTrue(result["limitations"])
        self.assertEqual(transport.calls[0][2]["json_body"]["tools"], [{"type": "web_search"}])

    async def test_research_rejects_claimed_sources_without_actual_search_evidence(self):
        report = {"sources": [{"url": "https://made-up.example/"}],
                  "web_sources": [{"url": "https://made-up.example/"}],
                  "designs": [{"prompt": "graphic"} for _ in range(5)]}
        transport = FakeTransport({"output_text": json.dumps(report)})
        with self.assertRaisesRegex(ProviderError, "doğrulanabilir"):
            await OpenAIClient({"openai_api_key": "secret"}, transport).research("tee")

    async def test_etsy_credential_aliases_and_refresh_no_mutation(self):
        transport = FakeTransport({"access_token": "new", "refresh_token": "rotated"})
        config = {"etsy_key": "appkey", "etsy_secret": "appsecret", "etsy_refresh_token": "old"}
        client = EtsyClient(config, transport)
        self.assertEqual(client.headers(False)["x-api-key"], "appkey:appsecret")
        result = await client.refresh_access_token()
        self.assertEqual(result["refresh_token"], "rotated")
        self.assertEqual(config["etsy_refresh_token"], "old")  # caller persists securely
        self.assertEqual(transport.calls[0][2]["form"]["client_id"], "appkey")

    async def test_etsy_draft_fails_closed_when_processing_profile_missing(self):
        transport = FakeTransport()
        client = EtsyClient({}, transport)
        with self.assertRaisesRegex(ProviderError, "readiness_state_id"):
            await client.create_draft({"title": "Tee"})
        self.assertEqual(transport.calls, [])

    async def test_etsy_draft_never_sends_active_state(self):
        client = EtsyClient({}, FakeTransport())
        payload = dict(quantity=2, title="Tee", description="Description", price=20,
                       who_made="someone_else", when_made="made_to_order", taxonomy_id=482,
                       shipping_profile_id=1, readiness_state_id=2, state="active")
        with self.assertRaisesRegex(ProviderError, "draft"):
            await client.create_draft(payload)

    async def test_etsy_inventory_uses_json_without_publish(self):
        transport = FakeTransport({"products": []})
        client = EtsyClient({"etsy_key": "k", "etsy_access_token": "a"}, transport)
        payload = {"products": [{"sku": "jarvis_123", "offerings": [{"price": 25, "quantity": 10, "is_enabled": True}]}]}
        await client.update_inventory(123, payload)
        method, url, args = transport.calls[0]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/listings/123/inventory"))
        self.assertEqual(args["json_body"], payload)

    async def test_printify_product_creation_does_not_publish(self):
        transport = FakeTransport({"id": "product1"})
        client = PrintifyClient({"printify_token": "secret", "printify_shop_id": "9"}, transport)
        payload = {"title": "Tee", "description": "Printed tee", "blueprint_id": 1, "print_provider_id": 2,
                   "variants": [{"id": 3, "price": 2500, "is_enabled": True}],
                   "print_areas": [{"variant_ids": [3], "placeholders": []}]}
        self.assertEqual((await client.create_product(payload))["id"], "product1")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(transport.calls[0][1].endswith("/shops/9/products.json"))
        self.assertNotIn("publish", transport.calls[0][1])

    async def test_instagram_will_not_publish_processing_container(self):
        transport = FakeTransport({"status_code": "IN_PROGRESS"})
        client = InstagramClient({"instagram_access_token": "secret", "instagram_user_id": "10"}, transport)
        with self.assertRaisesRegex(ProviderError, "hazır değil"):
            await client.publish("20")
        self.assertEqual(len(transport.calls), 1)

    async def test_instagram_publish_checks_status_before_remote_write(self):
        transport = FakeTransport({"status_code": "FINISHED"}, {"id": "posted"})
        client = InstagramClient({"instagram_access_token": "secret", "instagram_user_id": "10"}, transport)
        result = await client.publish("20")
        self.assertEqual(result["id"], "posted")
        self.assertEqual([c[0] for c in transport.calls], ["GET", "POST"])

    async def test_pinterest_embeds_image_without_exposing_private_asset_url(self):
        transport = FakeTransport({"id": "pin"})
        client = PinterestClient({"pinterest_access_token": "secret", "pinterest_board_id": "12"}, transport)
        await client.create_pin("Botanical tee", "An original print", "https://www.etsy.com/listing/1", PNG)
        payload = transport.calls[0][2]["json_body"]
        self.assertEqual(payload["board_id"], "12")
        self.assertEqual(payload["media_source"]["source_type"], "image_base64")
        self.assertEqual(base64.b64decode(payload["media_source"]["data"]), PNG)

    async def test_telegram_album_uses_attachments_and_no_callbacks(self):
        transport = FakeTransport({"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]})
        client = TelegramClient({"telegram_token": "token", "telegram_chat_id": "123"}, transport)
        result = await client.send_media_group([PNG, PNG], ["1", "2"])
        self.assertEqual(len(result), 2)
        method, url, args = transport.calls[0]
        self.assertTrue(url.endswith("/sendMediaGroup"))
        media = json.loads(args["form"]["media"])
        self.assertEqual(media[0]["media"], "attach://image0")
        self.assertEqual(len(args["files"]), 2)

    async def test_telegram_polling_restricts_update_types(self):
        transport = FakeTransport({"ok": True, "result": []})
        client = TelegramClient({"telegram_bot_token": "token"}, transport)
        await client.get_updates(offset=19, timeout=20)
        self.assertEqual(transport.calls[0][2]["json_body"]["offset"], 19)
        self.assertEqual(transport.calls[0][2]["json_body"]["allowed_updates"], ["message", "callback_query"])

    async def test_download_blocks_metadata_and_lookalike_domains(self):
        for url in ("http://169.254.169.254/latest", "https://localhost/secret", "https://printify.com.evil.test/x",
                    "https://api.printify.com:8443/a", "https://user:pass@api.printify.com/a"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                await download_image(url)

    async def test_transport_redacts_secrets_from_provider_http_error(self):
        token = "very-sensitive-provider-token"
        error_body = json.dumps({"error": {"message": "Bad token " + token}}).encode()
        error = HTTPError("https://example.test", 401, "Unauthorized", {}, io.BytesIO(error_body))
        transport = ApiTransport("Test", {"access_token": token})
        with patch("jarvis.providers._open", side_effect=error):
            with self.assertRaises(ProviderError) as raised:
                await transport.request("GET", "https://example.test")
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))
        self.assertEqual(raised.exception.status, 401)

    async def test_transport_timeout_does_not_retry_mutation(self):
        transport = ApiTransport("Test", {})
        with patch("jarvis.providers._open", side_effect=TimeoutError) as mocked:
            with self.assertRaises(ProviderError):
                await transport.request("POST", "https://example.test", json_body={"publish": True})
        self.assertEqual(mocked.call_count, 1)

    async def test_transport_rejects_plain_http(self):
        with self.assertRaisesRegex(ProviderError, "HTTPS"):
            await ApiTransport("Test", {}).request("GET", "http://example.test")

    def test_redirect_does_not_forward_auth_to_another_host(self):
        request = Request("https://api.example.test/x", headers={"Authorization": "Bearer secret"})
        handler = _SameHostRedirect()
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test/x"))
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "http://api.example.test/x"))


if __name__ == "__main__":
    unittest.main()
