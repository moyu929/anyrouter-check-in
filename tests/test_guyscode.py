"""guyscode 独立签到分支离线测试（Mock httpx/browser，不触网、不真实登录）。"""

import sys
from pathlib import Path

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import utils.guyscode as gc
from utils.config import AppConfig


def _resp(code=0, data=None, status=200):
	return httpx.Response(status, json={'code': code, 'message': 'ok', 'data': data or {}})


class TestParsePayload:
	def test_ok(self):
		assert gc._parse_payload(_resp(0, {'balance': 1.5})) == (0, {'balance': 1.5})

	def test_wrong_code(self):
		assert gc._parse_payload(_resp(40001, {})) == (40001, {})

	def test_not_json(self):
		r = httpx.Response(200, text='<html>waf</html>')
		assert gc._parse_payload(r) == (None, {})


class TestInfoFromBalance:
	def test_usd_format(self):
		info = gc._info_from_balance(0.2333)
		assert info['success'] is True
		assert info['quota'] == 0.23
		assert info['unit'] == 'usd'
		assert '0.23' in info['display']

	def test_zero(self):
		info = gc._info_from_balance(0)
		assert info['quota'] == 0.0


class TestGetBalance:
	def test_parses_balance(self, monkeypatch):
		client = httpx.Client(transport=httpx.MockTransport(lambda req: _resp(0, {'balance': 8.5})))
		monkeypatch.setattr(gc, 'request_with_retry', lambda c, m, u, **k: _resp(0, {'balance': 8.5}))
		assert gc._get_balance(client, 'A') == 8.5

	def test_none_on_wrong_code(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(gc, 'request_with_retry', lambda c, m, u, **k: _resp(40001, {}))
		assert gc._get_balance(client, 'A') is None

	def test_none_on_missing_field(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(gc, 'request_with_retry', lambda c, m, u, **k: _resp(0, {}))
		assert gc._get_balance(client, 'A') is None


class TestCheckin:
	"""guyscode_checkin：refresh_token 续期优先，失败回落浏览器登录。"""

	@pytest.fixture
	def no_refresh(self, monkeypatch, tmp_path):
		"""隔离 refresh_token 存储路径，且默认无缓存。"""
		monkeypatch.setattr(gc, '_refresh_file_path', lambda: str(tmp_path / 'refresh.json'))
		monkeypatch.setattr(gc, 'load_refresh_tokens', lambda: {})
		monkeypatch.setattr(gc, 'save_refresh_token', lambda a, r: None)
		return tmp_path

	def test_uses_cached_refresh_then_checkin(self, monkeypatch, no_refresh):
		"""有缓存 refresh_token → 纯 API 续期拿 token → 签到，浏览器登录不被调用。"""
		monkeypatch.setattr(gc, 'load_refresh_tokens', lambda: {'A': 'refresh-abc'})
		monkeypatch.setattr(gc, '_refresh_access_token', lambda c, rt, name: 'new-jwt')
		monkeypatch.setattr(
			gc, '_browser_login_sync', lambda *a, **k: (_ for _ in ()).throw(AssertionError('不应调用浏览器'))
		)
		balances = iter([5.0, 5.2])
		monkeypatch.setattr(gc, '_get_balance', lambda client, name: next(balances))

		def fake_req(client, method, url, **kwargs):
			if url.startswith(gc.CHECKIN_API):
				return _resp(0, {'reward_amount_usd': 0.2})
			raise AssertionError(f'unexpected url: {url}')

		monkeypatch.setattr(gc, 'request_with_retry', fake_req)
		success, before, after = gc.guyscode_checkin('A', 'e@mail.com', 'pw')
		assert success is True
		assert before['quota'] == 5.0
		assert after['quota'] == 5.2

	def test_refresh_invalid_falls_back_to_browser(self, monkeypatch, no_refresh):
		"""缓存 refresh_token 刷新失败 → 回落浏览器登录，并保存新 refresh_token。"""
		monkeypatch.setattr(gc, 'load_refresh_tokens', lambda: {'A': 'expired-refresh'})
		monkeypatch.setattr(gc, '_refresh_access_token', lambda c, rt, name: None)
		saved = {}
		monkeypatch.setattr(gc, 'save_refresh_token', lambda a, r: saved.update({a: r}))
		monkeypatch.setattr(
			gc, '_browser_login_sync', lambda *a, **k: {'token': 'browser-jwt', 'refresh_token': 'brand-new-refresh'}
		)
		balances = iter([3.0, 3.1])
		monkeypatch.setattr(gc, '_get_balance', lambda client, name: next(balances))

		def fake_req(client, method, url, **kwargs):
			if url.startswith(gc.CHECKIN_API):
				return _resp(0, {'reward_amount_usd': 0.1})
			raise AssertionError(f'unexpected url: {url}')

		monkeypatch.setattr(gc, 'request_with_retry', fake_req)
		success, before, after = gc.guyscode_checkin('A', 'e@mail.com', 'pw')
		assert success is True
		assert saved.get('A') == 'brand-new-refresh'

	def test_no_token_no_cached_refresh_browser_fails(self, monkeypatch, no_refresh):
		"""无缓存 refresh + 浏览器登录失败 → 返回失败。"""
		monkeypatch.setattr(gc, '_browser_login_sync', lambda *a, **k: None)
		success, before, after = gc.guyscode_checkin('A', 'e@mail.com', 'pw')
		assert success is False
		assert before is not None and before['success'] is False
		assert after is None

	def test_checkin_api_failure(self, monkeypatch, no_refresh):
		"""签到 API 返回失败 → 返回 False。"""
		monkeypatch.setattr(gc, '_refresh_access_token', lambda c, rt, name: 'jwt')
		monkeypatch.setattr(gc, 'load_refresh_tokens', lambda: {'A': 'refresh'})
		monkeypatch.setattr(gc, '_get_balance', lambda client, name: 1.0)

		def fake_req(client, method, url, **kwargs):
			if url.startswith(gc.CHECKIN_API):
				return _resp(40001, {})
			raise AssertionError

		monkeypatch.setattr(gc, 'request_with_retry', fake_req)
		success, before, after = gc.guyscode_checkin('A', 'e@mail.com', 'pw')
		assert success is False
		assert after is None

	def test_checkin_already_claimed_counts_as_success(self, monkeypatch, no_refresh):
		"""签到接口返回"今日已签到"(409) → 视为成功(幂等)，不发失败通知。"""
		monkeypatch.setattr(gc, '_refresh_access_token', lambda c, rt, name: 'jwt')
		monkeypatch.setattr(gc, 'load_refresh_tokens', lambda: {'A': 'refresh'})
		balances = iter([0.23, 0.23])
		monkeypatch.setattr(gc, '_get_balance', lambda client, name: next(balances))

		def fake_req(client, method, url, **kwargs):
			if url.startswith(gc.CHECKIN_API):
				return httpx.Response(
					409,
					json={'code': 409, 'message': 'check-in already claimed today', 'reason': 'CHECK_IN_ALREADY'},
				)
			raise AssertionError

		monkeypatch.setattr(gc, 'request_with_retry', fake_req)
		success, before, after = gc.guyscode_checkin('A', 'e@mail.com', 'pw')
		assert success is True
		assert before['quota'] == 0.23
		assert after['quota'] == 0.23


class TestRefreshTokenStorage:
	"""refresh_token 持久化读写。"""

	def test_save_and_load(self, tmp_path, monkeypatch):
		monkeypatch.setattr(gc, '_refresh_file_path', lambda: str(tmp_path / 'refresh.json'))
		gc.save_refresh_token('A', 'rt-1')
		assert gc.load_refresh_tokens().get('A') == 'rt-1'

	def test_save_preserves_others(self, tmp_path, monkeypatch):
		monkeypatch.setattr(gc, '_refresh_file_path', lambda: str(tmp_path / 'refresh.json'))
		gc.save_refresh_token('A', 'rt-1')
		gc.save_refresh_token('B', 'rt-2')
		tokens = gc.load_refresh_tokens()
		assert tokens == {'A': 'rt-1', 'B': 'rt-2'}

	def test_load_returns_empty_on_missing(self, tmp_path, monkeypatch):
		monkeypatch.setattr(gc, '_refresh_file_path', lambda: str(tmp_path / 'missing.json'))
		assert gc.load_refresh_tokens() == {}


class TestRefreshAccessToken:
	def test_refresh_success(self, monkeypatch):
		client = httpx.Client()

		def fake_req(c, m, u, **k):
			assert u == gc.REFRESH_API
			return _resp(0, {'token': 'new-token'})

		monkeypatch.setattr(gc, 'request_with_retry', fake_req)
		assert gc._refresh_access_token(client, 'rt', 'A') == 'new-token'

	def test_refresh_failure(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(gc, 'request_with_retry', lambda c, m, u, **k: _resp(40001, {}))
		assert gc._refresh_access_token(client, 'rt', 'A') is None


class TestConfig:
	def test_guyscode_provider_registered(self):
		cfg = AppConfig.load_from_env()
		provider = cfg.get_provider('guyscode')
		assert provider is not None
		assert provider.auth_method == 'guyscode'
		assert provider.domain == 'https://www.guyscode.com'
		assert provider.use_proxy is False

	def test_provider_defaults_immutable(self):
		"""默认 provider 不被其他调用破坏（域名覆盖继承逻辑）。"""
		cfg1 = AppConfig.load_from_env()
		cfg2 = AppConfig.load_from_env()
		assert cfg1.get_provider('guyscode').domain == cfg2.get_provider('guyscode').domain
