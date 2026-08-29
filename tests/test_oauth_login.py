"""GitHub OAuth 重放登录测试（MockTransport 拦截 GitHub 与站点，全程离线）。"""

import json
import sys
from pathlib import Path

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin as checkin_module
from checkin import login_with_github_oauth
from utils.config import AccountConfig, AppConfig, ProviderConfig

PROVIDER = ProviderConfig(
	name='agentrouter',
	domain='https://demo.example.com',
	sign_in_path=None,
	auth_method='oauth',
	oauth_client_id='client123',
)
FAKE_GH_SESSION = 'fake-github-session'


class _OAuthServer:
	"""可编程的 OAuth 三段式假服务端。"""

	def __init__(
		self,
		*,
		state_status: int = 200,
		state_payload: dict | None = None,
		state_text: str | None = None,
		gh_status: int = 302,
		gh_location: str = 'https://demo.example.com/oauth/github?code=abc123&state=st-1',
		cb_status: int = 200,
		cb_payload: dict | None = None,
		cb_text: str | None = None,
	):
		self.state_status = state_status
		self.state_payload = state_payload if state_payload is not None else {'success': True, 'data': 'st-1'}
		self.state_text = state_text
		self.gh_status = gh_status
		self.gh_location = gh_location
		self.cb_status = cb_status
		self.cb_payload = (
			cb_payload if cb_payload is not None else {'success': True, 'data': {'id': 77, 'checked_in': True}}
		)
		self.cb_text = cb_text
		self.requests: list[httpx.Request] = []

	def __call__(self, request: httpx.Request) -> httpx.Response:
		self.requests.append(request)
		host, path, method = request.url.host, request.url.path, request.method

		if host == 'github.com':
			return httpx.Response(self.gh_status, headers={'Location': self.gh_location})

		if path == '/api/oauth/state':
			# 旧版后端只注册了 GET 路由：POST 探测请求按 405 处理（模拟未升级站点）
			if method == 'POST':
				return httpx.Response(405, text='method not allowed')
			if self.state_text is not None:
				return httpx.Response(self.state_status, text=self.state_text)
			return httpx.Response(
				self.state_status,
				json=self.state_payload,
				headers={'set-cookie': 'acw_tc=waf-token; Path=/'},
			)

		if path == '/api/oauth/github':
			if self.cb_text is not None:
				return httpx.Response(self.cb_status, text=self.cb_text)
			return httpx.Response(
				self.cb_status,
				json=self.cb_payload,
				headers={'set-cookie': 'session=logged-in; Path=/'},
			)

		return httpx.Response(404)


@pytest.fixture
def run_oauth(monkeypatch):
	monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

	async def run(server: _OAuthServer):
		monkeypatch.setattr(
			checkin_module,
			'create_client',
			lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(server)),
		)
		return await login_with_github_oauth('Account 1', PROVIDER, FAKE_GH_SESSION)

	return run


class TestLoginWithGithubOauth:
	async def test_happy_path_returns_cookies_and_api_user(self, run_oauth):
		server = _OAuthServer()

		result = await run_oauth(server)

		assert result is not None
		assert result.api_user == '77'
		assert result.cookies.get('session') == 'logged-in'
		assert result.cookies.get('acw_tc') == 'waf-token'

	async def test_three_step_sequence_and_client_id(self, run_oauth):
		server = _OAuthServer()

		await run_oauth(server)

		urls = [str(r.url) for r in server.requests]
		# 先 POST 探测新版协议（旧版后端返回 405），再回退旧版 GET
		assert urls[0] == 'https://demo.example.com/api/oauth/state'
		assert server.requests[0].method == 'POST'
		assert urls[1] == 'https://demo.example.com/api/oauth/state?mode=login'
		assert 'client_id=client123' in urls[2]
		assert 'state=st-1' in urls[2]
		assert urls[3] == 'https://demo.example.com/api/oauth/github?code=abc123&state=st-1&mode=login'

	async def test_github_session_cookie_is_sent_to_github(self, run_oauth):
		server = _OAuthServer()

		await run_oauth(server)

		gh_request = next(r for r in server.requests if r.url.host == 'github.com')
		assert FAKE_GH_SESSION in gh_request.headers.get('cookie', '')

	async def test_github_response_cookies_are_not_returned_for_provider(self, run_oauth):
		class CookieServer(_OAuthServer):
			def __call__(self, request: httpx.Request) -> httpx.Response:
				response = super().__call__(request)
				if request.url.host == 'github.com':
					response.headers['set-cookie'] = 'github_auth=secret; Domain=github.com; Path=/'
				return response

		result = await run_oauth(CookieServer())

		assert result is not None
		assert result.cookies.get('session') == 'logged-in'
		assert 'github_auth' not in result.cookies

		server = _OAuthServer(state_text='<html>aliyun_waf_aa</html>')

		# WAF 拦截被视为节点问题，抛出 ProxyNodeIssue
		with pytest.raises(checkin_module.ProxyNodeIssue):
			await run_oauth(server)

	async def test_state_http_error(self, run_oauth):
		server = _OAuthServer(state_status=502)

		# 5xx 重试耗尽后被视为节点问题，抛出 ProxyNodeIssue
		with pytest.raises(checkin_module.ProxyNodeIssue):
			await run_oauth(server)

	async def test_state_non_json(self, run_oauth):
		server = _OAuthServer(state_text='<html>not json</html>')

		assert await run_oauth(server) is None

	async def test_state_success_false(self, run_oauth):
		server = _OAuthServer(state_payload={'success': False, 'message': 'nope'})

		assert await run_oauth(server) is None

	async def test_empty_state_value(self, run_oauth):
		server = _OAuthServer(state_payload={'success': True, 'data': ''})

		assert await run_oauth(server) is None

	@pytest.mark.parametrize('status', [401, 403])
	async def test_expired_github_session(self, run_oauth, status):
		server = _OAuthServer(gh_status=status)

		assert await run_oauth(server) is None

	async def test_github_not_redirecting(self, run_oauth):
		server = _OAuthServer(gh_status=200)

		assert await run_oauth(server) is None

	async def test_redirect_without_code(self, run_oauth):
		server = _OAuthServer(gh_location='https://demo.example.com/oauth/github?state=st-1')

		assert await run_oauth(server) is None

	async def test_callback_http_error(self, run_oauth):
		server = _OAuthServer(cb_status=500)

		# 5xx 重试耗尽后被视为节点问题，抛出 ProxyNodeIssue
		with pytest.raises(checkin_module.ProxyNodeIssue):
			await run_oauth(server)

	async def test_callback_waf_block(self, run_oauth):
		server = _OAuthServer(cb_text='aliyun_waf_aa detected')

		# WAF 拦截被视为节点问题，抛出 ProxyNodeIssue
		with pytest.raises(checkin_module.ProxyNodeIssue):
			await run_oauth(server)

	async def test_callback_non_json(self, run_oauth):
		server = _OAuthServer(cb_text='<html>oops</html>')

		assert await run_oauth(server) is None

	async def test_callback_success_false(self, run_oauth):
		server = _OAuthServer(cb_payload={'success': False, 'message': 'denied'})

		assert await run_oauth(server) is None

	async def test_callback_without_user_id_yields_none_api_user(self, run_oauth):
		server = _OAuthServer(cb_payload={'success': True, 'data': {'checked_in': False}})

		result = await run_oauth(server)

		assert result is not None
		assert result.api_user is None


class _NewOAuthServer(_OAuthServer):
	"""新版 new-api 后端（gorouter 2026-08 起）。

	POST /api/oauth/state（body {provider, intent}）→ data={flow_token,...}；
	authorize 带 scope=user:email；回调 GET /api/oauth/github?code&state（无 mode），
	返回 data={access_token, user}。
	"""

	def __init__(self, *, state_data=None, **kwargs):
		super().__init__(
			cb_payload={
				'success': True,
				'data': {
					'access_token': 'jwt-access-token',
					'token_type': 'bearer',
					'user': {'id': 17081},
					'session': {'sid': 's-1', 'current': True},
				},
			},
			**kwargs,
		)
		self.state_data = state_data if state_data is not None else {'flow_token': 'flow-token-1', 'expires_at': 123}

	def __call__(self, request: httpx.Request) -> httpx.Response:
		self.requests.append(request)
		host, path, method = request.url.host, request.url.path, request.method
		if host == 'github.com':
			return httpx.Response(self.gh_status, headers={'Location': self.gh_location})
		if path == '/api/oauth/state' and method == 'POST':
			return httpx.Response(200, json={'success': True, 'data': self.state_data})
		if path == '/api/oauth/github':
			return httpx.Response(
				self.cb_status,
				json=self.cb_payload,
				headers={'set-cookie': 'new_api_refresh=rt; Path=/'},
			)
		return httpx.Response(404)


class TestNewProtocolOauth:
	"""新版 new-api OAuth 协议：POST state + flow_token + scope + Bearer。"""

	@pytest.fixture
	def run_new_oauth(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		async def run(server: _NewOAuthServer):
			monkeypatch.setattr(
				checkin_module,
				'create_client',
				lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(server)),
			)
			return await login_with_github_oauth('Account 1', PROVIDER, FAKE_GH_SESSION)

		return run

	async def test_happy_path_returns_bearer_and_user_id(self, run_new_oauth):
		server = _NewOAuthServer()

		result = await run_new_oauth(server)

		assert result is not None
		assert result.api_user == '17081'
		assert result.bearer_token == 'jwt-access-token'
		assert result.cookies.get('new_api_refresh') == 'rt'

	async def test_sequence_uses_post_state_scope_and_no_mode(self, run_new_oauth):
		server = _NewOAuthServer()

		await run_new_oauth(server)

		requests = server.requests
		assert requests[0].method == 'POST'
		assert str(requests[0].url) == 'https://demo.example.com/api/oauth/state'
		assert json.loads(requests[0].content) == {'provider': 'github', 'intent': 'login'}
		auth_url = str(requests[1].url)
		assert 'client_id=client123' in auth_url
		assert 'state=flow-token-1' in auth_url
		assert 'scope=user%3Aemail' in auth_url
		callback_url = str(requests[2].url)
		assert callback_url == 'https://demo.example.com/api/oauth/github?code=abc123&state=flow-token-1'

	async def test_string_state_data_also_accepted(self, run_new_oauth):
		"""POST 探测成功但 data 为字符串（中间版本）也应接受。"""
		server = _NewOAuthServer(state_data='plain-state')

		result = await run_new_oauth(server)

		assert result is not None
		assert 'state=plain-state' in str(server.requests[1].url)

	async def test_post_waf_html_falls_back_to_get(self, monkeypatch):
		"""POST 探测被 WAF 拦截页命中时回退旧版 GET，不得抛节点异常。"""
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		class WafPostServer(_OAuthServer):
			def __call__(self, request: httpx.Request) -> httpx.Response:
				if request.url.path == '/api/oauth/state' and request.method == 'POST':
					self.requests.append(request)
					return httpx.Response(200, text='<html>aliyun_waf_aa challenge</html>')
				return super().__call__(request)

		server = WafPostServer()
		monkeypatch.setattr(
			checkin_module,
			'create_client',
			lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(server)),
		)

		result = await login_with_github_oauth('Account 1', PROVIDER, FAKE_GH_SESSION)

		assert result is not None
		assert result.bearer_token is None
		# 回退旧版 GET：回调仍带 mode=login
		assert 'mode=login' in str(server.requests[-1].url)

	async def test_post_500_raises_node_issue(self, monkeypatch):
		"""POST 探测遇 5xx 视为节点问题，触发上层节点切换重试。"""
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

		class ServerErrorPostServer(_OAuthServer):
			def __call__(self, request: httpx.Request) -> httpx.Response:
				if request.url.path == '/api/oauth/state' and request.method == 'POST':
					self.requests.append(request)
					return httpx.Response(502)
				return super().__call__(request)

		server = ServerErrorPostServer()
		monkeypatch.setattr(
			checkin_module,
			'create_client',
			lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(server)),
		)

		with pytest.raises(checkin_module.ProxyNodeIssue):
			await login_with_github_oauth('Account 1', PROVIDER, FAKE_GH_SESSION)


class TestOauthThenManualCheckin:
	"""cun 型提供商：GitHub OAuth 登录 + 主动签到接口（首个 OAuth + 手动签到组合）。

	agentrouter/gorouter 是 OAuth + 自动签到；cun 登录成功后还要 POST
	/api/user/checkin。本端到端测试验证内置 cun 配置走通完整六步序列。
	"""

	async def test_oauth_login_then_manual_checkin(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		monkeypatch.delenv('PROVIDERS', raising=False)
		provider = AppConfig.load_from_env().get_provider('cun')
		assert provider is not None and provider.is_oauth() and provider.needs_manual_check_in()

		requests_log: list[httpx.Request] = []

		def router(req: httpx.Request) -> httpx.Response:
			requests_log.append(req)
			host, path = req.url.host, req.url.path
			if host == 'github.com':
				return httpx.Response(
					302, headers={'Location': f'{provider.domain}/oauth/github?code=abc123&state=st-1'}
				)
			if path == '/api/oauth/state':
				return httpx.Response(
					200, json={'success': True, 'data': 'st-1'}, headers={'set-cookie': 'session=pre; Path=/'}
				)
			if path == '/api/oauth/github':
				return httpx.Response(
					200,
					json={'success': True, 'data': {'id': 42, 'checked_in': False}},
					headers={'set-cookie': 'session=logged-in; Path=/'},
				)
			if path == '/api/user/self':
				# 第 1 次（签到前）quota=$1.0，第 2 次（签到后）$1.5
				count = sum(1 for r in requests_log if r.url.path == '/api/user/self')
				quota = 500000 if count <= 1 else 750000
				return httpx.Response(200, json={'success': True, 'data': {'quota': quota, 'used_quota': 0}})
			if path == '/api/user/checkin':
				return httpx.Response(200, json={'success': True})
			return httpx.Response(404, text=f'unhandled {path}')

		monkeypatch.setattr(
			checkin_module, 'create_client', lambda **_kw: httpx.Client(transport=httpx.MockTransport(router))
		)

		account = AccountConfig(cookies=None, github_session='fake-gh-session', provider='cun')
		app_config = AppConfig(providers={'cun': provider})

		ok, before, after = await checkin_module.check_in_account(account, 0, app_config)

		assert ok is True
		assert before is not None and before['quota'] == 1.0
		assert after is not None and after['quota'] == 1.5

		# 完整序列：三段式 OAuth → 签到前余额 → 主动签到 → 签到后余额
		paths = [(r.url.host, r.url.path) for r in requests_log]
		assert paths == [
			('www.cun.ai', '/api/oauth/state'),
			('github.com', '/login/oauth/authorize'),
			('www.cun.ai', '/api/oauth/github'),
			('www.cun.ai', '/api/user/self'),
			('www.cun.ai', '/api/user/checkin'),
			('www.cun.ai', '/api/user/self'),
		]

		# OAuth 回调返回的用户 id 作为 New-Api-User 头随签到请求发送
		checkin_req = next(r for r in requests_log if r.url.path == '/api/user/checkin')
		assert checkin_req.headers.get('new-api-user') == '42'
		# GitHub 会话凭据不外泄到站点请求
		assert 'fake-gh-session' not in str(checkin_req.headers)

	async def test_oauth_failure_aborts_before_checkin(self, monkeypatch):
		"""OAuth 登录失败时不得发起签到请求。"""
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		monkeypatch.delenv('PROVIDERS', raising=False)
		provider = AppConfig.load_from_env().get_provider('cun')

		requests_log: list[httpx.Request] = []

		def router(req: httpx.Request) -> httpx.Response:
			requests_log.append(req)
			host, path = req.url.host, req.url.path
			if host == 'github.com':
				# GitHub 会话过期：401，而非 302
				return httpx.Response(401, json={'message': 'Bad credentials'})
			if path == '/api/oauth/state':
				return httpx.Response(200, json={'success': True, 'data': 'st-1'})
			return httpx.Response(404, text=f'unhandled {path}')

		monkeypatch.setattr(
			checkin_module, 'create_client', lambda **_kw: httpx.Client(transport=httpx.MockTransport(router))
		)

		account = AccountConfig(cookies=None, github_session='expired-gh-session', provider='cun')
		app_config = AppConfig(providers={'cun': provider})

		ok, before, after = await checkin_module.check_in_account(account, 0, app_config)

		assert ok is False
		assert before is None
		assert after is not None and 'OAuth 登录失败' in after['error']
		# 登录失败后不发起任何站点签到/余额请求
		assert all(r.url.host != 'www.cun.ai' or r.url.path == '/api/oauth/state' for r in requests_log)
		assert not any(r.url.path == '/api/user/checkin' for r in requests_log)


class TestNewProtocolAutoCheckin:
	"""gorouter 新版形态端到端：新版 OAuth + 自动签到（sign_in_path=None）+ Bearer 鉴权。

	新版后端 /api/user/self 不再认 session cookie，必须带 Authorization: Bearer；
	签到奖励随登录由服务端自动发放（签到日历接口已真实验证）。
	"""

	async def test_gorouter_new_protocol_flow(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		monkeypatch.delenv('PROVIDERS', raising=False)
		provider = AppConfig.load_from_env().get_provider('gorouter')
		assert provider is not None and provider.needs_manual_check_in() is False

		requests_log: list[httpx.Request] = []

		def router(req: httpx.Request) -> httpx.Response:
			requests_log.append(req)
			host, path, method = req.url.host, req.url.path, req.method
			if host == 'github.com':
				return httpx.Response(
					302, headers={'Location': f'{provider.domain}/oauth/github?code=abc123&state=ft-1'}
				)
			if path == '/api/oauth/state':
				if method == 'POST':
					return httpx.Response(200, json={'success': True, 'data': {'flow_token': 'ft-1'}})
				return httpx.Response(405)
			if path == '/api/oauth/github':
				return httpx.Response(
					200,
					json={
						'success': True,
						'data': {'access_token': 'jwt-tok', 'user': {'id': 17081, 'quota': 14015033}},
					},
					headers={'set-cookie': 'new_api_refresh=rt; Path=/'},
				)
			if path == '/api/user/self':
				auth = req.headers.get('authorization', '')
				if auth != 'Bearer jwt-tok':
					return httpx.Response(401, json={'success': False, 'code': 'AUTH_UNAUTHORIZED'})
				count = sum(1 for r in requests_log if r.url.path == '/api/user/self')
				quota = 14015033 if count <= 1 else 18837341
				return httpx.Response(200, json={'success': True, 'data': {'quota': quota, 'used_quota': 78450000}})
			return httpx.Response(404, text=f'unhandled {path}')

		monkeypatch.setattr(
			checkin_module, 'create_client', lambda **_kw: httpx.Client(transport=httpx.MockTransport(router))
		)

		account = AccountConfig(cookies=None, github_session='fake-gh', provider='gorouter', name='gorouter')
		app_config = AppConfig(providers={'gorouter': provider})

		ok, before, after = await checkin_module.check_in_account(account, 0, app_config)

		assert ok is True
		assert before is not None and before['quota'] == 28.03
		assert after is not None and after['quota'] == 37.67

		# 序列：POST state → github → 回调（无 mode）→ self×2（自动签到型不发 checkin）
		paths = [(r.url.host, r.method, r.url.path) for r in requests_log]
		assert paths == [
			('gorouter.app', 'POST', '/api/oauth/state'),
			('github.com', 'GET', '/login/oauth/authorize'),
			('gorouter.app', 'GET', '/api/oauth/github'),
			('gorouter.app', 'GET', '/api/user/self'),
			('gorouter.app', 'GET', '/api/user/self'),
		]
		# authorize 带 scope（与站点前端一致）
		assert 'scope=user%3Aemail' in str(requests_log[1].url)
