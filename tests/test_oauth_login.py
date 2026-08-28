"""GitHub OAuth 重放登录测试（MockTransport 拦截 GitHub 与站点，全程离线）。"""

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
		host, path = request.url.host, request.url.path

		if host == 'github.com':
			return httpx.Response(self.gh_status, headers={'Location': self.gh_location})

		if path == '/api/oauth/state':
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
		assert urls[0] == 'https://demo.example.com/api/oauth/state?mode=login'
		assert 'client_id=client123' in urls[1]
		assert 'state=st-1' in urls[1]
		assert urls[2] == 'https://demo.example.com/api/oauth/github?code=abc123&state=st-1&mode=login'

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
