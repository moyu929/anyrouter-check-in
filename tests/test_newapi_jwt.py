"""New-API JWT（nianhua/superapi）纯 API 签到分支离线测试（Mock httpx，不触网、不真实登录）。"""

import sys
import time
from pathlib import Path

import httpx

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import utils.checkin_core as cc
import utils.newapi_jwt as nj
from utils.config import AppConfig

DOMAIN = 'https://us-3.nianhuaapi.com'


def _r(ok=True, data=None, message='ok', status=200):
	body = {'success': ok, 'message': message, 'data': data or {}}
	return httpx.Response(status, json=body)


class TestLogin:
	"""登录请求已收敛到 checkin_core.newapi_login，patch 点在 core 模块。"""

	def test_ok_takes_access_token(self, monkeypatch):
		client = httpx.Client()

		def fake_req(c, m, u, **k):
			assert u == f'{DOMAIN}/api/user/login'
			assert k.get('json') == {'username': 'e@mail.com', 'password': 'pw'}
			return _r(True, {'access_token': 'tok-123'})

		monkeypatch.setattr(cc, 'request_with_retry', fake_req)
		assert nj._login(client, DOMAIN, 'e@mail.com', 'pw', 'A') == 'tok-123'

	def test_ok_takes_token_alias(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(True, {'token': 'tok'}))
		assert nj._login(client, DOMAIN, 'e', 'pw', 'A') == 'tok'

	def test_wrong_credentials_returns_none(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(
			cc,
			'request_with_retry',
			lambda c, m, u, **k: _r(False, message='Username or password is incorrect'),
		)
		assert nj._login(client, DOMAIN, 'e', 'bad', 'A') is None

	def test_no_token_field_returns_none(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(True, {'user': {'id': 1}}))
		assert nj._login(client, DOMAIN, 'e', 'pw', 'A') is None


class TestGetUserInfo:
	def test_parses_quota_divide(self, monkeypatch):
		client = httpx.Client()
		quota = 2_500_000  # = $5.0
		used = 100_000  # = $0.2
		monkeypatch.setattr(
			nj, 'request_with_retry', lambda c, m, u, **k: _r(True, {'quota': quota, 'used_quota': used})
		)
		info = nj._get_user_info(client, DOMAIN, 'A')
		assert info['success'] is True
		assert info['quota'] == 5.0
		assert info['used_quota'] == 0.2
		assert info['unit'] == 'usd'
		assert '5.0' in info['display']

	def test_failure_info(self, monkeypatch):
		client = httpx.Client()
		monkeypatch.setattr(nj, 'request_with_retry', lambda c, m, u, **k: _r(False))
		info = nj._get_user_info(client, DOMAIN, 'A')
		assert info['success'] is False


class TestCheckIn:
	def _fake_flow(self, monkeypatch, balances, checkin_resp=_r(True, {'quota': 2_600_000}), login_ok=True):
		"""登录 + self 前后余额 + checkin 均 mock：balance 迭代器依次提供 before/after 余额 dict。"""
		monkeypatch.setattr(
			nj,
			'_login',
			lambda c, d, e, p, name: ('tok' if login_ok else None),
		)
		calls = {'n': 0}

		def fake_info(client, domain, name):
			idx = calls['n'] % len(balances)
			calls['n'] += 1
			d = balances[idx]
			return {
				'success': True,
				'quota': d[0],
				'used_quota': d[1],
				'unit': 'usd',
				'display': f'💰 当前余额: ${d[0]}, 已用: ${d[1]}',
			}

		monkeypatch.setattr(nj, '_get_user_info', fake_info)

		def fake_req(client, method, url, **kwargs):
			if url == f'{DOMAIN}/api/user/checkin':
				return checkin_resp
			raise AssertionError(f'unexpected url: {url}')

		monkeypatch.setattr(nj, 'request_with_retry', fake_req)
		monkeypatch.setattr(time, 'sleep', lambda s: None)

	def test_success(self, monkeypatch):
		self._fake_flow(monkeypatch, [(5.0, 0.2), (5.2, 0.2)])
		ok, before, after = nj.newapi_jwt_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == 5.0
		assert after['quota'] == 5.2

	def test_already_claimed_counts_as_success(self, monkeypatch):
		self._fake_flow(
			monkeypatch, [(1.0, 0.0), (1.0, 0.0)], checkin_resp=_r(False, message='您今天已经签到过，请勿重复签到')
		)
		ok, before, after = nj.newapi_jwt_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is True
		assert before['quota'] == after['quota'] == 1.0

	def test_checkin_failure(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], checkin_resp=_r(False, message='签到失败'))
		ok, before, after = nj.newapi_jwt_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert after is None

	def test_login_failure(self, monkeypatch):
		self._fake_flow(monkeypatch, [(1.0, 0.0)], login_ok=False)
		ok, before, after = nj.newapi_jwt_checkin('A', 'e@mail.com', 'pw', DOMAIN)
		assert ok is False
		assert before['success'] is False
		assert after is None


class TestProviderConfig:
	def test_nianhua_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('nianhua')
		assert p is not None
		assert p.auth_method == 'newapi_jwt'
		assert p.domain == 'https://us-3.nianhuaapi.com'
		assert p.use_proxy is False

	def test_superapi_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('superapi')
		assert p is not None
		assert p.auth_method == 'newapi_jwt'
		assert p.domain == 'https://superapi.buzz'

	def test_kuaipao_registered(self):
		cfg = AppConfig.load_from_env()
		p = cfg.get_provider('kuaipao')
		assert p is not None
		assert p.auth_method == 'newapi_jwt'
		assert p.domain == 'https://kuaipao.ai'
		assert p.sign_in_path == '/api/user/checkin'
		assert p.user_info_path == '/api/user/self'
		assert p.api_user_key is None
		assert p.use_proxy is False
