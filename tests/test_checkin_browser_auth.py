"""checkin.py 浏览器认证与 cookie 编排测试（全离线）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import checkin
from utils.browser import BrowserLoginResult
from utils.config import ProviderConfig


class _Browser:
	def __init__(self, page=None):
		self.page = page or SimpleNamespace()
		self.new_page = AsyncMock(return_value=self.page)
		self.close = AsyncMock()


class _Context:
	def __init__(self, page=None, cookies=None):
		self.page = page or SimpleNamespace(url='https://demo.example/login')
		self.new_page = AsyncMock(return_value=self.page)
		self.cookies = AsyncMock(return_value=cookies or [])
		self.close = AsyncMock()


def _provider(**overrides) -> ProviderConfig:
	data = {
		'name': 'demo',
		'domain': 'https://demo.example',
		'login_path': '/login',
		'bypass_method': 'waf_cookies',
		'waf_cookie_names': ['acw_tc'],
		'persist_profile': False,
	}
	data.update(overrides)
	return ProviderConfig(**data)


class TestWafBrowserCookies:
	async def test_collects_required_cookies_and_closes_browser(self, monkeypatch):
		page = SimpleNamespace(
			goto=AsyncMock(),
			context=SimpleNamespace(
				cookies=AsyncMock(
					return_value=[
						{'name': 'acw_tc', 'value': 'fresh'},
						{'name': 'session', 'value': 'ignored'},
					]
				)
			),
		)
		browser = _Browser(page)
		launch = AsyncMock(return_value=browser)
		monkeypatch.setattr(checkin, 'launch_async', launch)
		monkeypatch.setattr(checkin, 'get_playwright_proxy', lambda **_kwargs: {'server': 'http://proxy:7890'})
		monkeypatch.setattr(checkin, 'prepare_browser_page', AsyncMock())
		monkeypatch.setattr(checkin, 'wait_for_waf_ready', AsyncMock())

		result = await checkin.get_waf_cookies_with_browser(
			'Account 1',
			'https://demo.example/login',
			['acw_tc'],
			use_proxy=True,
		)

		assert result == {'acw_tc': 'fresh'}
		launch.assert_awaited_once_with(headless=True, proxy={'server': 'http://proxy:7890'})
		page.goto.assert_awaited_once_with('https://demo.example/login', wait_until='domcontentloaded')
		browser.close.assert_awaited_once()

	async def test_missing_cookie_returns_none_and_closes(self, monkeypatch):
		page = SimpleNamespace(
			goto=AsyncMock(),
			context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
		)
		browser = _Browser(page)
		monkeypatch.setattr(checkin, 'launch_async', AsyncMock(return_value=browser))
		monkeypatch.setattr(checkin, 'get_playwright_proxy', lambda **_kwargs: None)
		monkeypatch.setattr(checkin, 'prepare_browser_page', AsyncMock())
		monkeypatch.setattr(checkin, 'wait_for_waf_ready', AsyncMock())

		assert await checkin.get_waf_cookies_with_browser('A', 'https://demo/login', ['acw_tc']) is None
		browser.close.assert_awaited_once()

	async def test_launch_failure_returns_none_without_close_error(self, monkeypatch):
		monkeypatch.setattr(checkin, 'launch_async', AsyncMock(side_effect=RuntimeError('launch failed')))
		monkeypatch.setattr(checkin, 'get_playwright_proxy', lambda **_kwargs: None)

		assert await checkin.get_waf_cookies_with_browser('A', 'https://demo/login', ['acw_tc']) is None


class TestPrepareCookies:
	async def test_fresh_waf_cookie_overrides_stale_user_value(self, monkeypatch):
		provider = _provider(use_proxy=True)
		get_waf = AsyncMock(return_value={'acw_tc': 'fresh'})
		monkeypatch.setattr(checkin, 'get_waf_cookies_with_browser', get_waf)

		result = await checkin.prepare_cookies('Account 1', provider, {'session': 's', 'acw_tc': 'stale'})

		assert result == {'session': 's', 'acw_tc': 'fresh'}
		get_waf.assert_awaited_once_with(
			'Account 1',
			'https://demo.example/login',
			['acw_tc'],
			use_proxy=True,
		)

	async def test_no_waf_provider_returns_user_cookies_without_browser(self, monkeypatch):
		provider = _provider(bypass_method=None, waf_cookie_names=None)
		get_waf = AsyncMock()
		monkeypatch.setattr(checkin, 'get_waf_cookies_with_browser', get_waf)

		assert await checkin.prepare_cookies('A', provider, {'session': 's'}) == {'session': 's'}
		get_waf.assert_not_awaited()

	async def test_waf_failure_returns_none(self, monkeypatch):
		monkeypatch.setattr(checkin, 'get_waf_cookies_with_browser', AsyncMock(return_value=None))

		assert await checkin.prepare_cookies('A', _provider(), {'session': 's'}) is None


class TestEmailCredentialLogin:
	@pytest.fixture
	def setup(self, monkeypatch, tmp_path):
		settings = SimpleNamespace(
			wait_timeout_ms=1000,
			profile_dir=tmp_path,
			persist_profile=False,
			headless=True,
			humanize=False,
		)
		monkeypatch.setattr(checkin, 'load_browser_login_settings', lambda *_args, **_kwargs: settings)
		for name in ('prepare_browser_page', 'navigate_login_page', 'login_with_email_form', 'save_login_screenshot'):
			monkeypatch.setattr(checkin, name, AsyncMock())
		return settings

	async def test_logged_profile_skips_form_and_returns_filtered_cookies(self, monkeypatch, setup):
		page = SimpleNamespace(url='https://demo.example/console')
		context = _Context(
			page,
			cookies=[
				{'name': 'session', 'value': 'valid'},
				{'name': '', 'value': 'ignored'},
				{'name': 'empty', 'value': ''},
			],
		)
		monkeypatch.setattr(checkin, 'launch_login_context', AsyncMock(return_value=context))
		monkeypatch.setattr(checkin, 'is_logged_in', AsyncMock(return_value=True))
		monkeypatch.setattr(checkin, 'verify_browser_login', AsyncMock(return_value={'id': 7}))

		result = await checkin.login_with_credentials('A', _provider(), 'demo', 'user@example.invalid', 'password')

		assert result == BrowserLoginResult(cookies={'session': 'valid'}, api_user='7')
		checkin.login_with_email_form.assert_not_awaited()
		context.close.assert_awaited_once()

	async def test_not_logged_in_runs_email_form(self, monkeypatch, setup):
		page = SimpleNamespace(url='https://demo.example/login')
		context = _Context(page, cookies=[{'name': 'session', 'value': 'valid'}])
		monkeypatch.setattr(checkin, 'launch_login_context', AsyncMock(return_value=context))
		monkeypatch.setattr(checkin, 'is_logged_in', AsyncMock(return_value=False))
		monkeypatch.setattr(checkin, 'has_session_cookie', AsyncMock(return_value=True))
		monkeypatch.setattr(checkin, 'verify_browser_login', AsyncMock(return_value={'id': 8}))

		result = await checkin.login_with_credentials('A', _provider(), 'demo', 'user@example.invalid', 'password')

		assert result is not None and result.api_user == '8'
		checkin.login_with_email_form.assert_awaited_once_with(
			page,
			'user@example.invalid',
			'password',
			1000,
			provider='demo',
			account_name='A',
		)
		context.close.assert_awaited_once()

	async def test_verification_failure_takes_screenshot_and_closes(self, monkeypatch, setup):
		page = SimpleNamespace(url='https://demo.example/console')
		context = _Context(page, cookies=[{'name': 'session', 'value': 'value'}])
		monkeypatch.setattr(checkin, 'launch_login_context', AsyncMock(return_value=context))
		monkeypatch.setattr(checkin, 'is_logged_in', AsyncMock(return_value=True))
		monkeypatch.setattr(checkin, 'verify_browser_login', AsyncMock(return_value=None))

		assert (
			await checkin.login_with_credentials('A', _provider(), 'demo', 'user@example.invalid', 'password') is None
		)
		checkin.save_login_screenshot.assert_awaited_once_with(page, 'demo', 'A', 'not-authenticated')
		context.close.assert_awaited_once()

	async def test_navigation_error_takes_error_screenshot_and_closes(self, monkeypatch, setup):
		page = SimpleNamespace(url='https://demo.example/login')
		context = _Context(page)
		monkeypatch.setattr(checkin, 'launch_login_context', AsyncMock(return_value=context))
		checkin.navigate_login_page.side_effect = RuntimeError('navigation failed')

		assert (
			await checkin.login_with_credentials('A', _provider(), 'demo', 'user@example.invalid', 'password') is None
		)
		checkin.save_login_screenshot.assert_awaited_once_with(page, 'demo', 'A', 'login-error')
		context.close.assert_awaited_once()

	async def test_context_launch_failure_returns_none(self, monkeypatch, setup):
		monkeypatch.setattr(checkin, 'launch_login_context', AsyncMock(side_effect=RuntimeError('launch failed')))

		assert (
			await checkin.login_with_credentials('A', _provider(), 'demo', 'user@example.invalid', 'password') is None
		)


class TestRunMain:
	def test_keyboard_interrupt_exits_one(self, monkeypatch):
		monkeypatch.setattr(checkin, 'main', lambda: None)
		monkeypatch.setattr(checkin.asyncio, 'run', MagicMock(side_effect=KeyboardInterrupt))

		with pytest.raises(SystemExit) as exc:
			checkin.run_main()

		assert exc.value.code == 1

	def test_unexpected_error_exits_one(self, monkeypatch):
		monkeypatch.setattr(checkin, 'main', lambda: None)
		monkeypatch.setattr(checkin.asyncio, 'run', MagicMock(side_effect=RuntimeError('boom')))

		with pytest.raises(SystemExit) as exc:
			checkin.run_main()

		assert exc.value.code == 1
