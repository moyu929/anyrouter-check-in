"""checkin.py 中纯函数与 HTTP 签到流程单元测试（全离线 mock，不出网不真签到）。"""

from __future__ import annotations

import httpx
import pytest

import checkin as checkin_module
from checkin import (
	_format_amount,
	check_in_account,
	execute_check_in,
	format_check_in_notification,
	generate_balance_hash,
	get_user_info,
	load_balance_hash,
	parse_cookies,
	run_check_in_requests,
	save_balance_hash,
)
from utils.config import AccountConfig, AppConfig, ProviderConfig

# ============================================================
# 余额哈希与持久化
# ============================================================


class TestBalanceHash:
	@pytest.fixture
	def hash_file(self, tmp_path, monkeypatch):
		"""将 BALANCE_HASH_FILE 指向临时目录，避免脏写工作区。"""
		p = tmp_path / 'balance_hash.txt'
		monkeypatch.setattr(checkin_module, 'BALANCE_HASH_FILE', str(p))
		return p

	def test_generate_hash_is_deterministic(self):
		balances = {
			'acc_1': {'quota': 1.5, 'used': 0.3},
			'acc_2': {'quota': 2.0, 'used': 0.0},
		}
		h1 = generate_balance_hash(balances)
		h2 = generate_balance_hash(balances)
		assert h1 == h2
		assert isinstance(h1, str)
		assert len(h1) == 16  # sha256 前 16 位

	def test_generate_hash_differs_when_value_changes(self):
		b1 = {'acc_1': {'quota': 1.5, 'used': 0.3}}
		b2 = {'acc_1': {'quota': 1.6, 'used': 0.3}}
		assert generate_balance_hash(b1) != generate_balance_hash(b2)

	def test_generate_hash_ignores_extra_keys(self):
		"""仅 quota / used 两个键参与 hash，其他键忽略。"""
		b1 = {'acc_1': {'quota': 1.0, 'used': 0.1, 'foo': 'x'}}
		b2 = {'acc_1': {'quota': 1.0, 'used': 0.1, 'foo': 'y'}}
		assert generate_balance_hash(b1) == generate_balance_hash(b2)

	def test_generate_hash_empty_is_not_none(self):
		assert generate_balance_hash({}) is not None
		assert generate_balance_hash(None) is not None

	def test_save_and_load_roundtrip(self, hash_file):
		h = 'abcdef1234567890'
		save_balance_hash(h)
		assert load_balance_hash() == h

	def test_load_missing_file_returns_none(self, hash_file):
		assert load_balance_hash() is None

	def test_load_corrupt_file_is_handled(self, hash_file):
		hash_file.write_bytes(b'\x00\x01broken\xff')
		# 异常被吞掉，返回 None
		assert load_balance_hash() in (None,)  # 读不出来即 None


# ============================================================
# Cookie 解析
# ============================================================


class TestParseCookies:
	def test_dict_passthrough(self):
		d = {'a': '1', 'b': '2'}
		assert parse_cookies(d) == d

	def test_string_semicolon_format(self):
		s = 'session=abc; token=xyz; empty='
		got = parse_cookies(s)
		assert got == {'session': 'abc', 'token': 'xyz', 'empty': ''}

	def test_string_entries_without_equals_ignored(self):
		s = 'session=abc; strayvalue; token=xyz'
		got = parse_cookies(s)
		assert got == {'session': 'abc', 'token': 'xyz'}

	def test_string_with_extra_separators(self):
		"""两端分号不影响解析。"""
		s = ';session=abc;token=xyz;'
		got = parse_cookies(s)
		assert got['session'] == 'abc'
		assert got['token'] == 'xyz'

	def test_other_types_return_empty_dict(self):
		assert parse_cookies(None) == {}
		assert parse_cookies(123) == {}
		assert parse_cookies([]) == {}


# ============================================================
# 金额格式化与通知格式化
# ============================================================


class TestFormatAmount:
	def test_usd_formats_with_dollar_sign(self):
		assert _format_amount(3.14, 'usd') == '$3.14'
		assert _format_amount(0, 'usd') == '$0.00'

	def test_credits_formats_as_integer_with_suffix(self):
		assert _format_amount(500.0, 'credits') == '500 积分'
		assert _format_amount(123.456, 'credits') == '123.456 积分'

	def test_default_unit_is_usd_like(self):
		assert _format_amount(1.5, 'anything') == '$1.50'


class TestFormatCheckInNotification:
	@staticmethod
	def _base_detail(**overrides):
		base = {
			'name': '账号A',
			'unit': 'usd',
			'before_quota': 10.0,
			'before_used': 1.0,
			'after_quota': 10.5,
			'after_used': 1.2,
			'check_in_reward': 0.5,
			'usage_increase': 0.2,
			'balance_change': 0.3,
		}
		base.update(overrides)
		return base

	def test_happy_path_includes_reward_and_usage(self):
		text = format_check_in_notification(self._base_detail())
		assert '**账号A**' in text
		assert '签到前余额: $10.00' in text
		assert '签到后余额: $10.50' in text
		assert '签到获得: +$0.50' in text
		assert '累积消耗: $1.20' in text

	def test_already_checked_no_change(self):
		detail = self._base_detail(
			after_quota=10.0,
			after_used=1.0,
			check_in_reward=0,
			usage_increase=0,
			balance_change=0,
		)
		text = format_check_in_notification(detail)
		assert '今日已签到' in text
		assert '签到获得' not in text
		assert '当前余额: $10.00' in text

	def test_used_only_without_reward(self):
		detail = self._base_detail(
			after_quota=9.8,
			after_used=1.2,
			check_in_reward=0,
			usage_increase=0.2,
			balance_change=-0.2,
		)
		text = format_check_in_notification(detail)
		assert '今日已签到（期间有消耗）' in text
		assert '签到获得' not in text
		assert '签到前余额: $10.00' in text
		assert '签到后余额: $9.80' in text

	def test_credits_unit_applied(self):
		detail = self._base_detail(unit='credits', before_quota=500, check_in_reward=100)
		text = format_check_in_notification(detail)
		assert '500 积分' in text
		assert '+100 积分' in text

	def test_negative_reward_omitted_when_no_change(self):
		# 负奖励（余额减少）按无奖励分支处理，不显示"签到获得"负数行
		detail = self._base_detail(check_in_reward=-0.5, usage_increase=0)
		text = format_check_in_notification(detail)
		assert '签到获得' not in text


# ============================================================
# get_user_info — 用 MockTransport 拦截 HTTP
# ============================================================


class TestGetUserInfo:
	@staticmethod
	def _client(handler):
		return httpx.Client(transport=httpx.MockTransport(handler))

	def test_success_returns_quota_in_dollars(self):
		# 500_000 quota = $1.0 (round to 2 decimal places: 1.0), 250_000 used = $0.5
		def h(req):
			return httpx.Response(200, json={'success': True, 'data': {'quota': 500000, 'used_quota': 250000}})

		with self._client(h) as c:
			info = get_user_info(c, {}, 'https://example.com/api/user/self')

		assert info['success'] is True
		assert info['quota'] == 1.0
		assert info['used_quota'] == 0.5
		# round(500000 / 500000, 2) = 1.0 → 显示为 $1.0, 不是 $1.00
		assert '$1.0' in info['display']
		assert '$0.5' in info['display']

	def test_non_200_retryable_error_is_propagated(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		def h(req):
			return httpx.Response(500)

		with self._client(h) as c, pytest.raises(RuntimeError, match='HTTP 500'):
			get_user_info(c, {}, 'https://example.com/api/user/self')

	def test_success_false_payload_returns_error(self):
		def h(req):
			return httpx.Response(200, json={'success': False})

		with self._client(h) as c:
			info = get_user_info(c, {}, 'https://example.com/api/user/self')

		assert info['success'] is False

	def test_network_exception_is_propagated_for_proxy_retry(self):
		def h(req):
			raise httpx.ConnectError('boom', request=req)

		with self._client(h) as c, pytest.raises(httpx.ConnectError):
			get_user_info(c, {}, 'https://example.com/api/user/self')

	def test_invalid_json_is_reported_as_failure(self):
		def h(req):
			return httpx.Response(200, text='<html>not json</html>')

		with self._client(h) as c:
			info = get_user_info(c, {}, 'https://example.com/api/user/self')

		assert info['success'] is False
		assert '获取用户信息失败' in info['error']

	def test_network_error_not_propagated_when_post_checkin(self, monkeypatch):
		"""签到成功后查询余额的网络异常不应向上抛（避免重复签到）。"""
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		def h(req):
			raise httpx.ConnectError('boom', request=req)

		with self._client(h) as c:
			info = get_user_info(c, {}, 'https://example.com/api/user/self', propagate_network_error=False)

		assert info['success'] is False
		assert '获取用户信息失败' in info['error']

	def test_retry_exhausted_not_propagated_when_post_checkin(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		def h(req):
			return httpx.Response(500)

		with self._client(h) as c:
			info = get_user_info(c, {}, 'https://example.com/api/user/self', propagate_network_error=False)

		assert info['success'] is False
		assert '获取用户信息失败' in info['error']


# ============================================================


class TestExecuteCheckIn:
	@staticmethod
	def _provider():
		return ProviderConfig(
			name='tp',
			domain='https://example.com',
			sign_in_path='/api/user/checkin',
		)

	@staticmethod
	def _client(handler):
		return httpx.Client(transport=httpx.MockTransport(handler))

	def test_ret_code_0_is_success(self):
		def h(req):
			assert req.method == 'POST'
			assert req.url.path == '/api/user/checkin'
			return httpx.Response(200, json={'code': 0, 'msg': 'ok'})

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is True

	def test_ret_1_is_success(self):
		def h(req):
			return httpx.Response(200, json={'ret': 1})

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is True

	def test_success_bool_is_success(self):
		def h(req):
			return httpx.Response(200, json={'success': True})

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is True

	def test_already_checked_keywords_treated_as_success(self):
		for keyword in ['已经签到', '已签到', '重复签到', 'already checked', 'Already Signed!']:

			def h(req, kw=keyword):
				return httpx.Response(200, json={'code': 1, 'msg': kw})

			with self._client(h) as c:
				assert execute_check_in(c, 'TestAcc', self._provider(), {}) is True

	def test_other_error_is_failure(self):
		def h(req):
			return httpx.Response(200, json={'code': 4001, 'msg': 'invalid session'})

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is False

	def test_non_json_with_success_keyword_is_success(self):
		def h(req):
			return httpx.Response(200, text='<html>sign in success</html>')

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is True

	def test_non_json_no_success_keyword_is_failure(self):
		def h(req):
			return httpx.Response(200, text='<html>error page</html>')

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is False

	def test_http_400_client_error_is_failure(self):
		"""4xx 不重试，直接返回 False。"""

		def h(req):
			return httpx.Response(400, json={'success': False})

		with self._client(h) as c:
			assert execute_check_in(c, 'TestAcc', self._provider(), {}) is False

	def test_headers_include_xhr_and_json_content_type(self):
		captured = {}

		def h(req):
			captured['content_type'] = req.headers.get('Content-Type')
			captured['xhr'] = req.headers.get('X-Requested-With')
			return httpx.Response(200, json={'success': True})

		with self._client(h) as c:
			execute_check_in(c, 'TestAcc', self._provider(), {'Referer': 'https://example.com'})

		assert captured['content_type'] == 'application/json'
		assert captured['xhr'] == 'XMLHttpRequest'


# ============================================================
# run_check_in_requests — mock HTTP 客户端层，测流程分支
# ============================================================


class _FakeProvider:
	"""最小 ProviderConfig 替身（仅暴露 run_check_in_requests 实际用到的属性）。"""

	def __init__(
		self,
		*,
		domain='https://example.com',
		user_info_path='/api/user/self',
		sign_in_path='/api/user/checkin',
		api_user_key='x-api-user',
		use_proxy=False,
	):
		self.domain = domain
		self.user_info_path = user_info_path
		self.sign_in_path = sign_in_path
		self.api_user_key = api_user_key
		self.use_proxy = use_proxy

	def needs_manual_check_in(self) -> bool:
		return self.sign_in_path is not None


class TestRunCheckInRequests:
	@pytest.fixture(autouse=True)
	def _patch_create_client(self, monkeypatch):
		"""所有 run_check_in_requests 内 create_client 返回的 httpx.Client 统一使用传入的 transport。"""
		self._transports = {}

	def _install_handler(self, monkeypatch, handler_map: dict):
		"""根据 path 分发到不同 handler。"""

		def _create_client_factory(**_kwargs):
			def router(req: httpx.Request) -> httpx.Response:
				path = req.url.path
				for p, h in handler_map.items():
					if path.endswith(p):
						return h(req)
				# 默认：500
				return httpx.Response(500, text=f'no handler for {path}')

			return httpx.Client(transport=httpx.MockTransport(router))

		monkeypatch.setattr(checkin_module, 'create_client', lambda **kw: _create_client_factory(**kw))
		monkeypatch.setattr(checkin_module, 'is_proxy_configured', lambda: False)

	@staticmethod
	def _account():
		return AccountConfig(cookies={'session': 'abc'}, provider='tp', api_user='42')

	def test_manual_check_in_happy_path(self, monkeypatch):
		handlers = {
			'/api/user/self': lambda req: httpx.Response(
				200,
				# 第一次 user_info: 1.00, 第二次: 1.50
				json={'success': True, 'data': {'quota': 500000 if _before(req) else 750000, 'used_quota': 0}},
			),
			'/api/user/checkin': lambda req: httpx.Response(200, json={'success': True}),
		}

		def _before(req: httpx.Request) -> bool:
			_before.c += 1
			return _before.c <= 1

		_before.c = 0  # type: ignore[attr-defined]

		self._install_handler(monkeypatch, handlers)

		ok, before, after = run_check_in_requests(
			{'session': 'abc'},
			self._account(),
			'Acc1',
			_FakeProvider(),
		)

		assert ok is True
		assert before and before['success'] is True
		assert after and after['success'] is True
		assert before['quota'] == 1.0
		assert after['quota'] == 1.5

	def test_auto_check_in_no_button_success(self, monkeypatch):
		"""sign_in_path=None — 仅靠两次 user_info 请求即可视为自动签到成功。"""

		def user_info_handler(req):
			return httpx.Response(200, json={'success': True, 'data': {'quota': 500000, 'used_quota': 50000}})

		self._install_handler(monkeypatch, {'/api/user/self': user_info_handler})

		provider = _FakeProvider(sign_in_path=None)
		# 使 needs_manual_check_in 返回 False
		provider.__dict__['needs_manual_check_in'] = lambda: False

		ok, before, after = run_check_in_requests(
			{'session': 'abc'},
			self._account(),
			'Acc2',
			provider,
		)

		assert ok is True
		assert after and after['success'] is True

	def test_auto_check_in_user_info_failure(self, monkeypatch):
		"""自动签到型：登录成功即签到完成，余额查询 401 仅影响展示，不判签到失败。"""

		def user_info_handler(req):
			return httpx.Response(401, json={'success': False})

		self._install_handler(monkeypatch, {'/api/user/self': user_info_handler})

		provider = _FakeProvider(sign_in_path=None)
		provider.__dict__['needs_manual_check_in'] = lambda: False

		ok, before, after = run_check_in_requests(
			{'session': 'abc'},
			self._account(),
			'Acc3',
			provider,
		)

		assert ok is True  # 签到已随登录完成
		assert before and before['success'] is False
		assert after and after['success'] is False

	def test_auto_check_in_waf_block_does_not_raise(self, monkeypatch):
		"""自动签到型：余额查询被 WAF 拦截且浏览器兜底失败 → 不抛 ProxyNodeIssue
		（不切节点重新登录），返回成功 + 失败 dict（余额未知）。"""
		from checkin import ProxyNodeIssue

		def waf_handler(req):
			return httpx.Response(200, text='<html>aliyun_waf_aa challenge</html>')

		self._install_handler(monkeypatch, {'/api/user/self': waf_handler})
		# 浏览器兜底失败（返回 None）
		monkeypatch.setattr(checkin_module, '_fetch_user_info_via_browser_sync', lambda *a, **kw: None)

		provider = _FakeProvider(sign_in_path=None)
		provider.__dict__['needs_manual_check_in'] = lambda: False

		try:
			ok, before, after = run_check_in_requests(
				{'session': 'abc'},
				self._account(),
				'AccW',
				provider,
			)
		except ProxyNodeIssue:
			pytest.fail('自动签到型余额查询失败不应抛 ProxyNodeIssue 触发重新登录')

		assert ok is True
		assert before and before['success'] is False and 'WAF' in before['error']
		assert after and after['success'] is False

	def test_api_user_key_header_is_set(self, monkeypatch):
		captured_headers = {}

		def user_info_handler(req):
			captured_headers.update(req.headers)
			return httpx.Response(200, json={'success': True, 'data': {'quota': 1, 'used_quota': 0}})

		self._install_handler(monkeypatch, {'/api/user/self': user_info_handler})

		provider = _FakeProvider(sign_in_path=None)
		provider.__dict__['needs_manual_check_in'] = lambda: False

		acc = AccountConfig(cookies={'session': 'abc'}, provider='tp', api_user='USER99')
		run_check_in_requests({'session': 'abc'}, acc, 'Acc4', provider)

		# x-api-user 头被正确注入
		assert captured_headers.get('x-api-user') == 'USER99'


# ============================================================
# 异常分支：run_check_in_requests 网络异常 → ProxyNodeIssue
# ============================================================


class TestRunCheckInRequestsNetworkErrors:
	def test_connect_error_raises_proxy_node_issue(self, monkeypatch):
		"""manual 模式下 signin 请求网络异常耗尽重试 → _NETWORK_ERRORS → ProxyNodeIssue。"""
		# 加速：关闭重试退避 sleep
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		monkeypatch.setenv('RETRY_TIMES', '0')

		call_count = {'n': 0}

		def router(req):
			call_count['n'] += 1
			path = req.url.path
			if path.endswith('/api/user/self'):
				# user_info 请求两次都 OK，不吞异常
				return httpx.Response(200, json={'success': True, 'data': {'quota': 500000, 'used_quota': 0}})
			if path.endswith('/api/user/checkin'):
				# 签到请求抛网络异常
				raise httpx.ConnectError('refused', request=req)
			return httpx.Response(404)

		def create(**_kw):
			return httpx.Client(transport=httpx.MockTransport(router))

		monkeypatch.setattr(checkin_module, 'create_client', create)
		monkeypatch.setattr(checkin_module, 'is_proxy_configured', lambda: False)

		provider = _FakeProvider(sign_in_path='/api/user/checkin')
		provider.__dict__['needs_manual_check_in'] = lambda: True

		from checkin import ProxyNodeIssue

		with pytest.raises(ProxyNodeIssue):
			run_check_in_requests(
				{'session': 'abc'},
				AccountConfig(cookies={'s': '1'}, provider='tp'),
				'AccX',
				provider,
			)

	def test_generic_exception_returns_failure_tuple(self, monkeypatch):
		def create(**_kw):
			raise RuntimeError('factory boom')

		monkeypatch.setattr(checkin_module, 'create_client', create)

		ok, before, after = run_check_in_requests(
			{'session': 'abc'},
			AccountConfig(cookies={'s': '1'}, provider='tp'),
			'AccY',
			_FakeProvider(),
		)

		assert ok is False
		assert before is None
		assert after['error'] == '签到过程异常: factory boom'


# ============================================================
# check_in_account 各种分支：无真实网络，全部 monkeypatch 子调用
# ============================================================


class _FakeProvider2:
	"""可编程的 ProviderConfig 替身，dataclass 风格字段。"""

	def __init__(
		self,
		*,
		auth_method='session',
		domain='https://example.com',
		name='tp',
		use_proxy=False,
	):
		self.auth_method = auth_method
		self.domain = domain
		self.provider_name = name
		self.use_proxy = use_proxy

	def is_oauth(self):
		return self.auth_method == 'github_oauth'

	def needs_waf_cookies(self):
		return False


class TestCheckInAccount:
	def _app(self, monkeypatch, provider: _FakeProvider2) -> AppConfig:
		app = AppConfig(providers={})
		monkeypatch.setattr(app, 'get_provider', lambda _n: provider)
		return app

	@staticmethod
	def _run(coro):
		import asyncio

		return asyncio.run(coro)

	# ----------------------------------------------------- 分支 1：无效 provider
	def test_unknown_provider_returns_failure(self, monkeypatch):
		app = AppConfig(providers={})
		monkeypatch.setattr(app, 'get_provider', lambda _n: None)
		acc = AccountConfig(cookies={'s': '1'}, provider='bogus')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == '提供商 "bogus" 未在配置中找到'

	# ----------------------------------------------------- 分支 2：gptgod（有凭据 → 成功）
	def test_gptgod_with_credentials_dispatches_to_gptgod_checkin(self, monkeypatch):
		trace = {}

		def fake_checkin(account_name, email, password, use_proxy):
			trace.update({'account_name': account_name, 'email': email, 'password': password, 'use_proxy': use_proxy})
			return True, {'success': True}, {'success': True}

		monkeypatch.setattr(checkin_module, 'gptgod_checkin', fake_checkin)

		provider = _FakeProvider2(auth_method='gptgod')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(
			email='a@b.com',
			password='pw',
			cookies=None,
			provider='tp',
		)

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is True
		assert trace['email'] == 'a@b.com'
		assert trace['password'] == 'pw'

	# ----------------------------------------------------- 分支 3：gptgod（无凭据 → 失败）
	def test_gptgod_without_credentials_fails(self, monkeypatch):
		provider = _FakeProvider2(auth_method='gptgod')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(cookies={'s': '1'}, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == 'GPTGod 提供商需要邮箱和密码'

	# ----------------------------------------------------- 分支 4：OAuth 成功
	def test_github_oauth_success_runs_requests(self, monkeypatch):
		from utils.browser import BrowserLoginResult

		async def fake_oauth(acc_name, provider_cfg, gh_session, *, force_direct=False):
			fake_oauth.called = True
			fake_oauth.force_direct = force_direct
			return BrowserLoginResult(cookies={'session': 'got'}, api_user='uid_42')

		fake_oauth.called = False
		monkeypatch.setattr(checkin_module, 'login_with_github_oauth', fake_oauth)

		def fake_run(cookies, account, account_name, provider, **kw):
			fake_run.cookies = cookies
			fake_run.kw = kw
			return True, {'a': 1}, {'a': 2}

		monkeypatch.setattr(checkin_module, 'run_check_in_requests', fake_run)

		provider = _FakeProvider2(auth_method='github_oauth')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(github_session='gh_sess_123', cookies=None, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is True
		assert fake_oauth.called is True
		assert fake_run.cookies == {'session': 'got'}
		assert fake_run.kw['api_user_override'] == 'uid_42'

	# ----------------------------------------------------- 分支 5：OAuth 失败
	def test_github_oauth_failure_returns_failure(self, monkeypatch):
		async def fake_oauth(acc_name, provider_cfg, gh_session, *, force_direct=False):
			return None

		monkeypatch.setattr(checkin_module, 'login_with_github_oauth', fake_oauth)

		provider = _FakeProvider2(auth_method='github_oauth')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(github_session='gh_sess_123', cookies=None, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == 'OAuth 登录失败（详见运行日志）'

	# ----------------------------------------------------- 分支 6：邮箱密码登录成功
	def test_credentials_login_success_runs_requests(self, monkeypatch):
		from utils.browser import BrowserLoginResult

		async def fake_login(acc_name, provider_cfg, provider_name, email, password):
			fake_login.cred = (email, password)
			return BrowserLoginResult(cookies={'c': 'ok'}, api_user=None)

		monkeypatch.setattr(checkin_module, 'login_with_credentials', fake_login)

		def fake_run(cookies, *_a, **_kw):
			return True, None, None

		monkeypatch.setattr(checkin_module, 'run_check_in_requests', fake_run)

		provider = _FakeProvider2(auth_method='email_password')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(email='u@c', password='pw0', cookies=None, provider='tp')

		ok, *_ = self._run(check_in_account(acc, 0, app))
		assert ok is True
		assert fake_login.cred == ('u@c', 'pw0')

	# ----------------------------------------------------- 分支 7：邮箱密码登录失败
	def test_credentials_login_failure_returns_failure(self, monkeypatch):
		async def fake_login(*_a, **_kw):
			return None

		monkeypatch.setattr(checkin_module, 'login_with_credentials', fake_login)

		provider = _FakeProvider2(auth_method='email_password')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(email='u@c', password='pw0', cookies=None, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == '邮箱密码登录失败（详见运行日志）'

	# ----------------------------------------------------- 分支 8：cookies 分支但 cookies 空
	def test_session_cookies_empty_dict_fails(self, monkeypatch):
		provider = _FakeProvider2(auth_method='session')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(cookies={}, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == '账号配置格式无效（无有效凭据）'

	# ----------------------------------------------------- 分支 9：prepare_cookies 返回 None → 失败
	def test_prepare_cookies_none_fails(self, monkeypatch):
		async def fake_prepare(acc_name, provider_cfg, user_cookies):
			return None

		monkeypatch.setattr(checkin_module, 'prepare_cookies', fake_prepare)

		provider = _FakeProvider2(auth_method='session')
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(cookies={'s': 'x'}, provider='tp')

		ok, before, after = self._run(check_in_account(acc, 0, app))
		assert ok is False
		assert before is None
		assert after['error'] == 'cookies 准备失败（WAF cookies 获取失败，详见运行日志）'

	# ----------------------------------------------------- 分支 10：纯 cookies 路径成功
	def test_session_cookies_success_runs_requests(self, monkeypatch):
		async def fake_prepare(acc_name, provider_cfg, user_cookies):
			return {**user_cookies, 'waf': 'passed'}

		monkeypatch.setattr(checkin_module, 'prepare_cookies', fake_prepare)

		def fake_run(cookies, *_a, **kw):
			fake_run.cookies = cookies
			fake_run.use_proxy = kw.get('use_proxy')
			return True, None, None

		monkeypatch.setattr(checkin_module, 'run_check_in_requests', fake_run)

		provider = _FakeProvider2(auth_method='session', use_proxy=True)
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(cookies={'s': 'abc'}, provider='tp')

		ok, *_ = self._run(check_in_account(acc, 0, app))
		assert ok is True
		assert fake_run.cookies == {'s': 'abc', 'waf': 'passed'}
		assert fake_run.use_proxy is True

	# ----------------------------------------------------- 分支 11：force_direct 取消代理
	def test_force_direct_disables_proxy_flag(self, monkeypatch):
		async def fake_prepare(acc_name, provider_cfg, user_cookies):
			return dict(user_cookies)

		monkeypatch.setattr(checkin_module, 'prepare_cookies', fake_prepare)

		def fake_run(cookies, *_a, **kw):
			fake_run.use_proxy = kw.get('use_proxy')
			return True, None, None

		monkeypatch.setattr(checkin_module, 'run_check_in_requests', fake_run)

		provider = _FakeProvider2(auth_method='session', use_proxy=True)
		app = self._app(monkeypatch, provider)
		acc = AccountConfig(cookies={'s': 'abc'}, provider='tp')

		self._run(check_in_account(acc, 0, app, force_direct=True))
		assert fake_run.use_proxy is False
