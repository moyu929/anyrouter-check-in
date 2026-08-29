import json

import pytest

from utils.config import AppConfig, ProviderConfig


def test_builtin_provider_profile_persistence_defaults(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is True
	assert config.providers['agentrouter'].persist_profile is False


def test_provider_profile_persistence_can_override_builtin(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps(
			{
				'anyrouter': {'domain': 'https://anyrouter.top', 'persist_profile': False},
				'agentrouter': {'domain': 'https://agentrouter.org', 'persist_profile': True},
			}
		),
	)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is False
	assert config.providers['agentrouter'].persist_profile is True


def test_builtin_gorouter_uses_autocheckin_and_oauth(monkeypatch):
	"""gorouter 与 agentrouter 同构：OAuth 登录 + 登录时自动签到（sign_in_path=None）。"""
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	gorouter = config.providers['gorouter']
	assert gorouter.domain == 'https://gorouter.app'
	assert gorouter.sign_in_path is None  # 自动签到，绕过 Turnstile 手动签到接口
	assert gorouter.needs_manual_check_in() is False
	assert gorouter.is_oauth() is True
	assert gorouter.oauth_client_id == 'Ov23lipc1Ups6bRqeQYE'


def test_builtin_cun_uses_oauth_and_manual_checkin(monkeypatch):
	"""cun 与 agentrouter/gorouter 同为 GitHub OAuth，但需主动调 /api/user/checkin 签到。"""
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	cun = config.providers['cun']
	assert cun.domain == 'https://www.cun.ai'
	assert cun.sign_in_path == '/api/user/checkin'
	assert cun.needs_manual_check_in() is True
	assert cun.is_oauth() is True
	assert cun.oauth_client_id == 'Ov23lipURdGRYDGN2jII'
	assert cun.user_info_path == '/api/user/self'
	assert cun.api_user_key == 'new-api-user'
	assert cun.use_proxy is True  # 站点大陆无法直连
	assert cun.persist_profile is False


def test_custom_provider_profile_persistence_defaults_to_false(monkeypatch):
	monkeypatch.setenv('PROVIDERS', json.dumps({'custom': {'domain': 'https://custom.example.com'}}))

	config = AppConfig.load_from_env()

	assert config.providers['custom'].persist_profile is False


def test_provider_from_dict_inherits_profile_persistence_from_defaults():
	defaults = ProviderConfig(name='custom', domain='https://old.example.com', persist_profile=True)

	provider = ProviderConfig.from_dict(
		'custom',
		{'domain': 'https://new.example.com'},
		defaults=defaults,
	)

	assert provider.persist_profile is True


def test_provider_from_dict_inherits_direct_fallback_from_defaults():
	defaults = ProviderConfig(name='custom', domain='https://old.example.com', allow_direct_fallback=False)

	provider = ProviderConfig.from_dict('custom', {'domain': 'https://new.example.com'}, defaults=defaults)

	assert provider.allow_direct_fallback is False


def test_provider_direct_fallback_can_be_overridden(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps({'agentrouter': {'allow_direct_fallback': False}}),
	)

	config = AppConfig.load_from_env()

	assert config.providers['agentrouter'].allow_direct_fallback is False


def test_provider_from_dict_inherits_domain_from_defaults():
	"""PROVIDERS 仅覆盖 use_proxy（缺 domain）时应从内置默认继承 domain，不再整个跳过。"""
	defaults = ProviderConfig(name='gptgod', domain='https://gptgod.online', use_proxy=False)

	provider = ProviderConfig.from_dict('gptgod', {'use_proxy': True}, defaults=defaults)

	assert provider.domain == 'https://gptgod.online'
	assert provider.use_proxy is True


def test_provider_from_dict_without_domain_and_defaults_fails():
	"""全新自定义 provider 既无 domain 又无内置默认时，仍应报错。"""
	with pytest.raises(ValueError):
		ProviderConfig.from_dict('custom', {'use_proxy': True})


def test_load_from_env_partial_override_inherits_builtin(monkeypatch):
	"""用户真实场景：PROVIDERS 只写 use_proxy，覆盖内置 gptgod/lyclaude 的代理开关。"""
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps({'gptgod': {'use_proxy': True}, 'lyclaude': {'use_proxy': True}}),
	)

	config = AppConfig.load_from_env()

	assert config.providers['gptgod'].use_proxy is True
	assert config.providers['gptgod'].domain == 'https://gptgod.online'
	assert config.providers['lyclaude'].use_proxy is True
	assert config.providers['lyclaude'].domain == 'https://free.lyclaude.site'


class TestWildcardProviderConfig:
	"""通配符 "*"：一键控制全部提供商代理开关（优先级：内置默认 < "*" < 具体条目）。"""

	def test_wildcard_enables_proxy_for_all_builtin_providers(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', json.dumps({'*': {'use_proxy': True}}))

		config = AppConfig.load_from_env()

		# 内置默认直连的站点被通配符打开，内置默认走代理的保持开启
		assert config.providers['gptgod'].use_proxy is True
		assert config.providers['lyclaude'].use_proxy is True
		assert config.providers['hcnsec'].use_proxy is True
		assert config.providers['agentrouter'].use_proxy is True

	def test_wildcard_disables_proxy_for_all_builtin_providers(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', json.dumps({'*': {'use_proxy': False}}))

		config = AppConfig.load_from_env()

		# 内置默认走代理的 agentrouter/gorouter/cun 也被通配符关闭
		assert config.providers['agentrouter'].use_proxy is False
		assert config.providers['gorouter'].use_proxy is False
		assert config.providers['cun'].use_proxy is False

	def test_specific_entry_overrides_wildcard(self, monkeypatch):
		monkeypatch.setenv(
			'PROVIDERS',
			json.dumps({'*': {'use_proxy': True}, 'gptgod': {'use_proxy': False}}),
		)

		config = AppConfig.load_from_env()

		assert config.providers['gptgod'].use_proxy is False
		assert config.providers['lyclaude'].use_proxy is True

	def test_wildcard_keeps_builtin_domain(self, monkeypatch):
		"""通配符仅写 use_proxy 时，domain 等其余字段从内置默认继承。"""
		monkeypatch.setenv('PROVIDERS', json.dumps({'*': {'use_proxy': True}}))

		config = AppConfig.load_from_env()

		assert config.providers['gptgod'].domain == 'https://gptgod.online'
		assert config.providers['agentrouter'].domain == 'https://agentrouter.org'

	def test_empty_specific_entry_inherits_wildcard_fields(self, monkeypatch):
		"""具体条目为空对象时继承通配符字段（通配铺底、具体覆盖的合并语义）。"""
		monkeypatch.setenv(
			'PROVIDERS',
			json.dumps({'*': {'use_proxy': True}, 'hcnsec': {}}),
		)

		config = AppConfig.load_from_env()

		assert config.providers['hcnsec'].use_proxy is True
		assert config.providers['hcnsec'].domain == 'https://api.hcnsec.cn'

	def test_non_dict_wildcard_is_ignored(self, monkeypatch):
		"""非法通配符（非 JSON 对象）被忽略，仅用内置默认。"""
		monkeypatch.setenv('PROVIDERS', json.dumps({'*': True}))

		config = AppConfig.load_from_env()

		assert config.providers['gptgod'].use_proxy is False
		assert config.providers['agentrouter'].use_proxy is True
