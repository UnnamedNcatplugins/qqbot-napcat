import jmcomic
from ncatbot.plugin_system import NcatBotPlugin, command_registry
from dataclasses import dataclass, field
from .config_proxy import ProxiedPluginConfig
from ncatbot.utils import get_log
from ncatbot.core.event import GroupMessageEvent

PLUGIN_NAME = 'UnnamedJmComicIntegrate'

logger = get_log(PLUGIN_NAME)


def format_name(raw_str) -> str:
    converted_str = ''
    in_bracket = 0
    for c in raw_str:
        if c in [']', '}', ')', '】', '）', '］']:
            in_bracket -= 1
            continue
        elif c in ['[', '{', '【', '(', '（', '［']:
            in_bracket += 1
            continue
        elif c == ' ':
            continue
        if not in_bracket:
            converted_str += c
        else:
            continue
    return converted_str


@dataclass
class JmComicConfig(ProxiedPluginConfig):
    proxy_server: str = field(default='')


class UnnamedJmComicIntegrate(NcatBotPlugin):
    name = PLUGIN_NAME  # 必须，插件名称，要求全局独立
    version = "0.0.1"  # 必须，插件版本
    dependencies = {}  # 必须，依赖的其他插件和版本
    description = "集成jmcomic功能"  # 可选
    author = "default_user"  # 可选

    jm_config: JmComicConfig = None
    jm_client: jmcomic.JmApiClient | jmcomic.JmHtmlClient = None

    async def on_load(self) -> None:
        jmcomic.disable_jm_log()
        jm_option = jmcomic.JmOption.default()
        self.jm_config = JmComicConfig(self)
        if self.jm_config.proxy_server:
            logger.info(f'检测到已配置代理: {self.jm_config.proxy_server}')
            jm_option.client['postman']['meta_data']['proxies']['http'] = self.jm_config.proxy_server
        self.jm_client = jmcomic.JmOption.new_jm_client(jm_option)
        await super().on_load()

    @command_registry.command('jm')
    async def resolve_jmid(self, event: GroupMessageEvent, jm_id: int = -1) -> None:
        if jm_id == -1:
            await event.reply(f'未设定jmid,重试')
            return
        page = self.jm_client.search_site(search_query=str(jm_id))
        if not hasattr(page, 'album'):
            await event.reply(f'无法解析的JM号{jm_id}')
            return
        album: jmcomic.JmAlbumDetail = page.single_album
        await self.api.send_group_text(event.group_id, album.title)
        await event.reply(f'\n{album.title=}\n{album.tags}')

    async def on_close(self) -> None:
        await super().on_close()
