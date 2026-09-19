"""browser_checkin 分支测试（全 mock：浏览器登录与 curl_cffi 会话均离线替换）"""

import asyncio
from types import SimpleNamespace

import utils.browser_checkin as bc
from utils.browser_checkin import browser_checkin


class FakeResponse:
	status_code = 200

	def __init__(self, payload: dict):
		self._payload = payload

	def json(self):
		return self._payload


class FakeCookieJar:
	def __init__(self):
		self.items: list[tuple] = []

	def set(self, name, value, **kwargs):
		self.items.append((name, value, kwargs))


class FakeSession:
	"""按 URL 后缀路由响应的假 curl_cffi 会话。"""

	def __init__(self, responses: list[tuple[str, dict]]):
		self.responses = responses
		self.headers: dict = {}
		self.cookies = FakeCookieJar()
		self.proxies: dict = {}

	def request(self, method, url, **kwargs):
		for suffix, payload in self.responses:
			if url.endswith(suffix):
				return FakeResponse(payload)
		raise AssertionError(f'未配置的请求: {method} {url}')

	def close(self):
		pass


_COOKIES = [{'name': 'cf_clearance', 'value': 'fake-cf', 'domain': '.superapi.buzz', 'path': '/'}]
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36'


def _self_payload(quota: int = 500_000) -> dict:
	return {'success': True, 'data': {'id': 1, 'username': 'probe', 'quota': quota, 'used_quota': 0}}


def _patch_authenticate(mocker, login_data, self_payload, checkin_payload):
	"""统一替换浏览器登录与 curl_cffi 会话构造。"""
	mocker.patch.object(bc, '_browser_capture_sync', return_value=login_data)

	def _make(cookies, ua, *, use_proxy, bearer_token=None):
		return FakeSession(
			[
				('/api/user/self', self_payload),
				('/api/user/checkin', checkin_payload),
			]
		)

	return mocker.patch.object(bc, '_make_cffi_session', side_effect=_make)


def test_browser_checkin_success(mocker):
	"""完整流程：浏览器登录 → self(入) → checkin 成功 → self(出)。"""
	_patch_authenticate(
		mocker,
		{'cookies': _COOKIES, 'ua': _UA},
		_self_payload(quota=1_000_000),
		{'success': True, 'message': '签到成功'},
	)
	ok, before, after = browser_checkin(
		'cfg-test',
		'a@b.c',
		'pw',
		'https://superapi.buzz',
		use_proxy=False,
	)
	assert ok is True
	assert before is not None and after is not None
	assert before.get('before') is not None or '余额' in str(before)


def test_browser_checkin_login_failed(mocker):
	"""浏览器登录未获得 cookies → 整流程失败。"""
	_patch_authenticate(
		mocker,
		None,
		_self_payload(),
		{'success': True, 'message': '签到成功'},
	)
	ok, before, after = browser_checkin('cfg-test', 'a@b.c', 'pw', 'https://superapi.buzz')
	assert ok is False


def test_browser_checkin_already_checked(mocker):
	"""签到接口幂等返回「今日已签到」→ 视为成功。"""
	_patch_authenticate(
		mocker,
		{'cookies': _COOKIES, 'ua': _UA},
		_self_payload(),
		{'success': False, 'message': '今日已签到'},
	)
	ok, before, after = browser_checkin('cfg-test', 'a@b.c', 'pw', 'https://superapi.buzz')
	assert ok is True


def test_make_cffi_session_injects_cookies_and_ua():
	"""cookies 与 UA 正确注入 curl_cffi 会话（离线，仅构造会话）。"""
	session = bc._make_cffi_session(_COOKIES, _UA, use_proxy=False)
	try:
		assert session.headers.get('User-Agent') == _UA
		assert 'cf_clearance' in list(session.cookies.keys())
	finally:
		session.close()


def test_make_cffi_session_injects_bearer_token():
	"""提供 bearer_token 时注入 Authorization 头。"""
	session = bc._make_cffi_session(_COOKIES, _UA, use_proxy=False, bearer_token='abc' * 12)
	try:
		assert session.headers.get('Authorization') == f'Bearer {"abc" * 12}'
	finally:
		session.close()


def test_browser_capture_missing_cookies_returns_none(mocker):
	"""登录成功但无 cookie → 返回 None（不向后续传递坏会话）。"""

	async def _capture(**kwargs):
		return None

	mocker.patch.object(bc, '_browser_capture_sync', side_effect=lambda **kw: None)
	ok, before, after = browser_checkin('cfg-test', 'a@b.c', 'pw', 'https://superapi.buzz')
	assert ok is False


class _FakeContext:
	"""cookies() 可编程的假 context。"""

	def __init__(self, cookie_names: list[str]):
		self._names = cookie_names

	async def cookies(self):
		return [{'name': n, 'value': 'v'} for n in self._names]


class _FakePage:
	"""够用即可的假 Page：on('request') 捕获 + goto 触发 + evaluate 可编程。"""

	def __init__(self, *, cookie_names: list[str] = (), self_status: int = 0, request_auth: str = ''):
		self.context = _FakeContext(cookie_names)
		self._self_status = self_status
		self._request_auth = request_auth
		self._handlers: list[tuple[str, callable]] = []
		self.goto_urls: list[str] = []

	def on(self, event: str, handler) -> None:
		self._handlers.append((event, handler))

	async def goto(self, url: str, **kwargs) -> None:
		self.goto_urls.append(url)
		if self._request_auth:
			for event, handler in self._handlers:
				if event == 'request':
					handler(
						SimpleNamespace(
							headers={'authorization': self._request_auth}, url='https://superapi.buzz/api/user/models'
						)
					)

	async def evaluate(self, script, *args):
		# 仅被 _is_logged_in 的 self 兜底探测调用
		if 'fetch' in script:
			return {'ok': True, 'status': self._self_status}
		return ''


def test_is_logged_in_detects_new_api_cookie():
	"""新版 new-api fork 的登录态 cookie（new_api_has_session）可被识别。"""
	page = _FakePage(cookie_names=['cf_clearance', 'new_api_has_session'])

	assert asyncio.run(bc._is_logged_in(page)) is True


def test_is_logged_in_falls_back_to_self_api():
	"""无登录态 cookie 时以页内 /api/user/self 探测兜底。"""
	page = _FakePage(cookie_names=['cf_clearance'], self_status=200)

	assert asyncio.run(bc._is_logged_in(page)) is True


def test_capture_bearer_token_from_network():
	"""从网络层捕获前端实际发送的 Bearer token。"""
	page = _FakePage(request_auth='Bearer fake-jwt-abcdefgh')

	token = asyncio.run(bc._capture_bearer_token(page, 'https://superapi.buzz', 'probe'))

	assert token == 'fake-jwt-abcdefgh'
	assert page.goto_urls and page.goto_urls[0].endswith(bc._DASHBOARD_PATH)
