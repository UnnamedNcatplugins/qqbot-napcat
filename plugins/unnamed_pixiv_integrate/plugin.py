import asyncio
from ncatbot.plugin_system import NcatBotPlugin, command_registry, group_filter, param
from ncatbot.plugin_system.builtin_plugin.unified_registry.filter_system import filter_registry
from ncatbot.utils import get_log
from ncatbot.core.event import GroupMessageEvent
from dataclasses import dataclass, fields, is_dataclass, field, MISSING
from typing import Optional
from .better_pixiv import BetterPixiv, Tag
from pathlib import Path

PLUGIN_NAME = 'UnnamedPixivIntegrate'

logger = get_log(PLUGIN_NAME)
enable_group_filter = False
filter_groups = []


# noinspection PyDataclass,PyArgumentList
def bind_config[T](plugin: NcatBotPlugin, config_class: type[T]) -> T:
    """
    将 Dataclass 绑定到 NcatBot 的配置系统 (Python 3.12+ PEP 695 版本)。
    """
    if not is_dataclass(config_class):
        raise TypeError("config_class must be a dataclass")
    # 1. 自动注册配置项
    for reg_field in fields(config_class):
        # === 修复核心：使用 MISSING 哨兵值进行判断 ===
        # 情况 A: 字段定义了 default (例如: a: int = 1)
        if reg_field.default is not MISSING:
            default_val = reg_field.default
        # 情况 B: 字段定义了 default_factory (例如: b: list = field(default_factory=list))
        elif reg_field.default_factory is not MISSING:
            default_val = reg_field.default_factory()
        # 情况 C: 没有默认值 (在配置系统中通常设为 None 或报错，这里给 None 方便注册)
        else:
            default_val = None
        # 注册到 NcatBot
        # 如果 data/xxx.yaml 中已经有值，NcatBot 会忽略这个 default_val
        plugin.register_config(reg_field.name, default_val, value_type=type(default_val))
    # 2. 从 plugin.config 读取最终值 (YAML > 默认值)
    loaded_data = {}
    for reg_field in fields(config_class):
        if reg_field.name in plugin.config:
            loaded_data[reg_field.name] = plugin.config[reg_field.name]
    # 3. 实例化 Dataclass
    return config_class(**loaded_data)


@dataclass
class PixivConfig:
    refresh_token: str = field(default='')
    proxy_server: str = field(default='')
    max_single_work_cnt: int = field(default=20)
    enable_group_filter: bool = field(default=False)
    filter_group: list[int] = field(default_factory=list)


@filter_registry.register('group_filter')
def filter_group_by_config(event: GroupMessageEvent) -> bool:
    if enable_group_filter:
        return int(event.group_id) in filter_groups
    return True


class UnnamedPixivIntegrate(NcatBotPlugin):
    name = PLUGIN_NAME  # 必须，插件名称，要求全局独立
    version = "0.0.1"  # 必须，插件版本
    dependencies = {}  # 必须，依赖的其他插件和版本
    description = "集成pixiv功能"  # 可选
    author = "default_user"  # 可选

    init: bool = False
    pixiv_api: Optional[BetterPixiv] = None
    pixiv_config: Optional[PixivConfig] = None

    async def on_load(self) -> None:
        self.pixiv_config = bind_config(self, PixivConfig)
        if self.pixiv_config.refresh_token:
            logger.info(f'正在使用token: {self.pixiv_config.refresh_token}')
            self.init = True
        else:
            logger.error(f'必须配置refresh_token, 将不会初始化pixiv功能')
            return
        if self.pixiv_config.proxy_server:
            logger.info(f'检测到代理服务器: {self.pixiv_config.proxy_server}')
        self.pixiv_api = BetterPixiv(proxy=self.pixiv_config.proxy_server if self.pixiv_config.proxy_server else None,
                                     logger=get_log('pixiv'))
        await self.pixiv_api.api_login(refresh_token=self.pixiv_config.refresh_token)
        if self.pixiv_config.enable_group_filter:
            logger.info(f'启用指定群聊过滤: {self.pixiv_config.filter_group}')
            global enable_group_filter, filter_groups
            enable_group_filter = True
            filter_groups += self.pixiv_config.filter_group
        await super().on_load()

    async def on_close(self) -> None:
        if self.init:
            await self.pixiv_api.shutdown()
        await super().on_close()

    @group_filter
    @filter_registry.filters('group_filter')
    @command_registry.command('pixiv', aliases=['p'], description='根据id获取对应illust')
    @param(name='work_id', help='作品id', default=-1)
    async def get_illust_work(self, event: GroupMessageEvent, work_id: int = -1):
        if not self.init:
            await event.reply(f'未配置refresh_token, 联系管理员进行配置后重启尝试')
            return
        if work_id == -1:
            await event.reply(f'未输入作品id,重试')
            return
        await event.reply(f'命令收到')
        self.pixiv_api.set_storge_path(self.workspace / Path('temp_dl'))
        work_details = await self.pixiv_api.get_work_details(work_id)
        if work_details.meta_pages:
            if len(work_details.meta_pages) > self.pixiv_config.max_single_work_cnt:
                await event.reply(f'超过单个作品数量限制({self.pixiv_config.max_single_work_cnt}),不下载')
                return
        download_result = await self.pixiv_api.download([work_details])
        if download_result.total != download_result.success:
            logger.error(f'{download_result}下载失败')
            await event.reply(f'下载失败')
            return
        for path in download_result.paths:
            await self.api.send_group_image(event.group_id, str(path))
            await asyncio.sleep(1)
        await event.reply('发送完成')


    @group_filter
    @filter_registry.filters('group_filter')
    @command_registry.command('pixiv_info', aliases=['pi'], description='根据id获取对应illust info')
    @param(name='work_id', help='作品id', default=-1)
    async def get_illust_info(self, event: GroupMessageEvent, work_id: int = -1):
        if not self.init:
            await event.reply(f'未配置refresh_token, 联系管理员进行配置后重启尝试')
            return
        if work_id == -1:
            await event.reply(f'未输入作品id,重试')
            return
        work_details = await self.pixiv_api.get_work_details(work_id)

        def plain_tags(tags: list[Tag]):
            return [tag.name for tag in tags]
        await event.reply(f'\n{work_details.title=}\n{work_details.create_date=}\n{work_details.user.name=}\n{work_details.user.id=}\n{plain_tags(work_details.tags)=}')
