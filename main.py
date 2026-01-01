from pathlib import Path
from ncatbot.core import BotClient
from ncatbot.plugin_system import on_group_poke, on_group_at, admin_filter
from ncatbot.core.event import PokeNoticeEvent, GroupMessageEvent
from ncatbot.core.event.message_segment.message_segment import PlainText
from ncatbot.utils import status, get_log, ncatbot_config
import subprocess
bot = BotClient()
WHAT_JPG = Path('what.jpg')
logger = get_log('Main')


@on_group_poke
async def poke_func(event: PokeNoticeEvent):
    if event.target_id != ncatbot_config.bt_uin:
        return
    if WHAT_JPG.exists():
        await status.global_api.send_group_image(event.group_id, str(WHAT_JPG))


@admin_filter
@on_group_at
async def cmd_func(event: GroupMessageEvent):
    shell_cmd = None
    logger.debug(f'收到at消息, 开始验证')
    if len(event.message) != 2:
        logger.debug(f'at消息长度不为2, 已取消')
    for message_segment in event.message:
        if message_segment.msg_seg_type == 'text':
            logger.debug(f'解析到文本消息段, 类型: {type(message_segment)}, 内容: {message_segment.text}')
            assert isinstance(message_segment, PlainText)
            shell_cmd = message_segment.text if message_segment.text[0] != ' ' else message_segment.text[1:]
            continue
    if shell_cmd is None:
        return
    await event.reply(subprocess.run(shell_cmd, capture_output=True, text=True).stdout)


bot.run_frontend()
