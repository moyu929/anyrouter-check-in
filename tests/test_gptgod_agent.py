"""GPTGod God Agent 客户端档签到测试（MockTransport 完全拦截，绝不触达真实站点/账号）。

覆盖：
- 33 槽风控快照构造（槽位顺序即服务端契约）
- encodeRiskSnapshot 编码（与客户端 encodeRiskSnapshot 五步一致，可逆验证）
- device/token 签发与持久化 TTL 复用
- 签到请求带 Bearer 头 + 风控 body
- 风控会话不可用时的中断行为（避免白签）
"""

import base64
import json
import time

import httpx
import pytest

from utils import gptgod as gptgod_module
from utils.gptgod import (
	build_agent_risk_snapshot,
	encode_risk_snapshot,
	gptgod_agent_checkin,
)

FAKE_EMAIL = 'agent-tester@example.invalid'
FAKE_PASSWORD = 'not-a-real-password'

# 33 项恒等重排表 + 16 字节固定 key，方便断言编码内容
_PERM = list(range(33))
_KEY = '0123456789abcdef'
_RISK_FIELD = '_risk_abc'


def _make_session(**overrides) -> dict:
	session = {
		'enabled': True,
		'sid': 'session-sid-001',
		'field': _RISK_FIELD,
		'key': _KEY,
		'perm': _PERM,
	}
	session.update(overrides)
	return session


def _decode_risk(encoded: str) -> list:
	"""encodeRiskSnapshot 的逆运算：base64 → XOR → UTF-8 → JSON。"""
	raw = base64.b64decode(encoded)
	restored = bytes(b ^ ord(_KEY[i & 15]) for i, b in enumerate(raw)).decode('utf-8')
	return json.loads(restored)


class _AgentScenario:
	"""可编程的 God Agent 客户端档假服务端。"""

	def __init__(
		self,
		*,
		login_code: int = 0,
		credits_before: int = 100,
		credits_after: int = 150,
		already_checked: bool = False,
		checkin_code: int = 0,
		checkin_credits: int = 2000,
		token_code: int = 0,
		token_value: str = 'dev-token-xxx',
		token_expires: int = 43200,
		session: dict | None = None,
	):
		self.login_code = login_code
		self.credits_before = credits_before
		self.credits_after = credits_after
		self.already_checked = already_checked
		self.checkin_code = checkin_code
		self.checkin_credits = checkin_credits
		self.token_code = token_code
		self.token_value = token_value
		self.token_expires = token_expires
		self.session = session if session is not None else _make_session()
		self.checked_in = False
		self.paths: list[str] = []
		self.checkin_body: dict | None = None
		self.checkin_headers: dict | None = None
		self.token_body: dict | None = None

	def __call__(self, request: httpx.Request) -> httpx.Response:
		path = request.url.path
		self.paths.append(path)

		if path == '/':
			return httpx.Response(200, text='<html></html>')

		if path == '/api/user/login':
			return httpx.Response(200, json={'code': self.login_code, 'msg': 'bad credentials'})

		if path == '/api/user/info':
			checked = self.already_checked or self.checked_in
			tokens = self.credits_after if self.checked_in else self.credits_before
			return httpx.Response(
				200,
				json={
					'code': 0,
					'data': {
						'tokens': tokens,
						'checkin': checked,
						'settings': {'groupCode': 'default'},
					},
				},
			)

		if path == '/api/user/device/token':
			self.token_body = json.loads(request.content)
			if self.token_code != 0:
				return httpx.Response(200, json={'code': self.token_code, 'msg': 'token issue failed'})
			return httpx.Response(
				200,
				json={'code': 0, 'token': self.token_value, 'expires_in': self.token_expires},
			)

		if path == '/api/user/risk/native-session':
			return httpx.Response(200, json=self.session)

		if path == '/api/user/checkin':
			self.checkin_body = json.loads(request.content)
			self.checkin_headers = dict(request.headers)
			if self.checkin_code == 0:
				self.checked_in = True
			return httpx.Response(
				200,
				json={
					'code': self.checkin_code,
					'credits': self.checkin_credits,
					'msg': '签到失败',
				},
			)

		return httpx.Response(404)


@pytest.fixture
def scenario_runner(monkeypatch, tmp_path):
	"""把 _make_client 换成 MockTransport 客户端，隔离状态文件，并消除 sleep。"""
	monkeypatch.setattr(gptgod_module.time, 'sleep', lambda _s: None)
	monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
	monkeypatch.setenv('GPTGOD_AGENT_STATE_FILE', str(tmp_path / 'agent_state.json'))

	def run(scenario: _AgentScenario):
		monkeypatch.setattr(
			gptgod_module,
			'_make_client',
			lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(scenario)),
		)
		return gptgod_agent_checkin('Agent 1', FAKE_EMAIL, FAKE_PASSWORD)

	return run


class TestBuildAgentRiskSnapshot:
	def test_has_33_slots(self):
		device = {'platform': 'Windows', 'machine_id': 'm', 'os_version': 'v', 'install_ts': 100, 'app_version': '0.9.0'}

		slots = build_agent_risk_snapshot(device)

		assert len(slots) == 33

	def test_identity_slots_stable_and_sourced_from_device(self):
		device = {
			'platform': 'Linux',
			'machine_id': 'fixed-machine',
			'os_version': 'Ubuntu 22.04',
			'install_ts': 12345678,
			'app_version': '0.8.1',
		}

		a = build_agent_risk_snapshot(device)
		b = build_agent_risk_snapshot(device)

		for idx in (5, 26, 27, 28, 29, 30, 31):
			assert a[idx] == b[idx]
		assert a[26] == 'app'  # 来源标识固定
		assert a[27] == 'fixed-machine'
		assert a[30] == 12345678

	def test_behavior_slots_vary_between_calls(self):
		device = {'platform': 'Windows', 'machine_id': 'm', 'os_version': 'v', 'install_ts': 100, 'app_version': '0.9.0'}

		a = build_agent_risk_snapshot(device)
		b = build_agent_risk_snapshot(device)

		# 行为参数（停留/点击/时序）允许随机波动
		assert a[17] != b[17] or a[18] != b[18] or a[32] != b[32]


class TestEncodeRiskSnapshot:
	def test_identity_perm_can_be_decoded_back(self):
		slots = [f'v{i}' for i in range(33)]

		encoded = encode_risk_snapshot(_KEY, _PERM, slots)

		assert _decode_risk(encoded) == slots

	def test_reorder_perm_matches_shuffled_order(self):
		perm = list(reversed(range(33)))
		slots = [f'v{i}' for i in range(33)]

		encoded = encode_risk_snapshot(_KEY, perm, slots)

		assert _decode_risk(encoded) == [slots[i] for i in perm]

	def test_utf8_preserved_not_ascii_escaped(self):
		slots = [f'中文-{i}' for i in range(33)]

		encoded = encode_risk_snapshot(_KEY, _PERM, slots)

		assert _decode_risk(encoded) == slots

	def test_output_is_deterministic(self):
		slots = [i for i in range(33)]

		assert encode_risk_snapshot(_KEY, _PERM, slots) == encode_risk_snapshot(_KEY, _PERM, slots)


class TestAgentCheckinFlow:
	def test_happy_path_sends_bearer_and_risk_body(self, scenario_runner):
		scenario = _AgentScenario(credits_before=100, credits_after=150)

		success, before, after = scenario_runner(scenario)

		assert success is True
		assert before is not None and before['quota'] == 100
		assert after is not None and after['quota'] == 150
		# 签到请求带客户端身份令牌
		assert scenario.checkin_headers is not None
		assert scenario.checkin_headers.get('authorization') == 'Bearer dev-token-xxx'
		# 风控 body 结构 {_k: sid, [field]: encoded}
		assert scenario.checkin_body is not None
		assert scenario.checkin_body['_k'] == 'session-sid-001'
		assert _RISK_FIELD in scenario.checkin_body
		assert len(_decode_risk(scenario.checkin_body[_RISK_FIELD])) == 33
		# token 签发参数：device_id + 分组来自首次 user/info
		assert scenario.token_body is not None
		assert scenario.token_body['group_code'] == 'default'
		assert scenario.token_body['device_id']

	def test_token_is_cached_and_reused_within_ttl(self, scenario_runner, monkeypatch):
		scenario = _AgentScenario()
		state_path = gptgod_module.agent_state_file()

		# 第一次签到：签发 token 并落盘
		scenario_runner(scenario)
		issued_count = scenario.paths.count('/api/user/device/token')
		assert issued_count == 1
		state = json.load(open(state_path, encoding='utf-8'))
		assert state['device']['device_id']
		assert state['tokens'][FAKE_EMAIL]['token'] == 'dev-token-xxx'

		# 重建场景（新 client 无缓存），第二次签到应复用持久化 token 不再签发
		scenario2 = _AgentScenario()
		any_result = scenario_runner(scenario2)
		assert any_result[0] is True
		assert scenario2.paths.count('/api/user/device/token') == 0

	def test_expired_token_is_reissued(self, scenario_runner):
		scenario = _AgentScenario(token_expires=1)  # 1 秒 TTL
		scenario_runner(scenario)

		# 过期后重建场景，应重新签发
		scenario2 = _AgentScenario(token_expires=43200)
		scenario_runner(scenario2)

		assert scenario2.paths.count('/api/user/device/token') == 1

	def test_already_checked_skips_everything_including_token(self, scenario_runner):
		scenario = _AgentScenario(already_checked=True, credits_before=77)

		success, before, after = scenario_runner(scenario)

		assert success is True
		assert before is not None and before['quota'] == 77
		assert after is not None and after['quota'] == 77
		assert '/api/user/device/token' not in scenario.paths
		assert '/api/user/checkin' not in scenario.paths

	def test_login_failure_short_circuits(self, scenario_runner):
		scenario = _AgentScenario(login_code=1)

		success, before, after = scenario_runner(scenario)

		assert success is False
		assert before is not None and before['success'] is False
		assert after is None
		assert '/api/user/device/token' not in scenario.paths

	def test_device_token_issue_failure_aborts_checkin(self, scenario_runner):
		scenario = _AgentScenario(token_code=1001)

		success, _before, after = scenario_runner(scenario)

		assert success is False
		assert after is None
		assert '/api/user/checkin' not in scenario.paths

	def test_risk_session_parameter_missing_aborts_checkin(self, scenario_runner):
		scenario = _AgentScenario(session=_make_session(field=''))  # field 缺失

		success, _before, after = scenario_runner(scenario)

		assert success is False
		assert after is None
		# 宁可中断，绝不发出"无风控"的签到请求
		assert '/api/user/checkin' not in scenario.paths

	def test_risk_enabled_false_falls_back_to_empty_body(self, scenario_runner):
		scenario = _AgentScenario(session=_make_session(enabled=False))

		success, _before, _after = scenario_runner(scenario)

		# 站点关闭风控时跟随客户端照常签（body 无风控字段）
		assert success is True
		assert scenario.checkin_body == {}
		assert scenario.checkin_headers is not None
		assert scenario.checkin_headers.get('authorization') == 'Bearer dev-token-xxx'

	def test_checkin_api_rejection_reported(self, scenario_runner):
		scenario = _AgentScenario(checkin_code=1)

		success, _before, after = scenario_runner(scenario)

		assert success is False
		assert after is None

	def test_device_id_is_stable_across_runs(self, scenario_runner):
		scenario = _AgentScenario()
		scenario_runner(scenario)
		state = json.load(open(gptgod_module.agent_state_file(), encoding='utf-8'))
		first_id = state['device']['device_id']

		# 再次运行（新进程模拟：清覆盖文件所属实例的进程内状态，但文件保留）
		scenario2 = _AgentScenario()
		scenario_runner(scenario2)
		state2 = json.load(open(gptgod_module.agent_state_file(), encoding='utf-8'))

		assert state2['device']['device_id'] == first_id