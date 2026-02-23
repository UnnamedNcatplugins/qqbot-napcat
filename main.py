from pathlib import Path
from ncatbot.core import BotClient
from ncatbot.plugin_system import on_group_poke, on_group_at, admin_filter, on_message
from ncatbot.core.event import PokeNoticeEvent, GroupMessageEvent, BaseMessageEvent, PrivateMessageEvent
from ncatbot.core.event.message_segment.message_segment import PlainText, Forward, Node, Reply
from ncatbot.utils import status, get_log, ncatbot_config
from ncatbot.utils.error import NcatBotConnectionError
import ncatbot
import subprocess
bot = BotClient()
WHAT_JPG = Path('what.jpg')
logger = get_log('Main')


def always_true(x=True):
    return True


# 用户有义务自行确保密码强度
ncatbot.utils.config.strong_password_check = always_true
# hook以确保远端模式正常运行
if ncatbot_config.napcat.remote_mode:
    ncatbot.utils.config.is_napcat_local = always_true


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
        return
    for message_segment in event.message:
        if message_segment.msg_seg_type == 'text':
            logger.debug(f'解析到文本消息段, 类型: {type(message_segment)}, 内容: {message_segment.text}')
            assert isinstance(message_segment, PlainText)
            shell_cmd = message_segment.text if message_segment.text[0] != ' ' else message_segment.text[1:]
            continue
    if shell_cmd is None:
        logger.debug('没有文本消息, 取消')
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


@on_message
async def export_msgs(event: BaseMessageEvent):
    if event.message_type != 'private':
        return
    assert isinstance(event, PrivateMessageEvent)
    if event.message.messages[0].msg_seg_type != 'forward':
        return
    forward_msg = event.message.messages[0]
    assert isinstance(forward_msg, Forward)
    forward_content = await forward_msg.get_content()
    await event.reply('检测到合并转发, 开始导出')
    sum_str = ''
    for node in forward_content:
        if node.msg_seg_type != 'node':
            continue
        assert isinstance(node, Node)
        cited_content: str | None = None
        for msg_seg in node.content:
            if msg_seg.msg_seg_type == 'reply':
                logger.debug(f'检测到引用消息')
                assert isinstance(msg_seg, Reply)
                cited_msg = await status.global_api.get_msg(msg_seg.id)
                cited_content = cited_msg.raw_message
        node_str = '' if cited_content is None else f'[引用: {cited_content}]'
        node_str += node.get_summary() + '\n'
        sum_str += node_str
    with open(f'{forward_msg.id}.txt', 'w', encoding='utf-8') as f:
        f.write(sum_str)
    await event.reply(f'导出完成, 文件名为{forward_msg.id}.txt')


retry_cnt = 0


while True:
    try:
        if retry_cnt >= 10:
            logger.error(f'连接重试已达最大限制, 退出')
            break
        bot.run_frontend()
        break
    except NcatBotConnectionError as ncne:
        retry_cnt += 1
        logger.warning(f'检测到napcat主动关闭链接错误, 尝试恢复')
