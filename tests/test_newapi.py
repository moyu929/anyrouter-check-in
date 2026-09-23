"""New-API 统一分支（nianhua / kuaipao / hcnsec）离线测试（Mock httpx，不触网、不真实登录）。

覆盖两条自适应链路：
  * 登录协议：新版 JWT（access_token）→ Bearer 头；老版 session（data.id）→ New-Api-User 头
  * 显示币种：/api/status 的 quota_display_type=CNY → 人民币（含汇率），其余 → 美元
"""

import sys
import time
from pathlib import Path

import httpx

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import utils.checkin_core as cc
import utils.newapi as na
from utils.config import AppConfig

DOMAIN = 'https://api.hcnsec.cn'


def _r(ok=True, data=None, message='ok', status=200):
	body = {'success': ok, 'message': message, 'data': data or {}}
	return httpx.Response(status, json=body)


class TestResolveAuth:
	"""登录响应 → 协议判定（纯函数，不触网）。"""

	def test_new_protocol_access_token(self):
		assert na._resolve_auth({'access_token': 'tok-123', 'user': {'id': 86433}}) == ('bearer', 'tok-123')

	def test_legacy_token_alias(self):
		assert na._resolve_auth({'token': 'tok'}) == ('bearer', 'tok')

	def test_old_protocol_top_level_id(self):
		assert na._resolve_auth({'id': 86433, 'username': '墨羽'}) == ('session', '86433')

	def test_new_protocol_user_id_only(self):
		"""新版响应体但缺 access_token 时，退回 user.id 走 session 协议。"""
		assert na._resolve_auth({'user': {'id': 7}}) == ('session', '7')

	def test_no_credential_returns_none(self):
		assert na._resolve_auth({'username': 'x'}) is None

	def test_non_int_id_ignored(self):
		assert na._resolve_auth({'id': '86433'}) is None


class TestLogin:
	"""登录请求已收敛到 checkin_core.newapi_login，patch 点在 core 模块。"""

	def test_ok_returns_payload(self, monkeypatch):
		client = httpx.Client()

		def fake_req(c, m, u, **k):
			assert u == f'{DOMAIN}/api/user/login'
			assert k.get('json') == {'username': 'e@mail.com', 'password': 'pw'}
			return _r(True, {'access_token': 'tok-123'})

		monkeypatch.setattr(cc, 'request_with_retry', fake_req)
		assert cc.newapi_login(client, DOMAIN, 'e@mail.com', 'pw', 'A') == {'access_token': 'tok-123'}

	def test_wrong_credentials_returns_none(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			cc,
			'request_with_retry',
			lambda c, m, u, **k: _r(False, message='Username or password is incorrect'),
		)
		assert cc.newapi_login(client, DOMAIN, 'e', 'bad', 'A') is None


class TestDetectCurrency:
	def test_cny_site_returns_rate(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			na,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota_display_type': 'CNY', 'usd_exchange_rate': 7.3}),
		)
		assert na._detect_currency(client, DOMAIN, 'A') == ('cny', 7.3)

	def test_usd_site_ignores_rate(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			na,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota_display_type': 'USD', 'usd_exchange_rate': 7.3}),
		)
		assert na._detect_currency(client, DOMAIN, 'A') == ('usd', 1.0)

	def test_custom_site_falls_back_to_usd(self, monkeypatch):
		"""CUSTOM 币种（kuaipao）暂按美元显示。"""
		client = httpx.Client()
		monkeypatch.setattr(
			na,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota_display_type': 'CUSTOM', 'usd_exchange_rate': 1}),
		)
		assert na._detect_currency(client, DOMAIN, 'A') == ('usd', 1.0)

	def test_cny_without_valid_rate_defaults_to_1(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			na, 'request_with_retry', lambda c, m, u, **k: _r(True, {'quota_display_type': 'CNY'})
		)
		assert na._detect_currency(client, DOMAIN, 'A') == ('cny', 1.0)

	def test_error_falls_back_to_usd(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(na, 'request_with_retry', lambda c, m, u, **k: (_ for _ in ()).throw(RuntimeError('x')))
		assert na._detect_currency(client, DOMAIN, 'A') == ('usd', 1.0)


class TestGetUserInfo:
	def test_parses_quota_usd(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			na,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota': 2_500_000, 'used_quota': 100_000}),
		)
		info = na._get_user_info(client, DOMAIN, 'A', 'usd', 1.0)
		assert info['success'] is True
		assert info['quota'] == 5.0
		assert info['used_quota'] == 0.2
		assert info['unit'] == 'usd'
		assert '$5.0' in info['display']

	def test_parses_quota_cny_with_rate(self, monkeypatch):
		"""页面余额 = quota/500000×usd_exchange_rate（hcnsec 7.3）。"""
		client = httpx.Client()
		monkeypatch.setattr(
			na,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota': 2_009_718_187, 'used_quota': 0}),
		)
		info = na._get_user_info(client, DOMAIN, 'A', 'cny', 7.3)
		assert info['quota'] == 29341.89
		assert info['unit'] == 'cny'
		assert '¥29341.89' in info['display']

	def test_failure_info_keeps_unit(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(na, 'request_with_retry', lambda c, m, u, **k: _r(False))
		info = na._get_user_info(client, DOMAIN, 'A', 'cny', 7.3)
		assert info['success'] is False
		assert info['unit'] == 'cny'


class TestCheckIn:
	def _fake_flow(
		self,
		monkeypatch,
		balances,
		checkin_resp=_r(True),
		login_payload=None,
		unit='usd',
		rate=1.0,
	):
		"""登录 + self 前后余额 + checkin 均 mock：balance 迭代器依次提供 before/after 余额 dict。"""
		monkeypatch.setattr(na, '_detect_currency', lambda c, d, n: (unit, rate))
		monkeypatch.setattr(na, 'newapi_login', lambda c, d, e, p, n: login_payload)
		calls = {'n': 0}

		def fake_info(client, domain, name, unit, rate):
			idx = calls['n'] % len(balances)
			calls['n'] += 1
			d = balances[idx]
			symbol = '¥' if unit == 'cny' else '$'
			return {
				'success': True,
				'quota': d[0],
				'used_quota': d[1],
				'unit': unit,
				'display': f'💰 当前余额: {symbol}{d[0]}, 已用: {symbol}{d[1]}',
			}

		monkeypatch.setattr(na, '_get_user_info', fake_info)

		def fake_req(client, method, url, **kwargs):
			if url == f'{DOMAIN}/api/user/checkin':
				return checkin_resp
			raise AssertionError(f'unexpected url: {url}')

		monkeypatch.setattr(na, 'request_with_retry', fake_req)
		monkeypatch.setattr(time, 'sleep', lambda s: None)

	# --- 鉴权头注入 ---

	def test_new_protocol_injects_bearer_header(self, monkeypatch):
		self._fake_flow(monkeypatch, [(5.0, 0.2), (5.2, 0.2)], login_payload={'access_token': 'tok-abc'})
		headers = {}
		monkeypatch.setattr(na, 'create_client', lambda **k: _SpyClient(headers, k))

		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert headers.get('Authorization') == 'Bearer tok-abc'
		assert 'New-Api-User' not in headers

	def test_old_protocol_injects_new_api_user_header(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0), (1.2, 0.0)], login_payload={'id': 86433})
		headers = {}
		monkeypatch.setattr(na, 'create_client', lambda **k: _SpyClient(headers, k))

		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert headers.get('New-Api-User') == '86433'
		assert 'Authorization' not in headers

	def test_login_payload_without_credential_fails(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], login_payload={'username': 'x'})
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert after is None

	def test_login_failure(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], login_payload=None)
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert before['success'] is False
		assert after is None

	# --- 流程结果 ---

	def test_success(self, monkeypatch):
		self._fake_flow(monkeypatch, [(5.0, 0.2), (5.2, 0.2)], login_payload={'access_token': 'tok'})
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == 5.0
		assert after['quota'] == 5.2

	def test_cny_site_keeps_cny_display(self, monkeypatch):
		"""CNY 站点（hcnsec）签到后仍以人民币显示，不因协议切换退化为美元。"""
		self._fake_flow(
			monkeypatch,
			[(29341.89, 0.0), (29341.89, 0.0)],
			login_payload={'access_token': 'tok'},
			unit='cny',
			rate=7.3,
		)
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['unit'] == 'cny'
		assert '¥29341.89' in before['display']

	def test_already_claimed_counts_as_success(self, monkeypatch):
		self._fake_flow(
			monkeypatch,
			[(1.0, 0.0), (1.0, 0.0)],
			checkin_resp=_r(False, message='您今天已经签到过，请勿重复签到'),
			login_payload={'access_token': 'tok'},
		)
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == after['quota'] == 1.0

	def test_already_claimed_today_message(self, monkeypatch):
		"""hcnsec 实测返回的「今日已签到」文案。"""
		self._fake_flow(
			monkeypatch,
			[(1.0, 0.0), (1.0, 0.0)],
			checkin_resp=_r(False, message='今日已签到'),
			login_payload={'access_token': 'tok'},
		)
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True

	def test_checkin_failure(self, monkeypatch):
		self._fake_flow(
			monkeypatch, [(1.0, 0.0)], checkin_resp=_r(False, message='签到失败'), login_payload={'access_token': 'tok'}
		)
		ok, before, after = na.newapi_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert after is None


class _SpyClient:
	"""记录鉴权头写入的最小假 client（供鉴权头注入用例观察 headers）。"""

	def __init__(self, sink: dict, kwargs: dict):
		self.headers = sink
		self.headers.update(kwargs.get('headers') or {})
		self.closed = False

	def close(self):
		self.closed = True


class TestProviderConfig:
	def test_nianhua_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('nianhua')
		assert p is not None
		assert p.auth_method == 'newapi'
		assert p.domain == 'https://us-3.nianhuaapi.com'
		assert p.use_proxy is False

	def test_kuaipao_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('kuaipao')
		assert p is not None
		assert p.auth_method == 'newapi'
		assert p.domain == 'https://kuaipao.ai'
		assert p.sign_in_path == '/api/user/checkin'
		assert p.user_info_path == '/api/user/self'
		assert p.api_user_key is None
		assert p.use_proxy is False

	def test_hcnsec_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('hcnsec')
		assert p is not None
		assert p.auth_method == 'newapi'
		assert p.domain == 'https://api.hcnsec.cn'
		assert p.use_proxy is False

	def test_superapi_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('superapi')
		assert p is not None
		# 2026-09 站点套 Cloudflare 全站质询后改走浏览器登录分支（见 utils/browser_checkin.py）
		assert p.auth_method == 'browser_checkin'
		assert p.domain == 'https://superapi.buzz'
