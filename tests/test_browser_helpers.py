"""utils/browser.py 纯函数与解析逻辑测试（不启动真实浏览器，不访问任何站点）。"""

from pathlib import Path
from typing import Any

import pytest

from utils.browser import (
	_env_bool,
	_extract_user_profile,
	_parse_user_self_response,
	_sanitize_screenshot_part,
	get_screenshot_dir,
	has_session_cookie,
	is_logged_in,
	load_browser_login_settings,
	save_login_screenshot,
	take_pending_screenshots,
	wait_for_logged_in,
)


class _FakeContext:
	def __init__(self, cookies: list[dict]):
		self._cookies = cookies

	async def cookies(self):
		return self._cookies


class _FakeLocator:
	def __init__(self, visible: bool):
		self._visible = visible

	@property
	def first(self):
		return self

	async def is_visible(self):
		return self._visible


class _FakePage:
	def __init__(self, url: str, *, cookies: list[dict] | None = None, email_form_visible: bool = False):
		self.url = url
		self.context = _FakeContext(cookies or [])
		self._email_form_visible = email_form_visible

	def locator(self, _selector: str):
		return _FakeLocator(self._email_form_visible)


class _FakeResponse:
	def __init__(self, url: str, status: int, payload, *, raises: bool = False):
		self.url = url
		self.status = status
		self._payload = payload
		self._raises = raises

	async def json(self):
		if self._raises:
			raise ValueError('not json')
		return self._payload


def _page(url: str, *, cookies: list[dict] | None = None, email_form_visible: bool = False) -> Any:
	"""构造鸭子类型的假 Page（被测函数只用到 url / context.cookies / locator）。"""
	return _FakePage(url, cookies=cookies, email_form_visible=email_form_visible)


class TestEnvBool:
	@pytest.mark.parametrize('raw', ['1', 'true', 'TRUE', 'yes', 'On', ' true '])
	def test_truthy_values(self, monkeypatch, raw):
		monkeypatch.setenv('X_FLAG', raw)

		assert _env_bool('X_FLAG', False) is True

	@pytest.mark.parametrize('raw', ['0', 'false', 'no', 'off', '', 'maybe'])
	def test_falsy_values(self, monkeypatch, raw):
		monkeypatch.setenv('X_FLAG', raw)

		assert _env_bool('X_FLAG', True) is False

	def test_unset_uses_default(self, monkeypatch):
		monkeypatch.delenv('X_FLAG', raising=False)

		assert _env_bool('X_FLAG', True) is True
		assert _env_bool('X_FLAG', False) is False


class TestLoadBrowserLoginSettings:
	def test_defaults(self, monkeypatch):
		for name in (
			'CHECKIN_BROWSER_PROFILE_DIR',
			'CHECKIN_HUMANIZE',
			'CHECKIN_HUMANIZE_AGENTROUTER',
			'CHECKIN_HEADLESS',
			'CHECKIN_WAIT_TIMEOUT_MS',
			'CLOAKBROWSER_BINARY_PATH',
		):
			monkeypatch.delenv(name, raising=False)

		settings = load_browser_login_settings('Account 1', 'anyrouter')

		assert settings.headless is True
		assert settings.humanize is True
		assert settings.persist_profile is True
		assert settings.profile_dir == Path('.browser_profiles') / 'anyrouter' / 'Account 1'
		assert settings.cloakbrowser_binary_path is None

	def test_agentrouter_humanize_override(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_HUMANIZE', 'true')
		monkeypatch.setenv('CHECKIN_HUMANIZE_AGENTROUTER', 'false')

		assert load_browser_login_settings('A', 'agentrouter').humanize is False
		# 其他提供商不受 agentrouter 专属开关影响
		assert load_browser_login_settings('A', 'anyrouter').humanize is True

	def test_timeout_and_binary_path_from_env(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_WAIT_TIMEOUT_MS', '45000')
		monkeypatch.setenv('CLOAKBROWSER_BINARY_PATH', '  /opt/chrome  ')

		settings = load_browser_login_settings('A', 'anyrouter')

		assert settings.wait_timeout_ms == 45000
		assert settings.cloakbrowser_binary_path == '/opt/chrome'

	def test_blank_binary_path_becomes_none(self, monkeypatch):
		monkeypatch.setenv('CLOAKBROWSER_BINARY_PATH', '   ')

		assert load_browser_login_settings('A', 'anyrouter').cloakbrowser_binary_path is None


class TestScreenshotHelpers:
	def test_screenshot_dir_from_env(self, monkeypatch, tmp_path):
		monkeypatch.setenv('CHECKIN_SCREENSHOT_DIR', str(tmp_path / 'shots'))

		assert get_screenshot_dir() == tmp_path / 'shots'

	def test_screenshot_dir_default(self, monkeypatch):
		monkeypatch.delenv('CHECKIN_SCREENSHOT_DIR', raising=False)

		assert get_screenshot_dir() == Path('checkin_screenshots')

	@pytest.mark.parametrize(
		('raw', 'expected'),
		[
			('账号 1', '账号_1'),
			('a/b\\c', 'a_b_c'),
			('  spaced  ', 'spaced'),
			('!!!', '_'),
			('', 'unknown'),
			('   ', 'unknown'),
			('keep.dash-ok', 'keep.dash-ok'),
		],
	)
	def test_sanitize_screenshot_part(self, raw, expected):
		assert _sanitize_screenshot_part(raw) == expected

	async def test_screenshot_is_skipped_outside_debug_mode(self, monkeypatch):
		monkeypatch.delenv('DEBUG_MODE', raising=False)
		take_pending_screenshots()

		result = await save_login_screenshot(_page('https://x.example.com'), 'anyrouter', 'A', 'label')

		assert result is None
		assert take_pending_screenshots() == []

	def test_take_pending_screenshots_clears_buffer(self):
		take_pending_screenshots()

		assert take_pending_screenshots() == []


class TestExtractUserProfile:
	def test_success_wrapper_with_data(self):
		assert _extract_user_profile({'success': True, 'data': {'id': 7}}) == {'id': 7}

	def test_bare_profile_object(self):
		assert _extract_user_profile({'id': 9, 'username': 'x'}) == {'id': 9, 'username': 'x'}

	def test_success_false_without_top_level_id(self):
		assert _extract_user_profile({'success': False, 'data': {'id': 7}}) is None

	def test_data_without_id_is_rejected(self):
		assert _extract_user_profile({'success': True, 'data': {'username': 'x'}}) is None

	def test_non_dict_payload(self):
		assert _extract_user_profile(['id']) is None
		assert _extract_user_profile(None) is None


class TestParseUserSelfResponse:
	async def test_matching_url_and_status(self):
		response = _FakeResponse('https://x.example.com/api/user/self', 200, {'success': True, 'data': {'id': 3}})

		assert await _parse_user_self_response(response) == {'id': 3}

	async def test_other_url_is_ignored(self):
		response = _FakeResponse('https://x.example.com/api/other', 200, {'success': True, 'data': {'id': 3}})

		assert await _parse_user_self_response(response) is None

	async def test_non_200_is_ignored(self):
		response = _FakeResponse('https://x.example.com/api/user/self', 401, {'success': True, 'data': {'id': 3}})

		assert await _parse_user_self_response(response) is None

	async def test_non_json_body_is_ignored(self):
		response = _FakeResponse('https://x.example.com/api/user/self', 200, None, raises=True)

		assert await _parse_user_self_response(response) is None


class TestSessionAndLoginDetection:
	async def test_session_cookie_present(self):
		page = _page('https://x.example.com', cookies=[{'name': 'session', 'value': 'abc'}])

		assert await has_session_cookie(page) is True

	async def test_empty_session_cookie_value_does_not_count(self):
		page = _page('https://x.example.com', cookies=[{'name': 'session', 'value': ''}])

		assert await has_session_cookie(page) is False

	async def test_other_cookies_do_not_count(self):
		page = _page('https://x.example.com', cookies=[{'name': 'acw_tc', 'value': 'abc'}])

		assert await has_session_cookie(page) is False

	async def test_console_url_means_logged_in(self):
		assert await is_logged_in(_page('https://x.example.com/console')) is True

	@pytest.mark.parametrize(
		'url',
		[
			'https://x.example.com/login',
			'https://x.example.com/signin',
			'https://x.example.com/sign-in',
		],
	)
	async def test_login_urls_mean_not_logged_in(self, url):
		assert await is_logged_in(_page(url)) is False

	async def test_email_form_visible_means_not_logged_in(self):
		page = _page('https://x.example.com/', email_form_visible=True)

		assert await is_logged_in(page) is False

	async def test_wait_for_logged_in_returns_true_immediately(self):
		assert await wait_for_logged_in(_page('https://x.example.com/console'), timeout_ms=100) is True

	async def test_wait_for_logged_in_times_out(self):
		assert await wait_for_logged_in(_page('https://x.example.com/login'), timeout_ms=100) is False
