"""main() 汇总与通知去重测试（完全 mock 掉签到调用，绝不发起真实请求）。"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin as checkin_module
from checkin import generate_balance_hash, load_balance_hash, main, save_balance_hash


def _usd(quota: float, used: float) -> dict:
	return {
		'success': True,
		'quota': quota,
		'used_quota': used,
		'display': f':money: Current balance: ${quota}, Used: ${used}',
	}


class _NotifySpy:
	def __init__(self):
		self.messages: list[tuple[str, str]] = []

	def push_message(self, title: str, content: str, msg_type: str = 'text') -> None:
		self.messages.append((title, content))

	@property
	def body(self) -> str:
		return self.messages[-1][1] if self.messages else ''


@pytest.fixture
def harness(monkeypatch, tmp_path):
	"""隔离余额哈希文件、拦截通知，并用假 check_in_account 替换真实签到。"""
	monkeypatch.setattr(checkin_module, 'BALANCE_HASH_FILE', str(tmp_path / 'balance_hash.txt'))
	monkeypatch.setattr(checkin_module, 'take_pending_screenshots', lambda: [])
	spy = _NotifySpy()
	monkeypatch.setattr(checkin_module.notify, 'push_message', spy.push_message)

	def setup(accounts: list, results: list):
		monkeypatch.setattr(checkin_module, 'load_accounts_config', lambda: accounts)

		async def fake_check_in_account(account, index, app_config):
			return results[index]

		monkeypatch.setattr(checkin_module, 'check_in_account', fake_check_in_account)
		return spy

	return setup


def _account(name: str):
	from utils.config import AccountConfig

	return AccountConfig(cookies={'session': 'abc'}, api_user='1', name=name)


class TestBalanceHashPersistence:
	def test_round_trip(self, monkeypatch, tmp_path):
		monkeypatch.setattr(checkin_module, 'BALANCE_HASH_FILE', str(tmp_path / 'h.txt'))

		assert load_balance_hash() is None

		save_balance_hash('deadbeef')

		assert load_balance_hash() == 'deadbeef'

	def test_unwritable_path_does_not_raise(self, monkeypatch, tmp_path):
		monkeypatch.setattr(checkin_module, 'BALANCE_HASH_FILE', str(tmp_path / 'missing-dir' / 'h.txt'))

		save_balance_hash('deadbeef')

		assert load_balance_hash() is None

	def test_hash_is_16_hex_chars(self):
		value = generate_balance_hash({'account_1': {'quota': 1.0, 'used': 0.0}})

		assert len(value) == 16
		assert all(c in '0123456789abcdef' for c in value)

	def test_empty_balances_hash_is_stable(self):
		assert generate_balance_hash({}) == generate_balance_hash(None)


class TestMainNotification:
	async def test_first_run_notifies_with_balance_detail(self, harness):
		spy = harness(
			[_account('主号')],
			[(True, _usd(100.0, 20.0), _usd(125.0, 20.0))],
		)

		with pytest.raises(SystemExit) as exc:
			await main()

		assert exc.value.code == 0
		assert '首次' not in spy.body  # 日志用语不进通知正文
		assert '[CHECK-IN] 主号' in spy.body
		assert '签到获得: +$25.00' in spy.body
		assert 'Success: 1/1' in spy.body

	async def test_second_run_without_change_skips_notification(self, harness):
		accounts = [_account('主号')]
		result = [(True, _usd(100.0, 20.0), _usd(100.0, 20.0))]

		spy = harness(accounts, result)
		with pytest.raises(SystemExit):
			await main()
		assert len(spy.messages) == 1

		# 第二次运行余额未变，且账号全部成功 → 不再通知（spy 为同一实例，比较增量）
		harness(accounts, result)
		with pytest.raises(SystemExit):
			await main()

		assert len(spy.messages) == 1

	async def test_failure_always_notifies(self, harness):
		spy = harness([_account('主号')], [(False, None, None)])

		with pytest.raises(SystemExit) as exc:
			await main()

		assert exc.value.code == 1
		assert '[FAIL] 主号' in spy.body
		assert 'Failed: 1/1' in spy.body

	async def test_similar_account_names_are_not_deduplicated(self, harness):
		"""account_1 不应被 account_11 的条目吞掉 —— 精确 key 判重回归测试。"""
		accounts = [_account(f'账号{i + 1}') for i in range(11)]
		results = [(True, _usd(100.0, 0.0), _usd(105.0, 0.0)) for _ in range(11)]

		spy = harness(accounts, results)
		with pytest.raises(SystemExit):
			await main()

		for i in range(11):
			assert f'[CHECK-IN] 账号{i + 1}' in spy.body

	async def test_account_is_not_reported_twice(self, harness):
		"""失败账号已在明细区列出时，不应再追加一条签到详情。"""
		spy = harness([_account('主号')], [(False, _usd(100.0, 20.0), _usd(125.0, 20.0))])

		with pytest.raises(SystemExit):
			await main()

		assert spy.body.count('主号') == 1

	async def test_credits_provider_renders_credit_unit(self, harness):
		before = {'success': True, 'quota': 100, 'used_quota': 0, 'unit': 'credits', 'display': 'tokens: 100'}
		after = {'success': True, 'quota': 150, 'used_quota': 0, 'unit': 'credits', 'display': 'tokens: 150'}

		spy = harness([_account('gptgod 号')], [(True, before, after)])

		with pytest.raises(SystemExit):
			await main()

		assert '签到获得: +50 积分' in spy.body
		assert '$' not in spy.body

	async def test_exception_in_account_is_captured(self, harness, monkeypatch):
		harness([_account('主号')], [])

		async def boom(account, index, app_config):
			raise RuntimeError('browser crashed')

		monkeypatch.setattr(checkin_module, 'check_in_account', boom)
		spy = _NotifySpy()
		monkeypatch.setattr(checkin_module.notify, 'push_message', spy.push_message)

		with pytest.raises(SystemExit) as exc:
			await main()

		assert exc.value.code == 1
		assert 'exception' in spy.body

	async def test_missing_accounts_config_exits_with_error(self, monkeypatch, tmp_path):
		monkeypatch.setattr(checkin_module, 'BALANCE_HASH_FILE', str(tmp_path / 'h.txt'))
		monkeypatch.setattr(checkin_module, 'load_accounts_config', lambda: None)
		spy = _NotifySpy()
		monkeypatch.setattr(checkin_module.notify, 'push_message', spy.push_message)

		with pytest.raises(SystemExit) as exc:
			await main()

		assert exc.value.code == 1
		assert '无法加载账号配置' in spy.body

	async def test_partial_success_exits_zero(self, harness):
		spy = harness(
			[_account('甲'), _account('乙')],
			[(True, _usd(100.0, 0.0), _usd(105.0, 0.0)), (False, None, None)],
		)

		with pytest.raises(SystemExit) as exc:
			await main()

		assert exc.value.code == 0
		assert 'Success: 1/2' in spy.body
		assert 'Some accounts check-in successful' in spy.body
