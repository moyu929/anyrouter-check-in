#!/usr/bin/env python3
"""
配置管理模块
"""

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Literal


@dataclass
class ProviderConfig:
	"""Provider 配置"""

	name: str
	domain: str
	login_path: str = '/login'
	sign_in_path: str | None = '/api/user/sign_in'
	user_info_path: str = '/api/user/self'
	api_user_key: str | None = 'new-api-user'
	bypass_method: Literal['waf_cookies'] | None = None
	waf_cookie_names: List[str] | None = None
	use_proxy: bool = False
	persist_profile: bool = False
	auth_method: Literal['email', 'oauth', 'gptgod'] | None = None
	oauth_client_id: str | None = None
	oauth_state_path: str = '/api/oauth/state'
	oauth_callback_path: str = '/api/oauth/github'

	def __post_init__(self):
		required_waf_cookies: list[str] = []
		if self.waf_cookie_names and isinstance(self.waf_cookie_names, list):
			seen: set[str] = set()
			for item in self.waf_cookie_names:
				name = '' if not item or not isinstance(item, str) else item.strip()
				if not name:
					print(f'[警告] 发现非法的 WAF cookie 名称: {item}')
					continue
				if name not in seen:
					seen.add(name)
					required_waf_cookies.append(name)

		if not required_waf_cookies:
			self.bypass_method = None

		self.waf_cookie_names = required_waf_cookies

	@classmethod
	def from_dict(cls, name: str, data: dict, *, defaults: 'ProviderConfig | None' = None) -> 'ProviderConfig':
		"""从字典创建 ProviderConfig

		配置格式:
		- 基础: {"domain": "https://example.com"}
		- 完整: {"domain": "https://example.com", "login_path": "/login", "use_proxy": true, ...}
		"""
		default_use_proxy = defaults.use_proxy if defaults else False
		default_persist_profile = defaults.persist_profile if defaults else False
		return cls(
			name=name,
			domain=data['domain'],
			login_path=data.get('login_path', defaults.login_path if defaults else '/login'),
			sign_in_path=data.get('sign_in_path', defaults.sign_in_path if defaults else '/api/user/sign_in'),
			user_info_path=data.get('user_info_path', defaults.user_info_path if defaults else '/api/user/self'),
			api_user_key=data.get('api_user_key', defaults.api_user_key if defaults else 'new-api-user'),
			bypass_method=data.get('bypass_method', defaults.bypass_method if defaults else None),
			waf_cookie_names=data.get('waf_cookie_names', defaults.waf_cookie_names if defaults else None),
			use_proxy=data.get('use_proxy', default_use_proxy),
			persist_profile=data.get('persist_profile', default_persist_profile),
			auth_method=data.get('auth_method', defaults.auth_method if defaults else None),
			oauth_client_id=data.get('oauth_client_id', defaults.oauth_client_id if defaults else None),
			oauth_state_path=data.get(
				'oauth_state_path', defaults.oauth_state_path if defaults else '/api/oauth/state'
			),
			oauth_callback_path=data.get(
				'oauth_callback_path', defaults.oauth_callback_path if defaults else '/api/oauth/github'
			),
		)

	def needs_waf_cookies(self) -> bool:
		"""判断是否需要获取 WAF cookies"""
		return self.bypass_method == 'waf_cookies'

	def needs_manual_check_in(self) -> bool:
		"""判断是否需要手动调用签到接口"""
		return self.sign_in_path is not None

	def is_oauth(self) -> bool:
		"""判断是否使用 OAuth 登录"""
		return self.auth_method == 'oauth'


@dataclass
class AppConfig:
	"""应用配置"""

	providers: Dict[str, ProviderConfig]

	@classmethod
	def load_from_env(cls) -> 'AppConfig':
		"""从环境变量加载配置"""
		providers = {
			'anyrouter': ProviderConfig(
				name='anyrouter',
				domain='https://anyrouter.top',
				login_path='/login',
				sign_in_path='/api/user/sign_in',
				user_info_path='/api/user/self',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'],
				use_proxy=False,
				persist_profile=True,
			),
			'agentrouter': ProviderConfig(
				name='agentrouter',
				domain='https://agentrouter.org',
				login_path='/login',
				sign_in_path=None,
				user_info_path='/api/user/self',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc'],
				use_proxy=True,
				persist_profile=False,
				auth_method='oauth',
				oauth_client_id='Ov23lidtiR4LeVZvVRNL',
			),
			'lyclaude': ProviderConfig(
				name='lyclaude',
				domain='https://free.lyclaude.site',
				login_path='/login',
				sign_in_path='/api/user/checkin',
				user_info_path='/api/user/self',
				api_user_key='new-api-user',
			),
			'gptgod': ProviderConfig(
				name='gptgod',
				domain='https://gptgod.online',
				login_path='/login',
				sign_in_path='/api/user/checkin',
				user_info_path='/api/user/info',
				api_user_key=None,
				auth_method='gptgod',
			),
		}

		# 尝试从环境变量加载自定义 providers
		providers_str = os.getenv('PROVIDERS')
		if providers_str:
			try:
				providers_data = json.loads(providers_str)

				if not isinstance(providers_data, dict):
					print('[警告] PROVIDERS 必须是 JSON 对象，已忽略自定义提供商')
					return cls(providers=providers)

				# 解析自定义 providers,会覆盖默认配置
				for name, provider_data in providers_data.items():
					try:
						providers[name] = ProviderConfig.from_dict(
							name,
							provider_data,
							defaults=providers.get(name),
						)
					except Exception as e:
						print(f'[警告] 解析提供商 "{name}" 失败: {e}，已跳过')
						continue

				print(f'[信息] 已从 PROVIDERS 环境变量加载 {len(providers_data)} 个自定义提供商')
			except json.JSONDecodeError as e:
				print(f'[警告] PROVIDERS 环境变量解析失败: {e}，仅使用默认配置')
			except Exception as e:
				print(f'[警告] 加载 PROVIDERS 出错: {e}，仅使用默认配置')

		return cls(providers=providers)

	def get_provider(self, name: str) -> ProviderConfig | None:
		"""获取指定 provider 配置"""
		return self.providers.get(name)


@dataclass
class AccountConfig:
	"""账号配置"""

	cookies: dict | str | None
	api_user: str | None = None
	provider: str = 'anyrouter'
	name: str | None = None
	email: str | None = None
	password: str | None = None
	github_session: str | None = None

	@classmethod
	def from_dict(cls, data: dict, index: int) -> 'AccountConfig':
		"""从字典创建 AccountConfig"""
		provider = data.get('provider', 'anyrouter')
		name = data.get('name', f'Account {index + 1}')

		return cls(
			cookies=data.get('cookies'),
			api_user=data.get('api_user'),
			provider=provider,
			name=name if name else None,
			email=data.get('email'),
			password=data.get('password'),
			github_session=data.get('github_session'),
		)

	def has_login_credentials(self) -> bool:
		"""是否配置了邮箱密码登录"""
		return bool(self.email and self.password)

	def has_oauth_credentials(self) -> bool:
		"""是否配置了 OAuth 凭据（如 GitHub session）"""
		return bool(self.github_session)

	def get_display_name(self, index: int) -> str:
		"""获取显示名称"""
		return self.name if self.name else f'Account {index + 1}'


def load_accounts_config() -> list[AccountConfig] | None:
	"""从环境变量加载账号配置"""
	accounts_str = os.getenv('ANYROUTER_ACCOUNTS')
	if not accounts_str:
		print('错误: 未找到 ANYROUTER_ACCOUNTS 环境变量')
		return None

	try:
		accounts_data = json.loads(accounts_str)
	except json.JSONDecodeError as e:
		print(f'错误: ANYROUTER_ACCOUNTS JSON 解析失败: {e}')
		print('提示: 常见原因 - 末尾多余逗号、使用了单引号、包含注释、或换行格式问题')
		return None

	try:
		if not isinstance(accounts_data, list):
			print('错误: 账号配置必须使用数组格式 [{}]')
			return None

		accounts = []
		for i, account_dict in enumerate(accounts_data):
			if not isinstance(account_dict, dict):
				print(f'错误: 账号 {i + 1} 配置格式不正确')
				return None

			has_oauth = bool(account_dict.get('github_session'))
			if 'api_user' not in account_dict:
				has_login = account_dict.get('email') and account_dict.get('password')
				if not has_login and not has_oauth:
					print(f'错误: 账号 {i + 1} 缺少必填字段 (api_user) - 仅邮箱密码或 github_session 登录可省略它')
					return None

			has_cookies = 'cookies' in account_dict and account_dict['cookies']
			has_login = account_dict.get('email') and account_dict.get('password')

			if not has_cookies and not has_login and not has_oauth:
				print(f'错误: 账号 {i + 1} 必须配置 cookies、邮箱密码 或 github_session 之一')
				return None

			if 'name' in account_dict and not account_dict['name']:
				print(f'错误: 账号 {i + 1} 的 name 字段不能为空')
				return None

			accounts.append(AccountConfig.from_dict(account_dict, i))

		return accounts
	except Exception as e:
		print(f'错误: 账号配置格式不正确: {e}')
		return None
