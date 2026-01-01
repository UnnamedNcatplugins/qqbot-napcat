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
    try:
        # 设置 timeout=10，意味着命令最多跑10秒，超时就杀掉
        # 这里的 capture_output=True 等价于 stdout=PIPE, stderr=PIPE
        result = subprocess.run(
            shell_cmd,
            capture_output=True,
            text=True,
            shell=True,
            timeout=10  # <--- 关键修改：加上超时时间
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[Stderr]\n{result.stderr}"

    except subprocess.TimeoutExpired:
        # 如果超时了，subprocess.run 会自动尝试杀掉进程，并抛出此异常
        output = f"⚠️ 命令执行超时（超过10秒），已被强制终止。\n请避免执行 top/vim 等交互式或长耗时命令。"
    except Exception as e:
        output = f"执行出错: {str(e)}"
    await event.reply(output)


bot.run_frontend()
