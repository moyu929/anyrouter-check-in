"""New-API session（hcnsec 等）纯 API 签到分支离线测试（Mock httpx，不触网、不真实登录）。"""

import sys
import time
from pathlib import Path

import httpx

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import utils.checkin_core as cc
import utils.newapi_session as ns
from utils.config import AppConfig

DOMAIN = 'https://api.hcnsec.cn'


def _r(ok=True, data=None, message='ok', status=200):
	body = {'success': ok, 'message': message, 'data': data or {}}
	return httpx.Response(status, json=body)


class TestLogin:
	"""登录请求已收敛到 checkin_core.newapi_login，patch 点在 core 模块。"""

	def test_ok_returns_user_id(self, monkeypatch):
		client = httpx.Client()

		def fake_req(c, m, u, **k):
			assert u == f'{DOMAIN}/api/user/login'
			assert k.get('json') == {'username': 'e@mail.com', 'password': 'pw'}
			return _r(True, {'id': 86433, 'username': '墨羽'})

		monkeypatch.setattr(cc, 'request_with_retry', fake_req)
		assert ns._login(client, DOMAIN, 'e@mail.com', 'pw', 'A') == '86433'

	def test_wrong_credentials_returns_none(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			cc,
			'request_with_retry',
			lambda c, m, u, **k: _r(False, message='Username or password is incorrect'),
		)
		assert ns._login(client, DOMAIN, 'e', 'bad', 'A') is None

	def test_no_id_returns_none(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(True, {'username': 'x'}))
		assert ns._login(client, DOMAIN, 'e', 'pw', 'A') is None


class TestGetUserInfo:
	def test_parses_quota_cny_rate1(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			ns,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota': 2_500_000, 'used_quota': 100_000}),
		)
		info = ns._get_user_info(client, DOMAIN, 'A', rate=1.0)
		assert info['success'] is True
		assert info['quota'] == 5.0
		assert info['used_quota'] == 0.2
		assert info['unit'] == 'cny'
		assert '¥5.0' in info['display']

	def test_parses_quota_cny_rate73(self, monkeypatch):
		"""页面余额 = quota/500000×usd_exchange_rate（该站 7.3），对齐页面 ¥29,341.89 级别。"""
		client = httpx.Client()
		monkeypatch.setattr(
			ns,
			'request_with_retry',
			lambda c, m, u, **k: _r(True, {'quota': 2_009_718_187, 'used_quota': 0}),
		)
		info = ns._get_user_info(client, DOMAIN, 'A', rate=7.3)
		assert info['quota'] == 29341.89
		assert '¥29341.89' in info['display']

	def test_failure_info(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(ns, 'request_with_retry', lambda c, m, u, **k: _r(False))
		info = ns._get_user_info(client, DOMAIN, 'A')
		assert info['success'] is False


class TestGetExchangeRate:
	def test_reads_rate(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(ns, 'request_with_retry', lambda c, m, u, **k: _r(True, {'usd_exchange_rate': 7.3}))
		assert ns._get_exchange_rate(client, DOMAIN, 'A') == 7.3

	def test_returns_1_on_missing(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(ns, 'request_with_retry', lambda c, m, u, **k: _r(True, {}))
		assert ns._get_exchange_rate(client, DOMAIN, 'A') == 1.0

	def test_none_on_error_returns_1(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(ns, 'request_with_retry', lambda c, m, u, **k: (_ for _ in ()).throw(RuntimeError('x')))
		assert ns._get_exchange_rate(client, DOMAIN, 'A') == 1.0


class TestCheckIn:
	def _fake_flow(self, monkeypatch, balances, checkin_resp=_r(True), login_ok=True):
		monkeypatch.setattr(ns, '_login', lambda c, d, e, p, name: ('86433' if login_ok else None))
		calls = {'n': 0}

		def fake_info(client, domain, name, rate=1.0):
			idx = calls['n'] % len(balances)
			calls['n'] += 1
			d = balances[idx]
			return {
				'success': True,
				'quota': d[0],
				'used_quota': d[1],
				'unit': 'cny',
				'display': f'💰 当前余额: ¥{d[0]}, 已用: ¥{d[1]}',
			}

		monkeypatch.setattr(ns, '_get_user_info', fake_info)
		monkeypatch.setattr(ns, '_get_exchange_rate', lambda c, d, n: 1.0)

		def fake_req(client, method, url, **kwargs):
			if url == f'{DOMAIN}/api/user/checkin':
				return checkin_resp
			raise AssertionError(f'unexpected url: {url}')

		monkeypatch.setattr(ns, 'request_with_retry', fake_req)
		monkeypatch.setattr(time, 'sleep', lambda s: None)

	def test_success(self, monkeypatch):
		self._fake_flow(monkeypatch, [(5.0, 0.2), (5.2, 0.2)])
		ok, before, after = ns.newapi_session_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == 5.0
		assert after['quota'] == 5.2

	def test_already_claimed_counts_as_success(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0), (1.0, 0.0)], checkin_resp=_r(False, message='今日已签到'))
		ok, before, after = ns.newapi_session_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == after['quota'] == 1.0

	def test_checkin_failure(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], checkin_resp=_r(False, message='签到失败'))
		ok, before, after = ns.newapi_session_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert after is None

	def test_login_failure(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], login_ok=False)
		ok, before, after = ns.newapi_session_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert before['success'] is False
		assert after is None


class TestProviderConfig:
	def test_hcnsec_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('hcnsec')
		assert p is not None
		assert p.auth_method == 'newapi_session'
		assert p.domain == 'https://api.hcnsec.cn'
		assert p.use_proxy is False


class TestFormatAmount:
	def test_cny(self):
		from checkin import _format_amount

		assert _format_amount(5.0, 'cny') == '¥5.00'
		assert _format_amount(5.0, 'usd') == '$5.00'
