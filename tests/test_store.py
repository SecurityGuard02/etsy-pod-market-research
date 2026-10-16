import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from jarvis.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'APP_PASSWORD': ''}):
            self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_encrypted_at_rest_and_secret_not_returned(self):
        self.store.update_settings({'openai_api_key': 'sk-unmistakable-secret', 'niche': 'nature'})
        public, presence = self.store.public_settings()
        self.assertNotIn('openai_api_key', public)
        self.assertTrue(presence['openai_api_key'])
        with self.store.db() as c:
            saved = c.execute("SELECT value FROM meta WHERE key='settings'").fetchone()['value']
        self.assertNotIn('sk-unmistakable-secret', saved)
        self.assertEqual(self.store.get_settings()['openai_api_key'], 'sk-unmistakable-secret')

    def test_restart_preserves_encryption_key_and_records(self):
        self.store.update_settings({'printify_token': 'actual-secret'})
        cid = self.store.create_campaign('one', {'designs': []})
        fresh = Store(self.tmp.name)
        self.assertEqual(fresh.get_settings()['printify_token'], 'actual-secret')
        self.assertEqual(fresh.get_campaign(cid)['title'], 'one')

    def test_empty_secret_does_not_destroy_previous_binding(self):
        self.store.update_settings({'openai_api_key': 'previous'})
        self.store.update_settings({'openai_api_key': ''})
        self.assertEqual(self.store.get_settings()['openai_api_key'], 'previous')

    def test_approval_is_single_use(self):
        cid = self.store.create_campaign('one', {})
        token = self.store.create_action(cid, 'etsy_publish', {'listing_id': 123}, 'Yayımla')
        result = self.store.consume_action(token, 'approve')
        self.assertEqual(result['payload']['listing_id'], 123)
        with self.assertRaises(ValueError):
            self.store.consume_action(token, 'approve')

    def test_unauthorized_chat_and_expiry_do_not_consume(self):
        self.store.update_settings({'telegram_chat_id': '123'})
        cid = self.store.create_campaign('one', {})
        token = self.store.create_action(cid, 'etsy_publish', {}, 'Yayımla')
        with self.assertRaises(ValueError):
            self.store.consume_action(token, 'approve', actor_chat='456')
        self.assertEqual(self.store.get_action(token)['status'], 'pending')
        with self.store.db() as c:
            c.execute('UPDATE actions SET expires=? WHERE token=?', (time.time() - 1, token))
        with self.assertRaises(ValueError):
            self.store.consume_action(token, 'approve', actor_chat='123')

    def test_pause_invalidates_actions(self):
        cid = self.store.create_campaign('one', {})
        token = self.store.create_action(cid, 'generate', {}, 'Üret')
        self.store.invalidate_actions()
        with self.assertRaises(ValueError):
            self.store.consume_action(token, 'approve')

    def test_password_is_salted_and_session_is_hashed(self):
        self.store.set_password('a-long-owner-password')
        self.assertTrue(self.store.check_password('a-long-owner-password'))
        self.assertFalse(self.store.check_password('wrong'))
        self.assertNotIn('a-long-owner-password', self.store.get_meta('password'))
        token, csrf = self.store.new_session()
        self.assertEqual(self.store.session(token)['csrf'], csrf)
        self.assertNotEqual(self.store.session(token)['token'], token)
        self.store.delete_session(token)
        self.assertIsNone(self.store.session(token))

    def test_private_assets_require_correct_signature_and_no_path_traversal(self):
        aid = self.store.save_asset(b'test', 'image/png', 'design.png')
        from urllib.parse import urlsplit, parse_qs
        url = self.store.asset_url(aid)
        q = parse_qs(urlsplit(url).query)
        self.assertTrue(self.store.verify_asset(aid, int(q['expires'][0]), q['signature'][0]))
        self.assertFalse(self.store.verify_asset(aid, int(q['expires'][0]), 'bad'))
        with self.assertRaises(ValueError):
            self.store.read_asset('../../encryption.key')

    def test_event_logs_redact_keys(self):
        self.store.update_settings({'etsy_key': 'real-sensitive-etsy-key'})
        self.store.add_event('failed real-sensitive-etsy-key')
        self.assertNotIn('real-sensitive-etsy-key', self.store.events()[0]['message'])


if __name__ == '__main__':
    unittest.main()
