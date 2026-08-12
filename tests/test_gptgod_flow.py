"""GPTGod 签到主流程测试（用 MockTransport 完全拦截，绝不触达真实站点或账号）。"""

import base64
import json

import httpx
import pytest

from utils import gptgod as gptgod_module
from utils.gptgod import gptgod_checkin

FAKE_EMAIL = 'tester@example.invalid'
FAKE_PASSWORD = 'not-a-real-password'

# 33 项恒等重排表 + 全零 XOR 密钥，方便断言签名内容
_PY = list(range(33))
_MC = [0] * 16


def _sign_js() -> str:
	return f'var a = [{",".join(map(str, _PY))}];var b = [{",".join(map(str, _MC))}];'


def _encrypt(plaintext: str, key: str) -> str:
	t = [(ord(key[h % len(key)]) ^ 66) + h * 55 & 255 for h in range(16)]
	return base64.b64encode(bytes(ord(c) ^ t[i % 16] for i, c in enumerate(plaintext))).decode('ascii')


class _Scenario:
	"""可编程的 GPTGod 假服务端。"""

	def __init__(
		self,
		*,
		login_code: int = 0,
		credits_before: int = 100,
		credits_after: int = 130,
		already_checked: bool = False,
		checkin_code: int = 0,
		config_status: int = 200,
		config_key: str = 'secretkey',
		nonce_field: str = '_jztz',
	):
		self.login_code = login_code
		self.credits_before = credits_before
		self.credits_after = credits_after
		self.already_checked = already_checked
		self.checkin_code = checkin_code
		self.config_status = config_status
		self.config_key = config_key
		self.nonce_field = nonce_field
		self.checked_in = False
		self.paths: list[str] = []
		self.login_body: dict | None = None
		self.checkin_body: dict | None = None

	def __call__(self, request: httpx.Request) -> httpx.Response:
		path = request.url.path
		self.paths.append(path)

		if path == '/':
			return httpx.Response(200, text='<html></html>')

		if path == '/api/user/login':
			self.login_body = json.loads(request.content)
			return httpx.Response(200, json={'code': self.login_code, 'msg': 'bad credentials'})

		if path == '/api/user/info':
			checked = self.already_checked or self.checked_in
			tokens = self.credits_after if self.checked_in else self.credits_before
			return httpx.Response(200, json={'code': 0, 'data': {'tokens': tokens, 'checkin': checked}})

		if path == '/api/user/register-config':
			if self.config_status != 200:
				return httpx.Response(self.config_status)
			return httpx.Response(
				200,
				json={
					'code': 0,
					'data': {
						'_e': _encrypt(_sign_js(), self.config_key),
						'_k': self.config_key,
						'_n': self.nonce_field,
					},
				},
			)

		if path == '/api/user/checkin':
			self.checkin_body = json.loads(request.content)
			if self.checkin_code == 0:
				self.checked_in = True
			return httpx.Response(200, json={'code': self.checkin_code, 'msg': '签到失败'})

		return httpx.Response(404)


@pytest.fixture
def scenario_runner(monkeypatch, tmp_path):
	"""把 _make_client 换成 MockTransport 客户端，并隔离设备指纹目录、消除 sleep。"""
	monkeypatch.setattr(gptgod_module, '_DEVICE_FP_DIR', str(tmp_path))
	monkeypatch.setattr(gptgod_module.time, 'sleep', lambda _s: None)
	monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)

	def run(scenario: _Scenario):
		monkeypatch.setattr(
			gptgod_module,
			'_make_client',
			lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(scenario)),
		)
		return gptgod_checkin('Account 1', FAKE_EMAIL, FAKE_PASSWORD)

	return run


class TestGptgodCheckinFlow:
	def test_happy_path_reports_credit_gain(self, scenario_runner):
		scenario = _Scenario(credits_before=100, credits_after=130)

		success, before, after = scenario_runner(scenario)

		assert success is True
		assert before == {
			'success': True,
			'quota': 100,
			'used_quota': 0,
			'unit': 'credits',
			'display': ':money: Current tokens: 100',
		}
		assert after is not None and after['quota'] == 130
		assert '/api/user/checkin' in scenario.paths

	def test_password_is_md5_hashed_before_submission(self, scenario_runner):
		import hashlib

		scenario = _Scenario()

		scenario_runner(scenario)

		assert scenario.login_body is not None
		assert scenario.login_body['password'] == hashlib.md5(FAKE_PASSWORD.encode()).hexdigest()  # nosec B324
		assert scenario.login_body['password'] != FAKE_PASSWORD
		assert scenario.login_body['email'] == FAKE_EMAIL

	def test_signature_field_name_comes_from_nonce(self, scenario_runner):
		scenario = _Scenario(nonce_field='_zzz')

		scenario_runner(scenario)

		assert scenario.checkin_body is not None
		assert '_zzz' in scenario.checkin_body
		assert scenario.checkin_body['_k'] == 'secretkey'

	def test_signature_payload_decodes_to_33_fingerprint_items(self, scenario_runner):
		scenario = _Scenario()

		scenario_runner(scenario)

		assert scenario.checkin_body is not None
		# 恒等重排 + 全零 XOR 密钥下，base64 解码即为原指纹 JSON
		decoded = json.loads(base64.b64decode(scenario.checkin_body['_jztz']).decode('utf-8'))
		assert len(decoded) == 33

	def test_login_failure_short_circuits(self, scenario_runner):
		scenario = _Scenario(login_code=1)

		success, before, after = scenario_runner(scenario)

		assert success is False
		assert before is not None and before['success'] is False
		assert after is None
		assert '/api/user/checkin' not in scenario.paths

	def test_already_checked_in_skips_checkin_request(self, scenario_runner):
		scenario = _Scenario(already_checked=True, credits_before=77)

		success, before, after = scenario_runner(scenario)

		assert success is True
		assert before is not None and before['quota'] == 77
		assert after is not None and after['quota'] == 77
		assert '/api/user/checkin' not in scenario.paths
		assert '/api/user/register-config' not in scenario.paths

	def test_register_config_failure_returns_before_info(self, scenario_runner):
		scenario = _Scenario(config_status=500)

		success, before, after = scenario_runner(scenario)

		assert success is False
		assert before is not None and before['success'] is True
		assert after is None

	def test_checkin_api_rejection_is_reported_as_failure(self, scenario_runner):
		scenario = _Scenario(checkin_code=1)

		success, _before, after = scenario_runner(scenario)

		assert success is False
		assert after is None

	def test_unchanged_credits_still_returns_success(self, scenario_runner):
		scenario = _Scenario(credits_before=100, credits_after=100)

		success, before, after = scenario_runner(scenario)

		assert success is True
		assert before is not None and after is not None
		assert before['quota'] == after['quota'] == 100

	def test_bad_signature_params_abort_before_checkin(self, scenario_runner, monkeypatch):
		monkeypatch.setattr(gptgod_module, 'extract_sign_params', lambda _js: None)
		scenario = _Scenario()

		success, before, after = scenario_runner(scenario)

		assert success is False
		assert before is not None and before['success'] is True
		assert after is None
		assert '/api/user/checkin' not in scenario.paths
