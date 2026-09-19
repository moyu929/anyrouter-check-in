"""browser_checkin 分支测试（全 mock：浏览器登录与 curl_cffi 会话均离线替换）"""

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
