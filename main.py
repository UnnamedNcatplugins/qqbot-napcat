from pathlib import Path
from ncatbot.core import BotClient
from ncatbot.plugin_system import on_group_at
from ncatbot.core.event import GroupMessageEvent
bot = BotClient()


@on_group_at
async def at_func(event: GroupMessageEvent):
    await event.reply(image=str(Path('what.jpg')))


bot.run_frontend()
