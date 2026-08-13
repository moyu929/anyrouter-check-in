"""check_in_account_with_retry 与节点问题分类单元测试（全离线，mock 掉真实签到）。"""

from __future__ import annotations

import httpx
import pytest

import checkin
from checkin import (
	ProxyNodeIssue,
	_is_node_issue_exception,
	check_in_account_with_retry,
)
from utils.config import AccountConfig, AppConfig, ProviderConfig


class _FakeSelector:
	"""最小节点选择器替身，记录调用并返回预设节点。"""

	def __init__(self, node_result=None):
		self.node_result = node_result
		self.excluded: list[str] = []
		self.select_calls = 0

	def exclude_node(self, node: str) -> None:
		self.excluded.append(node)

	def select_node(self, proxy_url: str):
		self.select_calls += 1
		return self.node_result


def _app_config(*, use_proxy: bool = True) -> AppConfig:
	return AppConfig(
		providers={'tp': ProviderConfig(name='tp', domain='https://example.com', use_proxy=use_proxy)}
	)


def _account() -> AccountConfig:
	return AccountConfig(cookies={'session': 'x'}, provider='tp')


@pytest.fixture
def fake_checkin(monkeypatch):
	"""替换 check_in_account 为可编程替身，记录 (force_direct) 调用。"""
	calls: list[dict] = []
	behaviour = {}

	async def _fake(account, index, app_config, *, force_direct=False):
		calls.append({'force_direct': force_direct})
		key = f'call{len(calls)}'
		if behaviour.get(key) is True:
			return True, {'success': True}, {'success': True}
		if behaviour.get(key) == 'issue':
			raise ProxyNodeIssue('waf blocked')
		return True, {'success': True}, {'success': True}

	monkeypatch.setattr(checkin, 'check_in_account', _fake)
	return calls, behaviour


class TestIsNodeIssueException:
	def test_network_timeout_is_node_issue(self):
		assert _is_node_issue_exception(httpx.ConnectTimeout('t')) is True
		assert _is_node_issue_exception(httpx.ConnectError('c')) is True
		assert _is_node_issue_exception(httpx.NetworkError('n')) is True

	def test_retry_exhausted_runtime_error_is_node_issue(self):
		e = RuntimeError('请求 https://x 返回 HTTP 503（已重试 3 次）')
		assert _is_node_issue_exception(e) is True

	def test_other_exception_not_node_issue(self):
		assert _is_node_issue_exception(ValueError('boom')) is False
		assert _is_node_issue_exception(RuntimeError('plain')) is False


class TestRetryDispatch:
	async def test_non_proxy_account_skips_retry(self, fake_checkin, monkeypatch):
		calls, _ = fake_checkin
		selector = _FakeSelector(node_result='node2')

		result = await check_in_account_with_retry(_account(), 0, _app_config(use_proxy=False), selector)

		assert result[0] is True
		assert len(calls) == 1
		assert selector.select_calls == 0

	async def test_node_selector_none_skips_retry(self, fake_checkin, monkeypatch):
		calls, _ = fake_checkin

		result = await check_in_account_with_retry(_account(), 0, _app_config(), None)

		assert result[0] is True
		assert len(calls) == 1

	async def test_no_available_node_directs_without_selector(self, fake_checkin, monkeypatch):
		calls, _ = fake_checkin
		selector = _FakeSelector(node_result='node2')
		monkeypatch.setattr(checkin, 'no_available_node', lambda: True)

		result = await check_in_account_with_retry(_account(), 0, _app_config(), selector)

		assert result[0] is True
		assert len(calls) == 1
		assert calls[0]['force_direct'] is True  # 直接直连，不请求节点选择器
		assert selector.select_calls == 0

	async def test_success_without_retry(self, fake_checkin, monkeypatch):
		calls, _ = fake_checkin
		selector = _FakeSelector(node_result='node2')
		monkeypatch.setattr(checkin, 'no_available_node', lambda: False)

		result = await check_in_account_with_retry(_account(), 0, _app_config(), selector)

		assert result[0] is True
		assert len(calls) == 1
		assert selector.select_calls == 0

	async def test_retry_switches_node_then_succeeds(self, fake_checkin, monkeypatch):
		calls, behaviour = fake_checkin
		behaviour['call1'] = 'issue'
		selector = _FakeSelector(node_result='node2')
		monkeypatch.setattr(checkin, 'no_available_node', lambda: False)
		monkeypatch.setattr(checkin, 'current_proxy_node', lambda: 'node1')

		result = await check_in_account_with_retry(_account(), 0, _app_config(), selector)

		assert result[0] is True
		assert len(calls) == 2
		assert selector.select_calls == 1
		assert selector.excluded == ['node1']  # 失败的当前节点被排除

	async def test_retry_exhausted_returns_failure(self, fake_checkin, monkeypatch):
		calls, behaviour = fake_checkin
		# 每次都抛节点问题
		monkeypatch.setattr(checkin, 'check_in_account', _always_issue)
		monkeypatch.setattr(checkin, 'no_available_node', lambda: False)
		monkeypatch.setattr(checkin, 'current_proxy_node', lambda: 'nodeX')
		monkeypatch.setenv('PROXY_RETRY_TIMES', '2')
		selector = _FakeSelector(node_result='node2')

		result = await check_in_account_with_retry(_account(), 0, _app_config(), selector)

		assert result == (False, None, None)
		# 初始 + 2 次重试 = 3 次尝试
		assert _always_issue.calls == 3
		assert selector.select_calls == 2

	async def test_direct_fallback_when_no_node_available(self, fake_checkin, monkeypatch):
		calls, behaviour = fake_checkin
		behaviour['call1'] = 'issue'
		# 节点选择器已无可用节点 → 直连兜底
		selector = _FakeSelector(node_result=None)
		monkeypatch.setattr(checkin, 'no_available_node', lambda: False)
		monkeypatch.setattr(checkin, 'current_proxy_node', lambda: 'node1')

		result = await check_in_account_with_retry(_account(), 0, _app_config(), selector)

		assert result[0] is True
		assert len(calls) == 2
		assert calls[1]['force_direct'] is True  # 第二次为直连


async def _always_issue(*args, **kwargs):
	_always_issue.calls = getattr(_always_issue, 'calls', 0) + 1
	raise ProxyNodeIssue('always issue')