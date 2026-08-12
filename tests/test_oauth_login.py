"""GitHub OAuth 重放登录测试（MockTransport 拦截 GitHub 与站点，全程离线）。"""

import sys
from pathlib import Path

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin as checkin_module
from checkin import login_with_github_oauth
from utils.config import ProviderConfig

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

	async def test_waf_block_on_state_is_detected(self, run_oauth):
		server = _OAuthServer(state_text='<html>aliyun_waf_aa</html>')

		assert await run_oauth(server) is None

	async def test_state_http_error(self, run_oauth):
		server = _OAuthServer(state_status=502)

		assert await run_oauth(server) is None

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

		assert await run_oauth(server) is None

	async def test_callback_waf_block(self, run_oauth):
		server = _OAuthServer(cb_text='aliyun_waf_aa detected')

		assert await run_oauth(server) is None

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
