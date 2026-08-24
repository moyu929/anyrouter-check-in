"""签到中枢 checkin_core 直接单测（全离线，不触网、不依赖具体分支）。"""

import subprocess
import sys
import time
from pathlib import Path

import httpx

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import utils.checkin_core as cc


def _r(ok=True, data=None, message='ok', status=200):
	body = {'success': ok, 'message': message, 'data': data or {}}
	return httpx.Response(status, json=body)


class TestPurity:
	"""中枢严禁反向导入签到分支/主流程（循环导入防线）。"""

	def test_import_does_not_pull_branch_modules(self):
		"""在干净子进程里导入 checkin_core，断言不连带加载分支/主流程。"""
		code = (
			'import sys; import utils.checkin_core; '
			"bad = [n for n in ('checkin', 'utils.gptgod', 'utils.guyscode', "
			"'utils.newapi_jwt', 'utils.newapi_session') if n in sys.modules]; "
			'print(",".join(bad))'
		)
		result = subprocess.run(
			[sys.executable, '-c', code],
			capture_output=True,
			text=True,
			cwd=project_root,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.strip() == ''


class TestIsAlreadyChecked:
	def test_matches_keywords(self):
		for msg in (
			'您今天已经签到过，请勿重复签到',
			'今日已签到',
			'重复签到',
			'already checked in',
			'Already Signed',
			'already claimed',
		):
			assert cc.is_already_checked(msg) is True

	def test_rejects_other_messages(self):
		assert cc.is_already_checked('签到失败') is False
		assert cc.is_already_checked('') is False
		assert cc.is_already_checked(None) is False


class TestQuotaAndFormat:
	def test_quota_to_currency_usd(self):
		assert cc.quota_to_currency(2_500_000) == 5.0

	def test_quota_to_currency_cny_rate(self):
		assert cc.quota_to_currency(2_500_000, rate=7.3) == 36.5

	def test_quota_to_currency_none_is_zero(self):
		assert cc.quota_to_currency(None) == 0.0

	def test_format_amount_units(self):
		assert cc.format_amount(12.5, 'usd') == '$12.50'
		assert cc.format_amount(12.5, 'cny') == '¥12.50'
		assert cc.format_amount(120.0, 'credits') == '120 积分'
		assert cc.format_amount(3.0, 'whatever') == '$3.00'

	def test_build_user_info_default_form(self):
		info = cc.build_user_info(5.0, 0.2)
		assert info['success'] is True
		assert 'unit' not in info
		assert '$5.0' in info['display']

	def test_build_user_info_unit_form(self):
		info = cc.build_user_info(5.0, 0.2, 'cny')
		assert info['unit'] == 'cny'
		assert '¥5.0' in info['display']

	def test_build_user_info_credits_form(self):
		info = cc.build_user_info(100, 0, 'credits')
		assert info['display'] == '💰 当前积分: 100'

	def test_failed_and_login_failed_info(self):
		info = cc.failed_info('查询失败', 'cny')
		assert info['success'] is False and info['unit'] == 'cny' and info['error'] == '查询失败'
		assert cc.login_failed_info('usd')['error'] == 'Login failed'


class TestParseCheckinResponse:
	def test_success(self):
		assert cc.parse_checkin_response(_r(True)) == (True, None)

	def test_failure_returns_message(self):
		assert cc.parse_checkin_response(_r(False, message='今日已签到')) == (False, '今日已签到')

	def test_non_json_response(self):
		resp = httpx.Response(502, text='<html>bad gateway</html>')
		ok, message = cc.parse_checkin_response(resp)
		assert ok is False and 'HTTP 502' in message

	def test_non_dict_json_does_not_crash(self):
		"""回归：合法 JSON 但为 list 时不得抛 AttributeError。"""
		resp = httpx.Response(200, json=[1, 2, 3])
		ok, message = cc.parse_checkin_response(resp)
		assert ok is False and 'HTTP 200' in message


class TestNewapiSelfToInfo:
	def test_success(self):
		info = cc.newapi_self_to_info(_r(True, {'quota': 2_500_000, 'used_quota': 100_000}), unit='usd')
		assert info['success'] is True and info['quota'] == 5.0 and info['used_quota'] == 0.2

	def test_success_cny_rate(self):
		info = cc.newapi_self_to_info(_r(True, {'quota': 2_500_000, 'used_quota': 0}), unit='cny', rate=7.3)
		assert info['quota'] == 36.5 and info['unit'] == 'cny'

	def test_failure_and_non_json(self):
		assert cc.newapi_self_to_info(_r(False))['success'] is False
		resp = httpx.Response(200, text='not json')
		assert cc.newapi_self_to_info(resp)['success'] is False


class TestNewapiLogin:
	DOMAIN = 'https://example.invalid'

	def test_success_returns_payload(self, monkeypatch):
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(True, {'access_token': 'tok'}))
		assert cc.newapi_login(None, self.DOMAIN, 'e', 'p', 'A') == {'access_token': 'tok'}

	def test_request_payload_shape(self, monkeypatch):
		seen = {}

		def fake_req(c, m, u, **k):
			seen.update(method=m, url=u, json=k.get('json'))
			return _r(True, {'id': 1})

		monkeypatch.setattr(cc, 'request_with_retry', fake_req)
		assert cc.newapi_login(None, self.DOMAIN, 'e@mail.com', 'pw', 'A') == {'id': 1}
		assert seen['method'] == 'POST'
		assert seen['url'] == f'{self.DOMAIN}/api/user/login'
		assert seen['json'] == {'username': 'e@mail.com', 'password': 'pw'}

	def test_failure_returns_none(self, monkeypatch):
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(False, message='bad credentials'))
		assert cc.newapi_login(None, self.DOMAIN, 'e', 'p', 'A') is None

	def test_non_json_returns_none(self, monkeypatch):
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: httpx.Response(502, text='<html/>'))
		assert cc.newapi_login(None, self.DOMAIN, 'e', 'p', 'A') is None

	def test_exception_returns_none(self, monkeypatch):
		def boom(*a, **k):
			raise RuntimeError('network down')

		monkeypatch.setattr(cc, 'request_with_retry', boom)
		assert cc.newapi_login(None, self.DOMAIN, 'e', 'p', 'A') is None

	def test_non_dict_data_payload_returns_empty(self, monkeypatch):
		monkeypatch.setattr(cc, 'request_with_retry', lambda c, m, u, **k: _r(True, [1, 2]))
		assert cc.newapi_login(None, self.DOMAIN, 'e', 'p', 'A') == {}


class TestRunStandardCheckin:
	"""标准流程编排：钩子契约与异常兜底（B1/B8 回归重点）。"""

	@staticmethod
	def _info(q=5.0, u=0.2, unit='usd'):
		return {'success': True, 'quota': q, 'used_quota': u, 'unit': unit, 'display': f'💰 当前余额: ${q}, 已用: ${u}'}

	def test_happy_path(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		order = []

		def fetch():
			order.append('fetch')
			return self._info(5.0) if len(order) == 2 else self._info(5.2)

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: order.append('auth') or True,
			fetch_user_info=fetch,
			perform_checkin=lambda: (order.append('checkin'), (True, None))[1],
		)
		assert ok is True
		assert before['quota'] == 5.0 and after['quota'] == 5.2
		assert order == ['auth', 'fetch', 'checkin', 'fetch']

	def test_login_failure_short_circuits(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		called = {'checkin': False}

		def perform():
			called['checkin'] = True
			return True, None

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: False,
			fetch_user_info=lambda: self._info(),
			perform_checkin=perform,
		)
		assert ok is False and after is None
		assert before['success'] is False and before['error'] == 'Login failed'
		assert called['checkin'] is False

	def test_before_fetch_failure_interrupts(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: cc.failed_info('获取用户信息失败: HTTP 500'),
			perform_checkin=lambda: (True, None),
		)
		assert ok is False and after is None and before['success'] is False

	def test_before_fetch_exception_is_contained(self, monkeypatch):
		"""B1 回归：fetch 抛异常不得穿透。"""
		monkeypatch.setattr(time, 'sleep', lambda s: None)

		def boom():
			raise RuntimeError('connection reset')

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=boom,
			perform_checkin=lambda: (True, None),
		)
		assert ok is False and after is None
		assert before['success'] is False

	def test_after_fetch_exception_keeps_success(self, monkeypatch):
		"""B1+B8 回归：签到成功后的第二次查询抛异常，结果仍是成功且沿用前值。"""
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		calls = {'n': 0}

		def fetch():
			calls['n'] += 1
			if calls['n'] == 1:
				return self._info(5.0)
			raise RuntimeError('timeout on second query')

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=fetch,
			perform_checkin=lambda: (True, None),
		)
		assert ok is True
		assert after['quota'] == before['quota'] == 5.0

	def test_after_fetch_failure_falls_back_to_before(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		calls = {'n': 0}

		def fetch():
			calls['n'] += 1
			return self._info(5.0) if calls['n'] == 1 else cc.failed_info('获取用户信息失败: HTTP 500')

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=fetch,
			perform_checkin=lambda: (True, None),
		)
		assert ok is True and after['quota'] == before['quota']

	def test_authenticate_exception_is_contained(self, monkeypatch):
		"""B1 回归：认证钩子抛异常按登录失败处理。"""
		monkeypatch.setattr(time, 'sleep', lambda s: None)

		def boom():
			raise RuntimeError('auth crashed')

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=boom,
			fetch_user_info=lambda: self._info(),
			perform_checkin=lambda: (True, None),
		)
		assert ok is False and before['error'] == 'Login failed'

	def test_already_checked_via_info_short_circuits(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		performed = {'n': 0}

		def perform():
			performed['n'] += 1
			return True, None

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			already_checked_via_info=lambda _b: True,
			perform_checkin=perform,
		)
		assert ok is True and performed['n'] == 0
		assert after['quota'] == before['quota']

	def test_already_checked_hook_exception_continues(self, monkeypatch):
		"""B1 回归：预判钩子抛异常时继续签到而不是中断。"""
		monkeypatch.setattr(time, 'sleep', lambda s: None)

		def boom(_b):
			raise RuntimeError('predictor crashed')

		ok, _, _ = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			already_checked_via_info=boom,
			perform_checkin=lambda: (True, None),
		)
		assert ok is True

	def test_perform_exception_fails(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)

		def boom():
			raise RuntimeError('5xx exhausted')

		ok, before, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			perform_checkin=boom,
		)
		assert ok is False and after is None and before['success'] is True

	def test_idempotent_message_counts_as_success(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		ok, _, _ = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			perform_checkin=lambda: (False, '您今天已经签到过，请勿重复签到'),
		)
		assert ok is True

	def test_failure_message_fails(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		ok, _, after = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			perform_checkin=lambda: (False, '余额不足'),
		)
		assert ok is False and after is None

	def test_post_checkin_receives_both_infos(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)
		calls = {'n': 0}
		received = {}

		def fetch():
			calls['n'] += 1
			return self._info(5.0 if calls['n'] == 1 else 5.2)

		def post(before, after):
			received['b'] = before['quota']
			received['a'] = after['quota']

		cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=fetch,
			perform_checkin=lambda: (True, None),
			post_checkin=post,
		)
		assert received == {'b': 5.0, 'a': 5.2}

	def test_post_checkin_exception_does_not_flip_result(self, monkeypatch):
		monkeypatch.setattr(time, 'sleep', lambda s: None)

		def boom(_b, _a):
			raise RuntimeError('verify crashed')

		ok, _, _ = cc.run_standard_checkin(
			'A',
			authenticate=lambda: True,
			fetch_user_info=lambda: self._info(),
			perform_checkin=lambda: (True, None),
			post_checkin=boom,
		)
		assert ok is True
