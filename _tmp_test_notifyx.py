"""一次性验证脚本：用 NOTIFYX_KEY 走真实代码路径发送测试消息。"""
import os

os.environ['NOTIFYX_KEY'] = 'mgo_nHvLfo18FL0fjRjXvq4ShDqFwCMdBIOs'

from utils.notify import NotificationKit

kit = NotificationKit()
print(f'[验证] 通道已配置: {kit._any_channel_configured()}')

ok = kit.push_message(
    'AnyRouter 签到通知测试',
    '这是一条 NotifyX 通道连通性测试消息。\n如果你收到本消息，说明 NotifyX 适配配置成功。',
    msg_type='text',
)
print(f'[验证] 推送结果: {ok}')
