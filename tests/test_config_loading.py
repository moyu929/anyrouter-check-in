"""账号配置解析与 WAF cookie 校验测试（全离线）。"""

import json

from utils.config import AccountConfig, AppConfig, ProviderConfig, load_accounts_config


class TestProviderPostInit:
	def test_blank_and_none_cookie_names_are_dropped(self):
		provider = ProviderConfig(
			name='demo',
			domain='https://demo.example.com',
			bypass_method='waf_cookies',
			waf_cookie_names=['acw_tc', '  ', None, 'cdn_sec_tc'],  # type: ignore[list-item]
		)

		assert sorted(provider.waf_cookie_names or []) == ['acw_tc', 'cdn_sec_tc']
		assert provider.needs_waf_cookies() is True

	def test_duplicate_cookie_names_are_deduplicated(self):
		provider = ProviderConfig(
			name='demo',
			domain='https://demo.example.com',
			bypass_method='waf_cookies',
			waf_cookie_names=['acw_tc', 'acw_tc', ' acw_tc '],
		)

		assert provider.waf_cookie_names == ['acw_tc']

	def test_bypass_disabled_when_no_valid_cookie_names(self):
		provider = ProviderConfig(
			name='demo',
			domain='https://demo.example.com',
			bypass_method='waf_cookies',
			waf_cookie_names=['', '   '],
		)

		assert provider.bypass_method is None
		assert provider.needs_waf_cookies() is False

	def test_needs_manual_check_in_follows_sign_in_path(self):
		with_path = ProviderConfig(name='a', domain='https://a.example.com', sign_in_path='/api/user/sign_in')
		without_path = ProviderConfig(name='b', domain='https://b.example.com', sign_in_path=None)

		assert with_path.needs_manual_check_in() is True
		assert without_path.needs_manual_check_in() is False

	def test_is_oauth_only_for_oauth_auth_method(self):
		assert ProviderConfig(name='a', domain='https://a.example.com', auth_method='oauth').is_oauth() is True
		assert ProviderConfig(name='b', domain='https://b.example.com', auth_method='gptgod').is_oauth() is False
		assert ProviderConfig(name='c', domain='https://c.example.com').is_oauth() is False


class TestAppConfigFromEnv:
	def test_builtin_providers_are_present(self, monkeypatch):
		monkeypatch.delenv('PROVIDERS', raising=False)

		config = AppConfig.load_from_env()

		assert set(config.providers) >= {'anyrouter', 'agentrouter', 'gptgod'}
		assert config.providers['agentrouter'].use_proxy is True
		assert config.providers['anyrouter'].use_proxy is False
		assert config.providers['gptgod'].use_proxy is False

	def test_gptgod_has_no_api_user_key(self, monkeypatch):
		monkeypatch.delenv('PROVIDERS', raising=False)

		assert AppConfig.load_from_env().providers['gptgod'].api_user_key is None

	def test_non_object_providers_env_is_ignored(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', json.dumps(['not', 'an', 'object']))

		config = AppConfig.load_from_env()

		assert 'anyrouter' in config.providers

	def test_malformed_json_falls_back_to_builtins(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', '{invalid json')

		config = AppConfig.load_from_env()

		assert 'anyrouter' in config.providers

	def test_provider_missing_domain_is_skipped(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', json.dumps({'broken': {'login_path': '/login'}}))

		config = AppConfig.load_from_env()

		assert 'broken' not in config.providers

	def test_custom_provider_inherits_builtin_use_proxy(self, monkeypatch):
		monkeypatch.setenv('PROVIDERS', json.dumps({'agentrouter': {'domain': 'https://mirror.example.com'}}))

		provider = AppConfig.load_from_env().providers['agentrouter']

		assert provider.domain == 'https://mirror.example.com'
		assert provider.use_proxy is True

	def test_get_provider_returns_none_for_unknown_name(self, monkeypatch):
		monkeypatch.delenv('PROVIDERS', raising=False)

		assert AppConfig.load_from_env().get_provider('nope') is None


class TestLoadAccountsConfig:
	def test_missing_env_returns_none(self, monkeypatch):
		monkeypatch.delenv('ANYROUTER_ACCOUNTS', raising=False)

		assert load_accounts_config() is None

	def test_malformed_json_returns_none(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', '[{"api_user": "1",}]')

		assert load_accounts_config() is None

	def test_non_array_returns_none(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps({'api_user': '1'}))

		assert load_accounts_config() is None

	def test_cookies_plus_api_user_is_accepted(self, monkeypatch):
		monkeypatch.setenv(
			'ANYROUTER_ACCOUNTS',
			json.dumps([{'api_user': '42', 'cookies': {'session': 'abc'}}]),
		)

		accounts = load_accounts_config()

		assert accounts is not None
		assert accounts[0].api_user == '42'
		assert accounts[0].provider == 'anyrouter'
		assert accounts[0].get_display_name(0) == 'Account 1'

	def test_email_password_may_omit_api_user(self, monkeypatch):
		monkeypatch.setenv(
			'ANYROUTER_ACCOUNTS',
			json.dumps([{'email': 'a@example.com', 'password': 'pw', 'provider': 'gptgod'}]),
		)

		accounts = load_accounts_config()

		assert accounts is not None
		assert accounts[0].has_login_credentials() is True
		assert accounts[0].provider == 'gptgod'

	def test_github_session_may_omit_api_user(self, monkeypatch):
		monkeypatch.setenv(
			'ANYROUTER_ACCOUNTS',
			json.dumps([{'github_session': 'sess', 'provider': 'agentrouter'}]),
		)

		accounts = load_accounts_config()

		assert accounts is not None
		assert accounts[0].has_oauth_credentials() is True

	def test_empty_github_session_is_rejected(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps([{'github_session': ''}]))

		assert load_accounts_config() is None

	def test_api_user_without_any_credential_is_rejected(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps([{'api_user': '42'}]))

		assert load_accounts_config() is None

	def test_missing_api_user_and_credentials_is_rejected(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps([{'provider': 'anyrouter'}]))

		assert load_accounts_config() is None

	def test_empty_name_is_rejected(self, monkeypatch):
		monkeypatch.setenv(
			'ANYROUTER_ACCOUNTS',
			json.dumps([{'api_user': '42', 'cookies': 'session=abc', 'name': ''}]),
		)

		assert load_accounts_config() is None

	def test_non_object_entry_is_rejected(self, monkeypatch):
		monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps(['just-a-string']))

		assert load_accounts_config() is None

	def test_custom_name_is_preserved(self, monkeypatch):
		monkeypatch.setenv(
			'ANYROUTER_ACCOUNTS',
			json.dumps([{'api_user': '42', 'cookies': 'session=abc', 'name': '主号'}]),
		)

		accounts = load_accounts_config()

		assert accounts is not None
		assert accounts[0].get_display_name(0) == '主号'


class TestAccountConfigDefaults:
	def test_display_name_falls_back_to_index(self):
		account = AccountConfig(cookies={'session': 'abc'}, api_user='1', name=None)

		assert account.get_display_name(2) == 'Account 3'

	def test_no_credentials_flags(self):
		account = AccountConfig(cookies={'session': 'abc'}, api_user='1')

		assert account.has_login_credentials() is False
		assert account.has_oauth_credentials() is False

	def test_email_without_password_is_not_login_credentials(self):
		account = AccountConfig(cookies=None, api_user='1', email='a@example.com')

		assert account.has_login_credentials() is False
