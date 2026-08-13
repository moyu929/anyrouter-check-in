"""mihomo 节点选择器 — 通过 external-controller API 按区域优先级选择可用节点。

设计目标：
- 不再依赖 mihomo 的 url-test / 300s 自动测速切换，改由 Python 主动控制 selector 组。
- 按区域顺序选择：🇯🇵 日本 → 🇸🇬 新加坡 → 🇭🇰 香港。
- 区域内并测延迟选最低 → 切换 → 轻量连通性验证；不通则排除该节点再试。
- 全部区域均无可用节点时返回 None（调用方回退直连）。
- 维护进程级"已排除节点"集合，跨账号与重试共享，避免反复踩同一坏节点。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from utils.debug import log

# 区域选择顺序（与 setup_mihomo_proxy.sh 的组名严格一致）
REGION_GROUPS = ['🇯🇵 日本', '🇸🇬 新加坡', '🇭🇰 香港']
AUTO_GROUP = 'AUTO'

# 连通性测试地址（可达性即可，WAF 是否通过由登录/签到尝试验证）
DEFAULT_TEST_URL = 'https://www.gstatic.com/generate_204'
_NODE_DELAY_TIMEOUT_MS = int(os.getenv('PROXY_NODE_DELAY_TIMEOUT_MS', '3000').strip() or 3000)
_VERIFY_TIMEOUT = 5.0


# ---- 进程级状态（跨账号与重试共享） ----
_excluded_nodes: set[str] = set()
_current_node: str | None = None
_no_available_node: bool = False
_lock = threading.Lock()


def reset_selector_state() -> None:
	"""重置节点选择器状态（测试用）。"""
	global _excluded_nodes, _current_node, _no_available_node
	with _lock:
		_excluded_nodes = set()
		_current_node = None
		_no_available_node = False


def current_proxy_node() -> str | None:
	"""返回当前选定的节点名（未选定或未使用代理时为 None）。"""
	with _lock:
		return _current_node


def no_available_node() -> bool:
	"""是否已确认无可用代理节点（select_node 返回 None 后为 True）。

	调用方据此可直接走直连，避免再用默认节点白试一次代理。
	"""
	with _lock:
		return _no_available_node


def excluded_node_count() -> int:
	"""返回已排除节点数量（日志用）。"""
	with _lock:
		return len(_excluded_nodes)


def _mark_excluded(node: str) -> None:
	with _lock:
		_excluded_nodes.add(node)


# ---- controller 连接信息 ----
def _controller_base() -> str | None:
	return os.getenv('MIHOMO_CONTROLLER', '').strip() or None


def _load_secret() -> str | None:
	path = os.getenv('MIHOMO_SECRET_FILE', '').strip()
	if path and os.path.exists(path):
		try:
			with open(path, encoding='utf-8') as f:
				return f.read().strip()
		except OSError:
			return None
	return None


def available() -> bool:
	"""controller 是否已配置（mihomo 是否可能已启动）。"""
	return _controller_base() is not None


class NodeSelector:
	"""mihomo 节点选择器：封装 external-controller REST API 并实现选择逻辑。"""

	# 区域组名 → 节点选择顺序
	REGIONS = REGION_GROUPS

	def __init__(self):
		self.base = _controller_base()
		self.secret = _load_secret()
		self._client = httpx.Client(timeout=10.0, trust_env=False)

	def _headers(self) -> dict:
		h = {'Accept': 'application/json'}
		if self.secret:
			h['Authorization'] = f'Bearer {self.secret}'
		return h

	@staticmethod
	def _quote(name: str) -> str:
		# 节点名可能含中文/空格/斜杠等，必须 URL 编码
		return urllib.parse.quote(name, safe='')

	def _get(self, path: str) -> dict:
		r = self._client.get(f'{self.base}{path}', headers=self._headers())
		r.raise_for_status()
		return dict(r.json())

	def _put(self, path: str, body: dict) -> dict:
		r = self._client.put(f'{self.base}{path}', json=body, headers=self._headers())
		r.raise_for_status()
		return dict(r.json())

	def group_nodes(self, group: str) -> list[str]:
		"""返回 selector 组内可选择节点列表（含子组名）。"""
		data = self._get(f'/proxies/{self._quote(group)}')
		return list(data.get('all', []))

	def now(self, group: str) -> str | None:
		"""返回组当前选中的节点。"""
		data = self._get(f'/proxies/{self._quote(group)}')
		return data.get('now')

	def exclude_node(self, node: str) -> None:
		"""将节点加入全局排除集合，后续选择不再考虑（跨账号与重试共享）。"""
		if node:
			_mark_excluded(node)

	def test_delay(self, node: str) -> int | None:
		"""测试节点延迟（毫秒），失败/超时返回 None。"""
		try:
			url = self._quote(DEFAULT_TEST_URL)
			data = self._get(f'/proxies/{self._quote(node)}/delay?url={url}&timeout={_NODE_DELAY_TIMEOUT_MS}')
			delay = data.get('delay')
			return int(delay) if isinstance(delay, (int, float)) else None
		except Exception:
			return None

	def select(self, group: str, node: str) -> None:
		"""切换 selector 组选中指定节点。"""
		self._put(f'/proxies/{self._quote(group)}', {'name': node})

	def verify_connectivity(self, proxy_url: str) -> bool:
		"""通过本地代理测试出网连通性（轻量，只证可达）。"""
		try:
			with httpx.Client(proxy=proxy_url, timeout=_VERIFY_TIMEOUT, trust_env=False) as c:
				r = c.get(DEFAULT_TEST_URL)
				return r.status_code in (200, 204)
		except Exception:
			return False

	def select_node(self, proxy_url: str) -> str | None:
		"""按区域优先级选择可用节点并切换。

		返回选中的节点名；全部区域均不可用时返回 None（调用方回退直连）。
		"""
		global _current_node, _no_available_node

		assert self.base, 'mihomo controller 未配置，无法选择节点'

		# 已知无可用节点时直接返回 None（避免重复遍历）
		if no_available_node():
			return None

		for region in self.REGIONS:
			try:
				nodes = self.group_nodes(region)
			except Exception as e:
				log.warn(f'节点选择: 获取区域 {region} 节点列表失败: {str(e)[:80]}')
				continue

			with _lock:
				candidates = [n for n in nodes if n not in _excluded_nodes and n != 'DIRECT']
			if not candidates:
				log.warn(f'节点选择: 区域 {region} 无可用候选节点（已排除 {excluded_node_count()} 个）')
				continue

			# 并行测延迟，筛掉超时/失败节点
			delays: dict[str, int] = {}
			workers = min(8, len(candidates))
			with ThreadPoolExecutor(max_workers=workers) as ex:
				future_map = {ex.submit(self.test_delay, n): n for n in candidates}
				for fut in as_completed(future_map):
					node = future_map[fut]
					delay = fut.result()
					if delay is not None:
						delays[node] = delay

			if not delays:
				log.warn(f'节点选择: 区域 {region} 所有节点测速超时/失败')
				continue

			ordered = sorted(delays, key=lambda node: delays[node])
			for node in ordered:
				log.info(f'节点选择: 候选 {region} -> {node}（延迟 {delays[node]}ms）')
				try:
					self.select(region, node)  # 区域 selector 切到该节点
					self.select(AUTO_GROUP, region)  # AUTO 切到该区域
				except Exception as e:
					log.warn(f'节点选择: 切换到 {node} 失败: {str(e)[:80]}，排除')
					_mark_excluded(node)
					continue

				if self.verify_connectivity(proxy_url):
					with _lock:
						_current_node = node
						_no_available_node = False
					log.success(f'节点选择: 已选定可用节点 {node}（{region}，延迟 {delays[node]}ms）')
					return node

				log.warn(f'节点选择: 节点 {node} 连通性验证失败，排除')
				_mark_excluded(node)

		with _lock:
			_current_node = None
			_no_available_node = True
		log.warn(f'节点选择: 所有区域均无可用节点（已排除 {excluded_node_count()} 个），将不使用代理（直连）')
		return None


def dump_selector_state() -> str:
	"""调试用：输出选择器状态。"""
	with _lock:
		return json.dumps({'current_node': _current_node, 'excluded_count': len(_excluded_nodes)}, ensure_ascii=False)
