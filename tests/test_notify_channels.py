"""通知渠道补充测试（Telegram / Bark / Server 酱 / Gotify 边界，全部本地 mock）。"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from utils.notify import NotificationKit


@pytest.fixture
def kit(monkeypatch):
	for name in (
		'EMAIL_USER',
		'EMAIL_PASS',
		'EMAIL_TO',
		'EMAIL_SENDER',
		'CUSTOM_SMTP_SERVER',
		'PUSHPLUS_TOKEN',
		'SERVERPUSHKEY',
		'DINGDING_WEBHOOK',
		'FEISHU_WEBHOOK',
		'WEIXIN_WEBHOOK',
		'GOTIFY_URL',
		'GOTIFY_TOKEN',
		'GOTIFY_PRIORITY',
		'TELEGRAM_BOT_TOKEN',
		'TELEGRAM_CHAT_ID',
		'BARK_KEY',
		'BARK_SERVER',
		'NOTIFYX_KEY',
		'NOTIFYX_TEAM',
	):
		monkeypatch.delenv(name, raising=False)
	return NotificationKit


@pytest.fixture
def post_spy():
	"""拦截 httpx.Client().post，返回 (spy, 可调整的响应容器)。"""
	holder = {'response': httpx.Response(200, json={'code': 0})}

	with patch('httpx.Client') as mock_client_class:
		client = MagicMock()
		client.post.side_effect = lambda *_a, **_kw: holder['response']
		mock_client_class.return_value.__enter__.return_value = client
		yield client, holder


class TestTelegram:
	def test_posts_html_message(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'bot123')
		monkeypatch.setenv('TELEGRAM_CHAT_ID', 'chat456')
		client, _ = post_spy

		kit().send_telegram('标题', '正文')

		url, kwargs = client.post.call_args[0][0], client.post.call_args[1]
		assert url == 'https://api.telegram.org/botbot123/sendMessage'
		assert kwargs['json']['chat_id'] == 'chat456'
		assert kwargs['json']['parse_mode'] == 'HTML'
		assert '<b>标题</b>' in kwargs['json']['text']

	def test_missing_chat_id_raises(self, kit, monkeypatch):
		monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'bot123')

		with pytest.raises(ValueError, match='Telegram Bot Token or Chat ID not configured'):
			kit().send_telegram('标题', '正文')

	def test_ok_false_payload_raises(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'bot123')
		monkeypatch.setenv('TELEGRAM_CHAT_ID', 'chat456')
		_client, holder = post_spy
		holder['response'] = httpx.Response(200, json={'ok': False, 'description': 'chat not found'})

		with pytest.raises(RuntimeError, match='chat not found'):
			kit().send_telegram('标题', '正文')


class TestBark:
	def test_default_server_and_payload(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('BARK_KEY', 'devicekey')
		client, _ = post_spy

		kit().send_bark('标题', '正文')

		url, kwargs = client.post.call_args[0][0], client.post.call_args[1]
		assert url == 'https://api.day.app/push'
		assert kwargs['json']['device_key'] == 'devicekey'
		assert kwargs['json']['body'] == '正文'

	def test_custom_server_trailing_slash_is_stripped(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('BARK_KEY', 'devicekey')
		monkeypatch.setenv('BARK_SERVER', 'https://bark.example.com/')
		client, _ = post_spy

		kit().send_bark('标题', '正文')

		assert client.post.call_args[0][0] == 'https://bark.example.com/push'

	def test_missing_key_raises(self, kit):
		with pytest.raises(ValueError, match='Bark Key not configured'):
			kit().send_bark('标题', '正文')


class TestNotifyX:
	def test_posts_title_and_content(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('NOTIFYX_KEY', 'notifyxkey')
		client, _ = post_spy

		kit().send_notifyx('标题', '正文')

		url, kwargs = client.post.call_args[0][0], client.post.call_args[1]
		assert url == 'https://www.notifyx.cn/api/v1/send/notifyxkey'
		assert kwargs['json'] == {'title': '标题', 'content': '正文'}

	def test_team_included_when_configured(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('NOTIFYX_KEY', 'notifyxkey')
		monkeypatch.setenv('NOTIFYX_TEAM', 'group123')
		client, _ = post_spy

		kit().send_notifyx('标题', '正文')

		assert client.post.call_args[1]['json']['team'] == 'group123'

	def test_missing_key_raises(self, kit):
		with pytest.raises(ValueError, match='NotifyX key not configured'):
			kit().send_notifyx('标题', '正文')

	def test_description_included_when_provided(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('NOTIFYX_KEY', 'notifyxkey')
		client, _ = post_spy

		kit().send_notifyx('标题', '正文', description='成功 4/5')

		payload = client.post.call_args[1]['json']
		assert payload['description'] == '成功 4/5'

	def test_description_omitted_when_empty(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('NOTIFYX_KEY', 'notifyxkey')
		client, _ = post_spy

		kit().send_notifyx('标题', '正文')

		assert 'description' not in client.post.call_args[1]['json']


class TestMarkdownDispatch:
	"""push_message(msg_type='markdown') 的渠道分发适配。"""

	MD = '**1. 账号A** ✅\n签到前余额: $10.00'

	def test_notifyx_receives_markdown_as_is(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('NOTIFYX_KEY', 'notifyxkey')
		client, _ = post_spy
		instance = kit()

		instance.push_message('标题', self.MD, msg_type='markdown', description='成功 1/1')

		payload = client.post.call_args[1]['json']
		assert payload['content'] == self.MD  # NotifyX 原生支持 Markdown，原样发送
		assert payload['description'] == '成功 1/1'

	def test_plain_text_channels_receive_stripped_content(self, kit, monkeypatch):
		monkeypatch.setenv('DINGDING_WEBHOOK', 'https://oapi.dingtalk.com/robot/send?token=x')
		instance = kit()
		sent = {}
		monkeypatch.setattr(instance, '_post_json', lambda service, url, data: sent.update({service: data}))

		instance.push_message('标题', self.MD, msg_type='markdown')

		dingtalk_body = sent['DingTalk']['text']['content']
		assert '**' not in dingtalk_body
		assert '1. 账号A ✅' in dingtalk_body

	def test_feishu_receives_markdown_as_is(self, kit, monkeypatch):
		monkeypatch.setenv('FEISHU_WEBHOOK', 'https://open.feishu.cn/hook/x')
		instance = kit()
		sent = {}
		monkeypatch.setattr(instance, '_post_json', lambda service, url, data: sent.update({service: data}))

		instance.push_message('标题', self.MD, msg_type='markdown')

		card_content = sent['Feishu']['card']['elements'][0]['content']
		assert card_content == self.MD

	def test_telegram_receives_html_bold(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'tok')
		monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
		client, _ = post_spy

		kit().push_message('标题', self.MD, msg_type='markdown')

		data = client.post.call_args[1]['json']
		assert data['parse_mode'] == 'HTML'
		assert '<b>1. 账号A</b> ✅' in data['text']
		assert '**' not in data['text']

	def test_text_mode_keeps_content_unchanged_for_plain_channels(self, kit, monkeypatch):
		monkeypatch.setenv('DINGDING_WEBHOOK', 'https://oapi.dingtalk.com/robot/send?token=x')
		instance = kit()
		sent = {}
		monkeypatch.setattr(instance, '_post_json', lambda service, url, data: sent.update({service: data}))

		instance.push_message('标题', '正文', msg_type='text')

		assert sent['DingTalk']['text']['content'] == '标题\n正文'


class TestServerPush:
	def test_url_contains_key(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('SERVERPUSHKEY', 'sckey')
		client, _ = post_spy

		kit().send_serverPush('标题', '正文')

		assert client.post.call_args[0][0] == 'https://sctapi.ftqq.com/sckey.send'
		assert client.post.call_args[1]['json'] == {'title': '标题', 'desp': '正文'}

	def test_missing_key_raises(self, kit):
		with pytest.raises(ValueError, match='Server Push key not configured'):
			kit().send_serverPush('标题', '正文')


class TestGotifyPriority:
	def test_priority_is_clamped_to_max(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('GOTIFY_URL', 'https://gotify.example.com/message')
		monkeypatch.setenv('GOTIFY_TOKEN', 'tok')
		monkeypatch.setenv('GOTIFY_PRIORITY', '99')
		client, _ = post_spy

		kit().send_gotify('标题', '正文')

		assert client.post.call_args[1]['json']['priority'] == 10

	def test_priority_is_clamped_to_min(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('GOTIFY_URL', 'https://gotify.example.com/message')
		monkeypatch.setenv('GOTIFY_TOKEN', 'tok')
		monkeypatch.setenv('GOTIFY_PRIORITY', '-5')
		client, _ = post_spy

		kit().send_gotify('标题', '正文')

		assert client.post.call_args[1]['json']['priority'] == 1

	def test_blank_priority_falls_back_to_default(self, kit, post_spy, monkeypatch):
		monkeypatch.setenv('GOTIFY_URL', 'https://gotify.example.com/message')
		monkeypatch.setenv('GOTIFY_TOKEN', 'tok')
		monkeypatch.setenv('GOTIFY_PRIORITY', '   ')
		client, _ = post_spy

		kit().send_gotify('标题', '正文')

		assert client.post.call_args[1]['json']['priority'] == 9

	def test_missing_token_raises(self, kit, monkeypatch):
		monkeypatch.setenv('GOTIFY_URL', 'https://gotify.example.com/message')

		with pytest.raises(ValueError, match='Gotify URL or Token not configured'):
			kit().send_gotify('标题', '正文')


class TestEmailDefaults:
	@patch('smtplib.SMTP_SSL')
	def test_smtp_host_is_derived_from_user_domain(self, mock_smtp, kit, monkeypatch):
		monkeypatch.setenv('EMAIL_USER', 'me@example.com')
		monkeypatch.setenv('EMAIL_PASS', 'pw')
		monkeypatch.setenv('EMAIL_TO', 'you@example.com')

		kit().send_email('标题', '正文')

		assert mock_smtp.call_args[0] == ('smtp.example.com', 465)

	@patch('smtplib.SMTP_SSL')
	def test_custom_smtp_server_wins(self, mock_smtp, kit, monkeypatch):
		monkeypatch.setenv('EMAIL_USER', 'me@example.com')
		monkeypatch.setenv('EMAIL_PASS', 'pw')
		monkeypatch.setenv('EMAIL_TO', 'you@example.com')
		monkeypatch.setenv('CUSTOM_SMTP_SERVER', 'smtp.custom.net')

		kit().send_email('标题', '正文')

		assert mock_smtp.call_args[0] == ('smtp.custom.net', 465)

	@patch('smtplib.SMTP_SSL')
	def test_html_type_uses_html_subtype(self, mock_smtp, kit, monkeypatch):
		monkeypatch.setenv('EMAIL_USER', 'me@example.com')
		monkeypatch.setenv('EMAIL_PASS', 'pw')
		monkeypatch.setenv('EMAIL_TO', 'you@example.com')
		server = MagicMock()
		mock_smtp.return_value.__enter__.return_value = server

		kit().send_email('标题', '<p>正文</p>', msg_type='html')

		msg = server.send_message.call_args[0][0]
		assert msg.get_content_subtype() == 'html'

	@patch('smtplib.SMTP_SSL')
	def test_sender_override_is_used_in_from_header(self, mock_smtp, kit, monkeypatch):
		monkeypatch.setenv('EMAIL_USER', 'me@example.com')
		monkeypatch.setenv('EMAIL_PASS', 'pw')
		monkeypatch.setenv('EMAIL_TO', 'you@example.com')
		monkeypatch.setenv('EMAIL_SENDER', 'alias@example.com')
		server = MagicMock()
		mock_smtp.return_value.__enter__.return_value = server

		kit().send_email('标题', '正文')

		assert 'alias@example.com' in server.send_message.call_args[0][0]['From']


class TestPushMessageResilience:
	def test_unconfigured_channels_do_not_raise(self, kit, capsys):
		"""所有渠道均未配置时只打印一条提示，不逐渠道刷失败。"""
		kit().push_message('标题', '正文')

		out = capsys.readouterr().out
		assert '未配置任何通知渠道，跳过推送' in out
		assert '推送失败' not in out

	def test_one_failing_channel_does_not_block_others(self, kit, monkeypatch, capsys):
		instance = kit()
		instance.bark_key = 'bark-key'  # 配置一个渠道，避免 push_message 因无渠道早退
		monkeypatch.setattr(instance, 'send_email', MagicMock(side_effect=RuntimeError('smtp down')))
		bark = MagicMock()
		monkeypatch.setattr(instance, 'send_bark', bark)

		assert instance.push_message('标题', '正文') is True

		assert bark.called
		out = capsys.readouterr().out
		assert 'Email 推送失败: smtp down' in out
		assert 'Bark 推送成功' in out

	def test_unconfigured_channels_are_skipped_silently(self, kit, monkeypatch, capsys):
		"""仅配置一个渠道时，其余未配置渠道不刷警告噪音。"""
		instance = kit()
		instance.notifyx_key = 'notifyx-key'
		notifyx = MagicMock()
		monkeypatch.setattr(instance, 'send_notifyx', notifyx)

		instance.push_message('标题', '正文')

		assert notifyx.called
		out = capsys.readouterr().out
		assert 'NotifyX 推送成功' in out
		assert '推送失败' not in out
		assert '未配置' not in out

	def test_all_configured_channels_failing_returns_false(self, kit, monkeypatch, capsys):
		instance = kit()
		for field in (
			'email_user',
			'email_pass',
			'email_to',
			'pushplus_token',
			'server_push_key',
			'dingding_webhook',
			'feishu_webhook',
			'weixin_webhook',
			'gotify_url',
			'gotify_token',
			'telegram_bot_token',
			'telegram_chat_id',
			'bark_key',
			'notifyx_key',
		):
			setattr(instance, field, None)
		instance.bark_key = 'bark-key'
		monkeypatch.setattr(instance, 'send_bark', MagicMock(side_effect=RuntimeError('service down')))

		assert instance.push_message('标题', '正文') is False
		assert '所有已配置通知渠道均发送失败' in capsys.readouterr().out
