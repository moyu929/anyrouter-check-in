"""统一日志模块 — 所有签到分支共享的日志输出。

日志级别（由 DEBUG_MODE 环境变量控制，支持 true/1/yes）：
  - 普通模式：仅输出 INFO / SUCCESS / WARN / FAILED，简洁
  - 调试模式：额外输出 DEBUG 级别，含网络请求、响应体、截图路径等详细日志

用法:
  from utils.debug import log
  log.info('开始处理 lyclaude')
  log.debug('响应体: {...}')
  log.success('签到成功，余额 $200')
"""

from __future__ import annotations

import os


def _is_debug() -> bool:
	"""是否开启调试模式。"""
	raw = os.getenv('DEBUG_MODE', '').strip().lower()
	return raw in {'1', 'true', 'yes', 'on'}


class _Log:
	"""统一日志输出，自动处理前缀和调试模式开关。"""

	@staticmethod
	def info(msg: str) -> None:
		print(f'[信息] {msg}')

	@staticmethod
	def success(msg: str) -> None:
		print(f'[成功] {msg}')

	@staticmethod
	def warn(msg: str) -> None:
		print(f'[警告] {msg}')

	@staticmethod
	def failed(msg: str) -> None:
		print(f'[失败] {msg}')

	@staticmethod
	def debug(msg: str) -> None:
		"""仅在调试模式下输出。"""
		if _is_debug():
			print(f'[调试] {msg}')

	@staticmethod
	def detail(msg: str) -> None:
		"""详细流程日志（登录/浏览器/代理内部步骤），仅在调试模式下输出。

		与 debug 等价，但语义上代表"面向开发者的流程细节"，
		普通用户模式下不显示，避免日志繁杂难懂。
		"""
		if _is_debug():
			print(f'[调试] {msg}')

	@staticmethod
	def notify(msg: str) -> None:
		"""通知相关日志（始终输出）。"""
		print(f'[通知] {msg}')

	@staticmethod
	def stats(msg: str) -> None:
		"""统计汇总日志（始终输出）。"""
		print(f'[统计] {msg}')


log = _Log()


def is_debug_enabled() -> bool:
	return _is_debug()


def debug_print(message: str) -> None:
	"""兼容旧版 debug_print 接口。"""
	if _is_debug():
		print(message)
