import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from jarvis.app import create_app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'APP_PASSWORD': 'test-owner-password-123', 'APP_PUBLIC_URL': ''}):
            self.app = create_app(self.tmp.name, workers=False)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def login(self):
        r = self.client.post('/api/login', json={'password': 'test-owner-password-123'})
        self.assertEqual(r.status_code, 200)
        return {'X-CSRF-Token': r.json()['csrf_token']}

    def test_health_and_admin_authentication(self):
        self.assertEqual(self.client.get('/healthz').json()['status'], 'ok')
        public = self.client.get('/api/state').json()
        self.assertFalse(public['authenticated'])
        self.assertNotIn('settings', public)
        self.assertEqual(self.client.get('/api/connections/check').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'password': 'bad'}).status_code, 401)
        self.login()
        self.assertTrue(self.client.get('/api/state').json()['authenticated'])

    def test_settings_require_csrf_and_preserve_secret(self):
        headers = self.login()
        body = {'settings': {'openai_api_key': 'sk-private-test-value'}}
        self.assertEqual(self.client.patch('/api/settings', json=body).status_code, 403)
        self.assertEqual(self.client.patch('/api/settings', json=body, headers=headers).status_code, 200)
        state = self.client.get('/api/state').json()
        self.assertNotIn('openai_api_key', state['settings'])
        self.assertTrue(state['presence']['openai_api_key'])
        self.assertNotIn('sk-private-test-value', self.client.get('/api/state').text)

    def test_foreign_origin_cannot_mutate(self):
        headers = self.login()
        headers['Origin'] = 'https://attacker.invalid'
        r = self.client.post('/api/pause', json={'paused': True}, headers=headers)
        self.assertEqual(r.status_code, 403)

    def test_dry_run_does_not_fabricate_design_or_publish(self):
        headers = self.login()
        response = self.client.post('/api/run', json={'dry_run': True}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.store.list_campaigns(), [])

    def test_oauth_has_pkce_state_and_bound_session(self):
        headers = self.login()
        self.client.patch('/api/settings', json={'settings': {'etsy_key': 'etsy-app-key'}}, headers=headers)
        response = self.client.get('/auth/etsy/start', follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        q = parse_qs(urlsplit(response.headers['location']).query)
        self.assertEqual(q['code_challenge_method'], ['S256'])
        state = q['state'][0]
        other = TestClient(self.app)
        other.post('/api/login', json={'password': 'test-owner-password-123'})
        r = other.get('/auth/etsy/callback', params={'state': state, 'error': 'denied'})
        self.assertEqual(r.status_code, 400)
        # Original session still can consume the state, but not twice.
        self.assertEqual(self.client.get('/auth/etsy/callback', params={'state': state, 'error': 'denied'}).status_code, 400)
        self.assertEqual(self.client.get('/auth/etsy/callback', params={'state': state, 'error': 'denied'}).status_code, 400)

    def test_product_change_invalidates_stale_approval(self):
        headers = self.login()
        store = self.app.state.store
        cid = store.create_campaign('Product', {})
        token = store.create_action(cid, 'etsy_publish', {'listing_id': 123}, 'publish')
        self.client.patch('/api/settings', json={'settings': {'retail_price': 35}}, headers=headers)
        self.assertEqual(store.get_action(token)['status'], 'cancelled')

    def test_pause_and_dismiss_preserve_remote_records(self):
        headers = self.login()
        store = self.app.state.store
        cid = store.create_campaign('Product', {'etsy_id': 123})
        store.update_campaign(cid, status='awaiting_approval')
        token = store.create_action(cid, 'etsy_publish', {}, 'publish')
        self.client.post('/api/pause', json={'paused': True}, headers=headers)
        self.assertEqual(store.get_action(token)['status'], 'cancelled')
        self.assertEqual(self.client.post(f'/api/campaigns/{cid}/dismiss', json={}, headers=headers).status_code, 200)
        self.assertEqual(store.get_campaign(cid)['data']['etsy_id'], 123)

    def test_invalid_boolean_and_nonfinite_price_are_rejected(self):
        headers = self.login()
        for settings in ({'scheduling_enabled': 'false'}, {'retail_price': 'Infinity'}):
            response = self.client.patch('/api/settings', json={'settings': settings}, headers=headers)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.app.state.store.get_settings()['scheduling_enabled'])

    def test_streamed_oversized_request_is_rejected(self):
        headers = self.login()
        response = self.client.patch('/api/settings', content=iter([b'x' * 60_000, b'x' * 60_000]),
                                     headers={**headers, 'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
