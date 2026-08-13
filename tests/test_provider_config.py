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
