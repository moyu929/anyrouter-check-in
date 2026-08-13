"""utils/proxy_selector 节点选择器单元测试（全离线，用 MockTransport 模拟 mihomo REST API）。"""

from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest

from utils import proxy_selector as ps
from utils.proxy_selector import NodeSelector


@pytest.fixture(autouse=True)
def _clean_selector_state():
	ps.reset_selector_state()
	yield
	ps.reset_selector_state()
	ps._no_available_node = False


class FakeMihomo:
	"""模拟 mihomo external-controller：三区域 selector 组 + AUTO。"""

	def __init__(self, groups: dict[str, list[str]] | None = None):
		default_groups = {
			'🇯🇵 日本': ['jp-fallback', 'jp-1'],
			'🇸🇬 新加坡': ['sg-1'],
			'🇭🇰 香港': ['hk-1'],
		}
		self.groups = groups or default_groups
		self.delays: dict[str, int | None] = {}
		self.select_calls: list[tuple[str, dict]] = []
		self.group_errors: set[str] = set()

	def handler(self, request: httpx.Request) -> httpx.Response:
		path = urllib.parse.unquote(request.url.path)
		if request.method == 'PUT':
			body = json.loads(request.content or b'{}')
			self.select_calls.append((path, body))
			return httpx.Response(200, json={})

		if path.endswith('/delay'):
			node = path.split('/proxies/')[1].split('/delay')[0]
			delay = self.delays.get(node, 100)
			if delay is None:
				return httpx.Response(404)
			return httpx.Response(200, json={'delay': delay})

		name = path[len('/proxies/') :]
		if name in self.group_errors:
			return httpx.Response(500)
		if name == 'AUTO':
			return httpx.Response(200, json={'all': list(self.groups), 'now': ''})
		if name in self.groups:
			return httpx.Response(200, json={'all': self.groups[name], 'now': ''})
		return httpx.Response(404, json={})


def _make_selector(monkeypatch, fake: FakeMihomo, *, verify: bool | None = True) -> NodeSelector:
	monkeypatch.setenv('MIHOMO_CONTROLLER', 'http://127.0.0.1:9090')
	monkeypatch.delenv('MIHOMO_SECRET_FILE', raising=False)
	sel = NodeSelector()
	sel._client = httpx.Client(transport=httpx.MockTransport(fake.handler))
	if verify is not None:
		monkeypatch.setattr(sel, 'verify_connectivity', lambda _url: verify)
	return sel


class TestAvailable:
	def test_not_available_without_controller(self, monkeypatch):
		monkeypatch.delenv('MIHOMO_CONTROLLER', raising=False)
		assert ps.available() is False

	def test_available_with_controller(self, monkeypatch):
		monkeypatch.setenv('MIHOMO_CONTROLLER', 'http://127.0.0.1:9090')
		assert ps.available() is True

	def test_blank_controller_treated_as_unset(self, monkeypatch):
		monkeypatch.setenv('MIHOMO_CONTROLLER', '   ')
		assert ps.available() is False


class TestSelectNode:
	def test_selects_lowest_delay_node(self, monkeypatch):
		fake = FakeMihomo()
		fake.delays = {'jp-1': 50, 'jp-fallback': 200}
		sel = _make_selector(monkeypatch, fake, verify=True)

		node = sel.select_node('http://127.0.0.1:7890')

		assert node == 'jp-1'
		assert ps.current_proxy_node() == 'jp-1'
		# 应先将区域组切到节点，再将 AUTO 切到该区域（select_calls 在 fake 上）
		assert ('/proxies/🇭🇰 香港', {'name': 'jp-1'}) not in fake.select_calls
		# 区域名带国旗 emoji
		assert any(call[0] == '/proxies/🇯🇵 日本' and call[1] == {'name': 'jp-1'} for call in fake.select_calls)
		assert any(call[0] == '/proxies/AUTO' and call[1] == {'name': '🇯🇵 日本'} for call in fake.select_calls)

	def test_prefers_japan_over_other_regions(self, monkeypatch):
		fake = FakeMihomo()
		# 将所有日本节点延迟设为高，但仍应优先日本组
		fake.delays = {'jp-1': 500, 'jp-fallback': 600, 'hk-1': 10, 'sg-1': 20}
		sel = _make_selector(monkeypatch, fake, verify=True)

		# 日本有可用节点时，即使延迟高于其他区域也应选日本（jp-1 是日本组最快）
		assert sel.select_node('http://127.0.0.1:7890') == 'jp-1'

	def test_skips_verify_failed_node_and_tries_next(self, monkeypatch):
		fake = FakeMihomo()
		fake.delays = {'jp-1': 50, 'jp-fallback': 60}
		sel = _make_selector(monkeypatch, fake, verify=False)

		# 全部节点 verify 失败 → 返回 None，且标记无可用节点
		assert sel.select_node('http://127.0.0.1:7890') is None
		assert ps.no_available_node() is True
		# 失败节点应被排除
		assert ps.excluded_node_count() >= 1

	def test_tries_next_node_when_first_excluded(self, monkeypatch):
		"""当首个节点被手动排除时，应选择组内次低延迟节点。"""
		fake = FakeMihomo()
		fake.delays = {'jp-1': 50, 'jp-fallback': 60}
		sel = _make_selector(monkeypatch, fake, verify=True)

		# 手动排除 jp-1 后，应选到次低延迟的 jp-fallback（verify=True 表示成功）
		sel.exclude_node('jp-1')
		node = sel.select_node('http://127.0.0.1:7890')
		assert node == 'jp-fallback'

	def test_select_skips_failed_verify_and_continues_in_region(self, monkeypatch):
		"""连通性验证首个节点失败后，跳过并尝试同区域下一个节点。"""
		fake = FakeMihomo()
		fake.delays = {'jp-1': 50, 'jp-fallback': 60, 'sg-1': 100}
		# 自定义 verify：jp-1 失败，其余成功
		_verify_calls: list[str] = []

		def custom_verify(url: str) -> bool:
			# 当前节点通过 current_proxy_node 判断不了（还未设），改用 select_calls 推断
			# 这里使用一个 trick：第 N 次 verify 对应 ordered 中的第 N 个
			_verify_calls.append(url)
			# 第一次 verify（jp-1）失败，下一次（jp-fallback）成功
			if len(_verify_calls) == 1:
				return False
			return True

		sel = _make_selector(monkeypatch, fake, verify=None)
		monkeypatch.setattr(sel, 'verify_connectivity', custom_verify)

		node = sel.select_node('http://127.0.0.1:7890')
		# jp-1 验证失败后排除，jp-fallback 验证成功
		assert node == 'jp-fallback'
		assert 'jp-1' in ps._excluded_nodes

	def test_returns_none_and_sets_flag_when_all_regions_fail(self, monkeypatch):
		fake = FakeMihomo()
		sel = _make_selector(monkeypatch, fake, verify=False)

		assert sel.select_node('http://127.0.0.1:7890') is None
		assert ps.no_available_node() is True

	def test_short_circuits_when_no_available_node_already_set(self, monkeypatch):
		fake = FakeMihomo()
		sel = _make_selector(monkeypatch, fake, verify=False)
		assert sel.select_node('http://127.0.0.1:7890') is None
		before_calls = len(fake.select_calls)

		# 已标记无可用节点后，再次调用直接返回 None，不再遍历
		assert sel.select_node('http://127.0.0.1:7890') is None
		assert len(fake.select_calls) == before_calls

	def test_skips_delay_failed_nodes(self, monkeypatch):
		fake = FakeMihomo()
		fake.delays = {'jp-1': None, 'jp-fallback': 80}
		sel = _make_selector(monkeypatch, fake, verify=True)

		assert sel.select_node('http://127.0.0.1:7890') == 'jp-fallback'

	def test_handles_empty_region(self, monkeypatch):
		fake = FakeMihomo(groups={'🇯🇵 日本': [], '🇸🇬 新加坡': ['sg-1'], '🇭🇰 香港': ['hk-1']})
		sel = _make_selector(monkeypatch, fake, verify=True)

		assert sel.select_node('http://127.0.0.1:7890') == 'sg-1'

	def test_handles_group_request_error_and_continues(self, monkeypatch):
		fake = FakeMihomo()
		fake.group_errors = {'🇯🇵 日本'}
		sel = _make_selector(monkeypatch, fake, verify=True)

		assert sel.select_node('http://127.0.0.1:7890') == 'sg-1'

	def test_skips_manually_excluded_node(self, monkeypatch):
		fake = FakeMihomo()
		fake.delays = {'jp-1': 50, 'jp-fallback': 60}
		sel = _make_selector(monkeypatch, fake, verify=True)
		sel.exclude_node('jp-1')

		assert sel.select_node('http://127.0.0.1:7890') == 'jp-fallback'

	def test_no_available_flag_reset_after_success(self, monkeypatch):
		fake = FakeMihomo()
		# 先失败设标记
		sel_bad = _make_selector(monkeypatch, fake, verify=False)
		assert sel_bad.select_node('http://127.0.0.1:7890') is None
		assert ps.no_available_node() is True

		# 重置后可再次成功，标记复位
		ps.reset_selector_state()
		sel_ok = _make_selector(monkeypatch, fake, verify=True)
		assert sel_ok.select_node('http://127.0.0.1:7890') == 'jp-fallback'
		assert ps.no_available_node() is False

	def test_url_encoding_of_special_node_name(self, monkeypatch):
		fake = FakeMihomo(groups={'🇯🇵 日本': ['node / with space', 'jp-1'], '🇸🇬 新加坡': ['sg-1'], '🇭🇰 香港': ['hk-1']})
		fake.delays = {'node / with space': 30}
		sel = _make_selector(monkeypatch, fake, verify=True)

		assert sel.select_node('http://127.0.0.1:7890') == 'node / with space'


class TestControllerConfig:
	def test_load_secret_from_file(self, monkeypatch, tmp_path):
		secret_file = tmp_path / 'secret'
		secret_file.write_text('my-secret\n', encoding='utf-8')
		monkeypatch.setenv('MIHOMO_SECRET_FILE', str(secret_file))
		monkeypatch.setenv('MIHOMO_CONTROLLER', 'http://127.0.0.1:9090')

		sel = NodeSelector()
		assert sel.secret == 'my-secret'
		assert sel._headers()['Authorization'] == 'Bearer my-secret'

	def test_header_without_secret(self, monkeypatch):
		monkeypatch.setenv('MIHOMO_CONTROLLER', 'http://127.0.0.1:9090')
		monkeypatch.delenv('MIHOMO_SECRET_FILE', raising=False)

		sel = NodeSelector()
		assert 'Authorization' not in sel._headers()

	def test_missing_secret_file_returns_none(self, monkeypatch, tmp_path):
		monkeypatch.setenv('MIHOMO_SECRET_FILE', str(tmp_path / 'nope'))
		sel = NodeSelector()
		assert sel.secret is None


class TestStateHelpers:
	def test_reset_clears_everything(self, monkeypatch):
		fake = FakeMihomo()
		sel = _make_selector(monkeypatch, fake, verify=False)
		sel.select_node('http://127.0.0.1:7890')
		assert ps.no_available_node() is True

		ps.reset_selector_state()
		assert ps.current_proxy_node() is None
		assert ps.excluded_node_count() == 0
		assert ps.no_available_node() is False

	def test_exclude_node_marks_global(self, monkeypatch):
		sel = _make_selector(monkeypatch, FakeMihomo())
		sel.exclude_node('bad-node')
		assert ps.excluded_node_count() == 1

	def test_dump_selector_state(self, monkeypatch):
		fake = FakeMihomo()
		sel = _make_selector(monkeypatch, fake, verify=True)
		sel.select_node('http://127.0.0.1:7890')
		state = ps.dump_selector_state()
		assert 'current_node' in state


class TestNodeSelectorGroupIntrospection:
	def test_group_nodes(self, monkeypatch):
		fake = FakeMihomo()
		sel = _make_selector(monkeypatch, fake)
		assert sel.group_nodes('🇯🇵 日本') == ['jp-fallback', 'jp-1']

	def test_now(self, monkeypatch):
		fake = FakeMihomo()

		def handler(request: httpx.Request) -> httpx.Response:
			path = urllib.parse.unquote(request.url.path)
			if path == '/proxies/AUTO':
				return httpx.Response(200, json={'all': [], 'now': '🇯🇵 日本'})
			return httpx.Response(404)

		sel = _make_selector(monkeypatch, fake)
		sel._client = httpx.Client(transport=httpx.MockTransport(handler))
		assert sel.now('AUTO') == '🇯🇵 日本'

	def test_test_delay_returns_none_on_error(self, monkeypatch):
		def handler(request: httpx.Request) -> httpx.Response:
			return httpx.Response(500)

		sel = _make_selector(monkeypatch, FakeMihomo())
		sel._client = httpx.Client(transport=httpx.MockTransport(handler))
		assert sel.test_delay('jp-1') is None

	def test_verify_connectivity_false_when_request_fails(self, monkeypatch):
		class _FailingClient:
			def __init__(self, **kwargs):
				pass

			def __enter__(self):
				return self

			def __exit__(self, *args):
				return False

			def get(self, url):
				raise httpx.ConnectError('refused')

		monkeypatch.setattr(ps.httpx, 'Client', _FailingClient)
		sel = _make_selector(monkeypatch, FakeMihomo(), verify=None)
		assert sel.verify_connectivity('http://127.0.0.1:7890') is False

	def test_verify_connectivity_true_when_204(self, monkeypatch):
		class _OkClient:
			def __init__(self, **kwargs):
				pass

			def __enter__(self):
				return self

			def __exit__(self, *args):
				return False

			def get(self, url):
				return httpx.Response(204)

		monkeypatch.setattr(ps.httpx, 'Client', _OkClient)
		sel = _make_selector(monkeypatch, FakeMihomo(), verify=None)
		assert sel.verify_connectivity('http://127.0.0.1:7890') is True

	def test_verify_connectivity_false_on_non_204(self, monkeypatch):
		class _RedirClient:
			def __init__(self, **kwargs):
				pass

			def __enter__(self):
				return self

			def __exit__(self, *args):
				return False

			def get(self, url):
				return httpx.Response(403)

		monkeypatch.setattr(ps.httpx, 'Client', _RedirClient)
		sel = _make_selector(monkeypatch, FakeMihomo(), verify=None)
		assert sel.verify_connectivity('http://127.0.0.1:7890') is False