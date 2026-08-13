"""浏览器编排状态机测试：仅使用 fake Page/Locator，不启动真实浏览器。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from utils import browser as browser_module
from utils.browser import (
	BrowserLoginSettings,
	_click_email_login_entry,
	_click_locator,
	_dismiss_blocking_overlays,
	_ensure_binary_path,
	_EphemeralBrowserContext,
	_first_visible_locator,
	_open_email_login_form,
	_set_input_value,
	_settle_page,
	_wait_for_login_page_ready,
	_wait_for_login_shell,
	_wait_for_optional_load_state,
	_wait_for_username_input,
	fill_email_credentials,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	save_login_screenshot,
	submit_login_form,
	take_pending_screenshots,
	verify_browser_login,
	wait_for_site_ready,
	wait_for_waf_ready,
)


class _Locator:
	def __init__(self, *, visible: bool = True, count: int = 0):
		self.visible = visible
		self.count_value = count
		self.first = self
		self.click = AsyncMock()
		self.fill = AsyncMock()
		self.input_value = AsyncMock(return_value='')
		self.evaluate = AsyncMock()
		self.wait_for = AsyncMock()
		self.scroll_into_view_if_needed = AsyncMock()

	async def is_visible(self):
		return self.visible

	async def count(self):
		return self.count_value

	def nth(self, _index: int):
		return self

	def get_by_role(self, *_args, **_kwargs):
		return self


class _Page:
	def __init__(self):
		self.url = 'https://demo.example/login'
		self.default_locator = _Locator()
		self.goto = AsyncMock()
		self.reload = AsyncMock()
		self.evaluate = AsyncMock(return_value=True)
		self.wait_for_load_state = AsyncMock()
		self.wait_for_function = AsyncMock()
		self.screenshot = AsyncMock()
		self.add_init_script = AsyncMock()

	def locator(self, _selector: str):
		return self.default_locator

	def get_by_role(self, *_args, **_kwargs):
		return self.default_locator


@pytest.fixture(autouse=True)
def _clear_screenshot_queue():
	take_pending_screenshots()
	yield
	take_pending_screenshots()


class TestBrowserResourceHelpers:
	def test_ensure_binary_path_sets_environment(self, monkeypatch, tmp_path):
		binary = str(tmp_path / 'browser')
		settings = BrowserLoginSettings(True, False, 1000, tmp_path, binary, False)

		_ensure_binary_path(settings)

		assert browser_module.os.environ['CLOAKBROWSER_BINARY_PATH'] == binary

	async def test_ephemeral_context_closes_browser_when_context_close_fails(self):
		context = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('context close failed')))
		browser = SimpleNamespace(close=AsyncMock())
		wrapper = _EphemeralBrowserContext(context, browser)

		with pytest.raises(RuntimeError, match='context close failed'):
			await wrapper.close(reason='test')

		context.close.assert_awaited_once_with(reason='test')
		browser.close.assert_awaited_once()

	async def test_prepare_browser_page_installs_popup_guard(self, monkeypatch):
		page = object()
		guard = AsyncMock()
		monkeypatch.setattr(browser_module, 'setup_popup_guard', guard)

		await prepare_browser_page(page)

		guard.assert_awaited_once_with(page)


class TestScreenshots:
	async def test_debug_screenshot_is_saved_and_queued(self, monkeypatch, tmp_path):
		page = _Page()
		monkeypatch.setattr(browser_module, 'is_debug_enabled', lambda: True)
		monkeypatch.setattr(browser_module, 'get_screenshot_dir', lambda: tmp_path)
		monkeypatch.setattr(
			browser_module,
			'datetime',
			SimpleNamespace(now=lambda: datetime(2026, 1, 2, 3, 4, 5)),
		)

		path = await save_login_screenshot(page, 'provider', 'Account 1', 'login error')

		expected = tmp_path / 'provider_Account_1_20260102_030405_login_error.png'
		assert path == expected
		page.screenshot.assert_awaited_once_with(path=str(expected), full_page=True, timeout=15_000)
		assert take_pending_screenshots() == [expected]
		assert take_pending_screenshots() == []

	async def test_screenshot_failure_is_not_queued(self, monkeypatch, tmp_path):
		page = _Page()
		page.screenshot.side_effect = RuntimeError('capture failed')
		monkeypatch.setattr(browser_module, 'is_debug_enabled', lambda: True)
		monkeypatch.setattr(browser_module, 'get_screenshot_dir', lambda: tmp_path)

		assert await save_login_screenshot(page, 'provider', 'account', 'error') is None
		assert take_pending_screenshots() == []


class TestPageReadiness:
	async def test_wait_for_site_ready_caps_timeout_and_dismisses_popups(self, monkeypatch):
		page = _Page()
		dismiss = AsyncMock(return_value=2)
		monkeypatch.setattr(browser_module, 'dismiss_popups', dismiss)

		await wait_for_site_ready(page, timeout_ms=90_000)

		page.wait_for_load_state.assert_awaited_once_with('domcontentloaded', timeout=30_000)
		page.wait_for_function.assert_awaited_once_with(browser_module._SITE_READY_JS, timeout=30_000)
		dismiss.assert_awaited_once_with(page)

	async def test_wait_for_site_ready_recovers_from_optional_js_timeout(self, monkeypatch):
		page = _Page()
		page.wait_for_function.side_effect = RuntimeError('not ready')
		sleep = AsyncMock()
		monkeypatch.setattr(browser_module.asyncio, 'sleep', sleep)
		monkeypatch.setattr(browser_module, 'dismiss_popups', AsyncMock(return_value=0))

		await wait_for_site_ready(page, timeout_ms=1000)

		sleep.assert_awaited_once_with(3)

	async def test_optional_load_state_success_and_failure(self, monkeypatch):
		page = _Page()
		assert await _wait_for_optional_load_state(page, 'networkidle', 100) is True

		page.wait_for_load_state.side_effect = RuntimeError('timeout')
		debug = MagicMock()
		monkeypatch.setattr(browser_module, 'debug_print', debug)
		assert await _wait_for_optional_load_state(page, 'networkidle', 100) is False
		assert 'networkidle' in debug.call_args.args[0]

	async def test_settle_page_sleeps_then_waits_networkidle(self, monkeypatch):
		page = _Page()
		sleep = AsyncMock()
		wait = AsyncMock(return_value=False)
		monkeypatch.setattr(browser_module.asyncio, 'sleep', sleep)
		monkeypatch.setattr(browser_module, '_wait_for_optional_load_state', wait)

		await _settle_page(page, 1.5, 700)

		sleep.assert_awaited_once_with(1.5)
		wait.assert_awaited_once_with(page, 'networkidle', 700)

	async def test_wait_for_login_shell_caps_timeout_and_handles_failure(self):
		page = _Page()
		assert await _wait_for_login_shell(page, 90_000) is True
		page.wait_for_function.assert_awaited_once_with(browser_module._LOGIN_SHELL_READY_JS, timeout=60_000)

		page.wait_for_function.side_effect = RuntimeError('not rendered')
		assert await _wait_for_login_shell(page, 1000) is False

	async def test_wait_for_waf_ready_delegates(self, monkeypatch):
		page = _Page()
		wait = AsyncMock()
		monkeypatch.setattr(browser_module, 'wait_for_site_ready', wait)

		await wait_for_waf_ready(page, 1234)

		wait.assert_awaited_once_with(page, 1234)


class TestNavigation:
	async def test_navigate_login_page_succeeds_on_first_attempt(self, monkeypatch):
		page = _Page()
		monkeypatch.setattr(browser_module, '_settle_page', AsyncMock())
		monkeypatch.setattr(browser_module, 'dismiss_popups', AsyncMock(return_value=0))
		monkeypatch.setattr(browser_module, '_wait_for_login_shell', AsyncMock(return_value=True))
		monkeypatch.setattr(browser_module, 'wait_for_site_ready', AsyncMock())

		await navigate_login_page(page, 'https://demo.example/login', 10_000)

		assert page.goto.await_args_list == [
			call('https://demo.example/', wait_until='load', timeout=10_000),
			call('https://demo.example/login', wait_until='load', timeout=10_000),
		]
		page.reload.assert_not_awaited()

	async def test_navigate_login_page_retries_and_times_out(self, monkeypatch):
		page = _Page()
		monkeypatch.setattr(browser_module, '_settle_page', AsyncMock())
		monkeypatch.setattr(browser_module, 'dismiss_popups', AsyncMock(return_value=0))
		monkeypatch.setattr(browser_module, '_wait_for_login_shell', AsyncMock(return_value=False))
		monkeypatch.setattr(browser_module, '_log_login_page_state', AsyncMock())
		screenshot = AsyncMock()
		monkeypatch.setattr(browser_module, 'save_login_screenshot', screenshot)
		monkeypatch.setattr(browser_module.asyncio, 'sleep', AsyncMock())

		with pytest.raises(TimeoutError, match='never rendered'):
			await navigate_login_page(
				page,
				'https://demo.example/login',
				1000,
				provider='demo',
				account_name='Account 1',
			)

		assert page.goto.await_count == 4
		assert page.reload.await_count == 2
		assert screenshot.await_count == 3

	async def test_warmup_failure_does_not_abort_login(self, monkeypatch):
		page = _Page()
		page.goto.side_effect = [RuntimeError('warmup failed'), None]
		monkeypatch.setattr(browser_module, '_settle_page', AsyncMock())
		monkeypatch.setattr(browser_module, 'dismiss_popups', AsyncMock(return_value=0))
		monkeypatch.setattr(browser_module, '_wait_for_login_shell', AsyncMock(return_value=True))
		monkeypatch.setattr(browser_module, 'wait_for_site_ready', AsyncMock())

		await navigate_login_page(page, 'https://demo.example/login', 1000)

		assert page.goto.await_count == 2


class _Response:
	url = 'https://demo.example/api/user/self'
	status = 200

	async def json(self):
		return {'success': True, 'data': {'id': 7, 'username': 'tester'}}


class _EventPage(_Page):
	def __init__(self, response=None):
		super().__init__()
		self.response = response
		self.listener = None
		self.removed = None
		self.url = 'https://demo.example/console'

	def on(self, _event: str, callback):
		self.listener = callback

	def remove_listener(self, _event: str, callback):
		self.removed = callback

	async def _goto(self, *_args, **_kwargs):
		if self.response is not None:
			await self.listener(self.response)


class TestLoginVerification:
	async def test_verify_browser_login_captures_profile_and_removes_listener(self):
		page = _EventPage(_Response())
		page.goto = AsyncMock(side_effect=page._goto)

		profile = await verify_browser_login(page, 'https://demo.example/console', 1000)

		assert profile == {'id': 7, 'username': 'tester'}
		assert page.removed is page.listener

	async def test_verify_browser_login_timeout_returns_none(self, monkeypatch):
		page = _EventPage()

		async def fake_wait_for(awaitable, *, timeout):
			awaitable.close()
			raise TimeoutError

		monkeypatch.setattr(browser_module.asyncio, 'wait_for', fake_wait_for)

		assert await verify_browser_login(page, 'https://demo.example/console', 1000) is None
		assert page.removed is page.listener

	async def test_verify_browser_login_removes_listener_when_navigation_fails(self):
		page = _EventPage()
		page.goto.side_effect = RuntimeError('navigation failed')

		with pytest.raises(RuntimeError, match='navigation failed'):
			await verify_browser_login(page, 'https://demo.example/console', 1000)

		assert page.removed is page.listener


class TestLocatorHelpers:
	async def test_first_visible_locator_skips_hidden_and_errors(self):
		locators = {
			'a': _Locator(visible=False),
			'b': _Locator(visible=True),
		}
		page = _Page()
		page.locator = lambda selector: locators[selector]

		assert await _first_visible_locator(page, ('a', 'b')) is locators['b']
		locators['a'].is_visible = AsyncMock(side_effect=RuntimeError('detached'))
		assert await _first_visible_locator(page, ('a', 'b')) is locators['b']

	async def test_dismiss_blocking_overlays_stops_when_no_progress(self, monkeypatch):
		page = _Page()
		monkeypatch.setattr(browser_module, '_is_email_form_visible', AsyncMock(return_value=False))
		dismiss = AsyncMock(side_effect=[1, 0])
		sleep = AsyncMock()
		monkeypatch.setattr(browser_module, 'dismiss_popups', dismiss)
		monkeypatch.setattr(browser_module.asyncio, 'sleep', sleep)

		await _dismiss_blocking_overlays(page)

		assert dismiss.await_count == 2
		sleep.assert_awaited_once_with(0.3)

	async def test_click_locator_uses_force_fallback(self):
		locator = _Locator()
		locator.click.side_effect = [RuntimeError('covered'), None]

		assert await _click_locator(locator) is True
		assert locator.click.await_args_list[-1] == call(force=True, timeout=15_000)

	async def test_click_locator_returns_false_when_both_clicks_fail(self):
		locator = _Locator()
		locator.click.side_effect = RuntimeError('blocked')

		assert await _click_locator(locator) is False

	async def test_wait_for_login_page_ready_uses_role_fallback(self, monkeypatch):
		page = _Page()
		css = _Locator()
		css.wait_for.side_effect = RuntimeError('missing')
		role = _Locator()
		page.locator = lambda _selector: css
		page.get_by_role = lambda *_args, **_kwargs: role
		monkeypatch.setattr(browser_module, '_is_email_form_visible', AsyncMock(return_value=False))

		await _wait_for_login_page_ready(page, 500)

		role.wait_for.assert_awaited_once_with(state='visible', timeout=500)

	async def test_click_email_login_entry_uses_visible_css_button(self):
		page = _Page()
		button = _Locator(visible=True, count=1)
		page.locator = lambda _selector: button

		assert await _click_email_login_entry(page) is True
		button.click.assert_awaited_once_with(timeout=15_000)

	async def test_wait_for_username_input_falls_through_selectors(self, monkeypatch):
		page = _Page()
		first = _Locator()
		first.wait_for.side_effect = RuntimeError('missing')
		second = _Locator()
		locators = iter([first, second])
		page.locator = lambda _selector: next(locators)

		assert await _wait_for_username_input(page, 500) is True
		second.wait_for.assert_awaited_once_with(state='visible', timeout=500)


class TestEmailFormFlow:
	async def test_open_email_form_returns_after_entry_click(self, monkeypatch):
		page = _Page()
		clock = iter([0.0, 0.1, 0.2])
		monkeypatch.setattr(browser_module, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
		monkeypatch.setattr(browser_module, '_dismiss_blocking_overlays', AsyncMock())
		monkeypatch.setattr(browser_module, '_is_email_form_visible', AsyncMock(side_effect=[False, False]))
		monkeypatch.setattr(browser_module, '_wait_for_login_page_ready', AsyncMock())
		monkeypatch.setattr(browser_module, '_click_email_login_entry', AsyncMock(return_value=True))
		monkeypatch.setattr(browser_module, '_wait_for_username_input', AsyncMock(return_value=True))
		monkeypatch.setattr(browser_module.asyncio, 'sleep', AsyncMock())

		await _open_email_login_form(page, 1000)

		browser_module._click_email_login_entry.assert_awaited_once_with(page)

	async def test_open_email_form_timeout_records_diagnostics(self, monkeypatch):
		page = _Page()
		clock = iter([0.0, 2.0, 2.0])
		monkeypatch.setattr(browser_module, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
		monkeypatch.setattr(browser_module, '_dismiss_blocking_overlays', AsyncMock())
		monkeypatch.setattr(browser_module, '_is_email_form_visible', AsyncMock(return_value=False))
		monkeypatch.setattr(browser_module, '_wait_for_login_page_ready', AsyncMock())
		log_state = AsyncMock()
		screenshot = AsyncMock()
		monkeypatch.setattr(browser_module, '_log_login_page_state', log_state)
		monkeypatch.setattr(browser_module, 'save_login_screenshot', screenshot)

		with pytest.raises(TimeoutError, match='Cannot open email login form'):
			await _open_email_login_form(page, 1000, provider='demo', account_name='Account 1')

		log_state.assert_awaited_once_with(page)
		screenshot.assert_awaited_once_with(page, 'demo', 'Account 1', 'email-form-timeout')

	async def test_set_input_value_fill_success_skips_js(self):
		locator = _Locator()
		locator.input_value.return_value = 'value'

		await _set_input_value(locator, 'value', 10_000)

		locator.fill.assert_awaited_once_with('value', timeout=10_000)
		locator.evaluate.assert_not_awaited()

	async def test_set_input_value_uses_js_when_fill_does_not_stick(self):
		locator = _Locator()
		locator.input_value.return_value = 'old'

		await _set_input_value(locator, 'new', 10_000)

		assert locator.evaluate.await_args.args[1] == 'new'

	async def test_fill_email_credentials_sets_both_inputs(self, monkeypatch):
		page = _Page()
		username = _Locator()
		password = _Locator()
		monkeypatch.setattr(browser_module, '_dismiss_blocking_overlays', AsyncMock())
		monkeypatch.setattr(browser_module, '_first_visible_locator', AsyncMock(side_effect=[username, password]))
		set_value = AsyncMock()
		monkeypatch.setattr(browser_module, '_set_input_value', set_value)

		await fill_email_credentials(page, 'user@example.invalid', 'synthetic-password', 20_000)

		assert set_value.await_args_list == [
			call(username, 'user@example.invalid', 15_000),
			call(password, 'synthetic-password', 15_000),
		]

	async def test_fill_email_credentials_raises_when_username_missing(self, monkeypatch):
		page = _Page()
		missing = _Locator()
		missing.wait_for.side_effect = RuntimeError('missing')
		page.locator = lambda _selector: missing
		monkeypatch.setattr(browser_module, '_dismiss_blocking_overlays', AsyncMock())
		monkeypatch.setattr(browser_module, '_first_visible_locator', AsyncMock(return_value=None))

		with pytest.raises(TimeoutError, match='username input'):
			await fill_email_credentials(page, 'user@example.invalid', 'password', 1000)

	async def test_submit_login_form_uses_force_click_and_waits(self, monkeypatch):
		page = _Page()
		submit = _Locator()
		submit.click.side_effect = [RuntimeError('covered'), None]
		monkeypatch.setattr(browser_module, '_first_visible_locator', AsyncMock(return_value=submit))
		wait_state = AsyncMock(return_value=True)
		wait_login = AsyncMock(return_value=True)
		monkeypatch.setattr(browser_module, '_wait_for_optional_load_state', wait_state)
		monkeypatch.setattr(browser_module, 'wait_for_logged_in', wait_login)

		await submit_login_form(page, 60_000)

		assert submit.click.await_args_list[-1] == call(force=True, timeout=15_000)
		assert wait_state.await_args_list == [
			call(page, 'domcontentloaded', 15_000),
			call(page, 'networkidle', 30_000),
		]
		wait_login.assert_awaited_once_with(page, 45_000)

	async def test_login_with_email_form_runs_steps_in_order(self, monkeypatch):
		page = _Page()
		opened = AsyncMock()
		filled = AsyncMock()
		submitted = AsyncMock()
		monkeypatch.setattr(browser_module, '_open_email_login_form', opened)
		monkeypatch.setattr(browser_module, 'fill_email_credentials', filled)
		monkeypatch.setattr(browser_module, 'submit_login_form', submitted)

		await login_with_email_form(
			page,
			'user@example.invalid',
			'synthetic-password',
			1000,
			provider='demo',
			account_name='Account 1',
		)

		opened.assert_awaited_once_with(page, 1000, provider='demo', account_name='Account 1')
		filled.assert_awaited_once_with(page, 'user@example.invalid', 'synthetic-password', 1000)
		submitted.assert_awaited_once_with(page, 1000)
