"""弹窗关闭逻辑测试（用假 Locator 模拟 Playwright，不启动浏览器）。"""

from typing import Any

import pytest

from utils.popups import _dismiss_popups_playwright, dismiss_popups, setup_popup_guard


class _FakeButton:
	def __init__(self, *, visible: bool, on_click=None):
		self._visible = visible
		self._on_click = on_click

	@property
	def first(self):
		return self

	async def is_visible(self):
		return self._visible

	async def click(self, timeout: int | None = None):
		if self._on_click:
			self._on_click()


class _FakeModal:
	"""单个模态框。has_login_form=True 表示这是登录表单，不应被关闭。"""

	def __init__(self, *, visible: bool = True, has_login_form: bool = False, close_style: str = 'announcement'):
		self.visible = visible
		self.has_login_form = has_login_form
		self.close_style = close_style
		self.close_calls = 0

	async def is_visible(self):
		return self.visible

	def _close(self):
		self.close_calls += 1
		self.visible = False

	def get_by_role(self, _role: str, name=None):
		matched = self.close_style == 'announcement'
		return _FakeButton(visible=matched, on_click=self._close)

	def locator(self, selector: str):
		if 'semi-form' in selector:
			return _FakeCounter(1 if self.has_login_form else 0)
		return _FakeButton(visible=self.close_style == 'x-button', on_click=self._close)


class _FakeCounter:
	def __init__(self, value: int):
		self._value = value

	async def count(self):
		return self._value


class _FakeModalList:
	def __init__(self, modals: list[_FakeModal]):
		self._modals = modals

	async def count(self):
		return len(self._modals)

	def nth(self, index: int):
		return self._modals[index]


class _FakePage:
	def __init__(self, modals: list[_FakeModal], *, js_closed: int = 0):
		self._modals = modals
		self._js_closed = js_closed
		self.init_scripts: list[str] = []
		self.evaluated: list[str] = []

	def locator(self, _selector: str):
		return _FakeModalList(self._modals)

	async def add_init_script(self, script: str):
		self.init_scripts.append(script)

	async def evaluate(self, script: str):
		self.evaluated.append(script)
		return self._js_closed


def _page(modals: list[_FakeModal], *, js_closed: int = 0) -> Any:
	return _FakePage(modals, js_closed=js_closed)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
	async def fake_sleep(_seconds):
		return None

	monkeypatch.setattr('utils.popups.asyncio.sleep', fake_sleep)


class TestSetupPopupGuard:
	async def test_injects_init_script(self):
		page = _FakePage([])

		await setup_popup_guard(page)  # type: ignore[arg-type]

		assert len(page.init_scripts) == 1
		assert '__dismissModals' in page.init_scripts[0]


class TestDismissPopupsPlaywright:
	async def test_no_modals_returns_zero(self):
		assert await _dismiss_popups_playwright(_page([])) == 0

	async def test_closes_announcement_modal(self):
		modal = _FakeModal(close_style='announcement')

		assert await _dismiss_popups_playwright(_page([modal])) == 1
		assert modal.close_calls == 1

	async def test_falls_back_to_x_button(self):
		modal = _FakeModal(close_style='x-button')

		assert await _dismiss_popups_playwright(_page([modal])) == 1
		assert modal.close_calls == 1

	async def test_login_form_modal_is_never_closed(self):
		modal = _FakeModal(has_login_form=True)

		assert await _dismiss_popups_playwright(_page([modal])) == 0
		assert modal.close_calls == 0

	async def test_invisible_modal_is_skipped(self):
		modal = _FakeModal(visible=False)

		assert await _dismiss_popups_playwright(_page([modal])) == 0

	async def test_closes_multiple_modals(self):
		modals = [_FakeModal(), _FakeModal(), _FakeModal()]

		assert await _dismiss_popups_playwright(_page(modals)) == 3
		assert all(m.close_calls == 1 for m in modals)

	async def test_unclosable_modal_does_not_loop_forever(self):
		"""无可点按钮的可见模态框：一轮无进展即退出，不会死循环。"""
		modal = _FakeModal(close_style='none')

		assert await _dismiss_popups_playwright(_page([modal])) == 0
		assert modal.visible is True


class TestDismissPopups:
	async def test_sums_playwright_and_js_results(self):
		page = _page([_FakeModal()], js_closed=2)

		assert await dismiss_popups(page) == 3

	async def test_js_only_result(self):
		page = _page([], js_closed=4)

		assert await dismiss_popups(page) == 4

	async def test_none_js_result_is_treated_as_zero(self):
		page = _FakePage([], js_closed=0)
		page._js_closed = None  # type: ignore[assignment]

		assert await dismiss_popups(page) == 0  # type: ignore[arg-type]

	async def test_js_dismiss_script_is_always_evaluated(self):
		page = _FakePage([])

		await dismiss_popups(page)  # type: ignore[arg-type]

		assert len(page.evaluated) == 1
