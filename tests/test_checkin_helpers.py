"""checkin.py 纯函数与请求分支测试（全离线，用 MockTransport 拦截，不触碰真实账号）。"""

import sys
from pathlib import Path

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import (
	_format_amount,
	execute_check_in,
	format_check_in_notification,
	get_user_info,
	parse_cookies,
	run_check_in_requests,
)
from utils.config import AccountConfig, ProviderConfig
from utils.http_client import QUOTA_PER_DOLLAR


def _mock_client(handler) -> httpx.Client:
	return httpx.Client(transport=httpx.MockTransport(handler))


def _detail(**overrides) -> dict:
	detail = {
		'name': 'Account 1',
		'before_quota': 100.0,
		'before_used': 20.0,
		'after_quota': 125.0,
		'after_used': 20.0,
		'check_in_reward': 25.0,
		'usage_increase': 0.0,
		'balance_change': 25.0,
		'unit': 'usd',
	}
	detail.update(overrides)
	return detail


class TestParseCookies:
	def test_dict_passes_through(self):
		assert parse_cookies({'session': 'abc'}) == {'session': 'abc'}

	def test_parses_cookie_header_string(self):
		assert parse_cookies('session=abc; acw_tc=xyz') == {'session': 'abc', 'acw_tc': 'xyz'}

	def test_keeps_equals_sign_inside_value(self):
		assert parse_cookies('token=a=b=c') == {'token': 'a=b=c'}

	def test_skips_segments_without_equals(self):
		assert parse_cookies('session=abc; garbage; acw_tc=xyz') == {'session': 'abc', 'acw_tc': 'xyz'}

	def test_returns_empty_dict_for_unsupported_types(self):
		assert parse_cookies(None) == {}
		assert parse_cookies(123) == {}
		assert parse_cookies([]) == {}


class TestFormatAmount:
	def test_usd_uses_two_decimals(self):
		assert _format_amount(12.5, 'usd') == '$12.50'

	def test_credits_use_suffix_without_trailing_zeros(self):
		assert _format_amount(120.0, 'credits') == '120 积分'

	def test_unknown_unit_defaults_to_usd(self):
		assert _format_amount(3.0, 'whatever') == '$3.00'


class TestFormatCheckInNotification:
	def test_renders_usd_amounts(self):
		text = format_check_in_notification(_detail())

		assert '余额: $100.00' in text
		assert '签到获得: +$25.00' in text
		assert '积分' not in text

	def test_renders_credits_amounts(self):
		text = format_check_in_notification(
			_detail(
				before_quota=100.0,
				after_quota=150.0,
				before_used=0.0,
				after_used=0.0,
				check_in_reward=50.0,
				balance_change=50.0,
				unit='credits',
			)
		)

		assert '余额: 100 积分' in text
		assert '签到获得: +50 积分' in text
		assert '$' not in text

	def test_no_change_branch(self):
		text = format_check_in_notification(
			_detail(
				after_quota=100.0,
				check_in_reward=0.0,
				usage_increase=0.0,
				balance_change=0.0,
			)
		)

		assert '今日已签到，无变化' in text

	def test_usage_without_reward_branch(self):
		text = format_check_in_notification(
			_detail(
				after_quota=95.0,
				after_used=25.0,
				check_in_reward=0.0,
				usage_increase=5.0,
				balance_change=-5.0,
			)
		)

		assert '今日已签到（期间有使用）' in text
		assert '期间消耗: $5.00' in text
		assert '余额变化: $-5.00' in text

	def test_account_name_is_included(self):
		assert '[CHECK-IN] 账号一' in format_check_in_notification(_detail(name='账号一'))


class TestGetUserInfo:
	def test_converts_quota_to_dollars(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(
				200,
				json={
					'success': True,
					'data': {'quota': 100 * QUOTA_PER_DOLLAR, 'used_quota': 25 * QUOTA_PER_DOLLAR},
				},
			)

		with _mock_client(handler) as client:
			info = get_user_info(client, {}, 'https://example.com/api/user/self')

		assert info == {
			'success': True,
			'quota': 100.0,
			'used_quota': 25.0,
			'display': ':money: 当前余额: $100.0, 已用: $25.0',
		}

	def test_missing_quota_fields_default_to_zero(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': True, 'data': {}})

		with _mock_client(handler) as client:
			info = get_user_info(client, {}, 'https://example.com/api/user/self')

		assert info['quota'] == 0
		assert info['used_quota'] == 0

	def test_api_level_failure_is_reported(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': False, 'message': 'unauthorized'})

		with _mock_client(handler) as client:
			info = get_user_info(client, {}, 'https://example.com/api/user/self')

		assert info['success'] is False

	def test_http_error_is_reported(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(403)

		with _mock_client(handler) as client:
			info = get_user_info(client, {}, 'https://example.com/api/user/self')

		assert info['success'] is False
		assert 'HTTP 403' in info['error']

	def test_network_error_is_propagated_for_node_retry(self):
		def handler(request: httpx.Request) -> httpx.Response:
			raise httpx.ConnectError('node unavailable', request=request)

		with _mock_client(handler) as client, pytest.raises(httpx.ConnectError):
			get_user_info(client, {}, 'https://example.com/api/user/self')

		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, text='<html>WAF</html>')

		with _mock_client(handler) as client:
			info = get_user_info(client, {}, 'https://example.com/api/user/self')

		assert info['success'] is False
		assert '获取用户信息失败' in info['error']


class TestExecuteCheckIn:
	provider = ProviderConfig(name='demo', domain='https://demo.example.com', sign_in_path='/api/user/sign_in')

	@pytest.mark.parametrize('payload', [{'ret': 1}, {'code': 0}, {'success': True}])
	def test_success_payload_variants(self, payload):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json=payload)

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is True

	def test_already_checked_in_counts_as_success(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': False, 'msg': '今日已经签到过了'})

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is True

	def test_genuine_failure_returns_false(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': False, 'msg': '风控拦截'})

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is False

	def test_non_json_body_with_success_keyword_passes(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, text='SUCCESS')

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is True

	def test_non_json_body_without_keyword_fails(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, text='<html>blocked</html>')

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is False

	def test_http_error_returns_false(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(401)

		with _mock_client(handler) as client:
			assert execute_check_in(client, 'Account 1', self.provider, {}) is False

	def test_request_targets_sign_in_path_with_ajax_headers(self):
		seen = {}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['url'] = str(request.url)
			seen['method'] = request.method
			seen['requested_with'] = request.headers.get('X-Requested-With')
			seen['new_api_user'] = request.headers.get('new-api-user')
			return httpx.Response(200, json={'success': True})

		with _mock_client(handler) as client:
			execute_check_in(client, 'Account 1', self.provider, {'new-api-user': '42'})

		assert seen['url'] == 'https://demo.example.com/api/user/sign_in'
		assert seen['method'] == 'POST'
		assert seen['requested_with'] == 'XMLHttpRequest'
		assert seen['new_api_user'] == '42'

	def test_caller_headers_are_not_mutated(self):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': True})

		headers = {'Referer': 'https://demo.example.com'}
		with _mock_client(handler) as client:
			execute_check_in(client, 'Account 1', self.provider, headers)

		assert headers == {'Referer': 'https://demo.example.com'}


class TestRunCheckInRequests:
	"""run_check_in_requests 的请求头/分支验证。create_client 被替换为 MockTransport 客户端。"""

	@staticmethod
	def _patch_client(monkeypatch, handler) -> None:
		def fake_create_client(*, headers=None, use_proxy=False, **_kwargs):
			from utils.http_client import API_HEADERS as defaults

			client = httpx.Client(transport=httpx.MockTransport(handler))
			merged = dict(defaults)
			if headers:
				merged.update(headers)
			client.headers.update(merged)
			return client

		monkeypatch.setattr('checkin.create_client', fake_create_client)
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

	@staticmethod
	def _quota_response(quota: float, used: float) -> httpx.Response:
		return httpx.Response(
			200,
			json={
				'success': True,
				'data': {'quota': quota * QUOTA_PER_DOLLAR, 'used_quota': used * QUOTA_PER_DOLLAR},
			},
		)

	def test_manual_check_in_provider_calls_sign_in(self, monkeypatch):
		seen: dict = {'paths': []}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['paths'].append(request.url.path)
			if request.url.path == '/api/user/sign_in':
				return httpx.Response(200, json={'success': True})
			return self._quota_response(100.0, 20.0)

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(name='demo', domain='https://demo.example.com')
		account = AccountConfig(cookies={'session': 'abc'}, api_user='42')

		success, before, after = run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider)

		assert success is True
		assert before is not None and before['quota'] == 100.0
		assert after is not None and after['quota'] == 100.0
		assert seen['paths'].count('/api/user/self') == 2
		assert '/api/user/sign_in' in seen['paths']

	def test_auto_check_in_provider_skips_sign_in(self, monkeypatch):
		paths: list[str] = []

		def handler(request: httpx.Request) -> httpx.Response:
			paths.append(request.url.path)
			return self._quota_response(50.0, 5.0)

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(name='auto', domain='https://auto.example.com', sign_in_path=None)
		account = AccountConfig(cookies={'session': 'abc'}, api_user='7')

		success, _before, after = run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider)

		assert success is True
		assert after is not None and after['quota'] == 50.0
		assert '/api/user/sign_in' not in paths

	def test_api_user_header_uses_provider_key(self, monkeypatch):
		seen: dict = {}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['api_user'] = request.headers.get('x-user-id')
			seen['ua'] = request.headers.get('User-Agent')
			seen['platform'] = request.headers.get('sec-ch-ua-platform')
			seen['cookie'] = request.headers.get('Cookie')
			return self._quota_response(1.0, 0.0)

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(
			name='demo',
			domain='https://demo.example.com',
			sign_in_path=None,
			api_user_key='x-user-id',
		)
		account = AccountConfig(cookies={'session': 'abc'}, api_user='99')

		run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider)

		assert seen['api_user'] == '99'
		# UA 与 Client Hints 必须来自同一套 API_HEADERS，避免指纹自相矛盾
		assert 'Windows NT 10.0' in seen['ua']
		assert seen['platform'] == '"Windows"'
		assert 'session=abc' in seen['cookie']

	def test_api_user_override_wins_over_account_value(self, monkeypatch):
		seen: dict = {}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['api_user'] = request.headers.get('new-api-user')
			return self._quota_response(1.0, 0.0)

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(name='demo', domain='https://demo.example.com', sign_in_path=None)
		account = AccountConfig(cookies={'session': 'abc'}, api_user='11')

		run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider, api_user_override='22')

		assert seen['api_user'] == '22'

	def test_provider_without_api_user_key_omits_header(self, monkeypatch):
		seen: dict = {}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['headers'] = dict(request.headers)
			return self._quota_response(1.0, 0.0)

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(name='demo', domain='https://demo.example.com', sign_in_path=None, api_user_key=None)
		account = AccountConfig(cookies={'session': 'abc'}, api_user='99')

		run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider)

		assert 'new-api-user' not in seen['headers']

	def test_auto_provider_failure_returns_false(self, monkeypatch):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(200, json={'success': False, 'message': 'unauthorized'})

		self._patch_client(monkeypatch, handler)
		provider = ProviderConfig(name='auto', domain='https://auto.example.com', sign_in_path=None)
		account = AccountConfig(cookies={'session': 'abc'}, api_user='7')

		success, _before, after = run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider)

		assert success is False
		assert after is not None and after['success'] is False

	def test_client_error_is_swallowed_into_false(self, monkeypatch):
		def boom(**_kwargs):
			raise RuntimeError('client init failed')

		monkeypatch.setattr('checkin.create_client', boom)
		provider = ProviderConfig(name='demo', domain='https://demo.example.com')
		account = AccountConfig(cookies={'session': 'abc'}, api_user='42')

		assert run_check_in_requests({'session': 'abc'}, account, 'Account 1', provider) == (False, None, None)
