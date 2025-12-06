import os.path
import asyncio
from ncatbot.plugin_system import NcatBotPlugin, command_registry, group_filter, param
from ncatbot.utils import get_log
from ncatbot.core.event import GroupMessageEvent, BaseMessageEvent
from dataclasses import dataclass, fields, asdict, is_dataclass, field, MISSING
from typing import Optional
from .better_pixiv import BetterPixiv
from pathlib import Path

logger = get_log("UnnamedPixivIntegrate")


# noinspection PyDataclass,PyArgumentList
def bind_config[T](plugin: NcatBotPlugin, config_class: type[T]) -> T:
    """
    将 Dataclass 绑定到 NcatBot 的配置系统 (Python 3.12+ PEP 695 版本)。
    """
    if not is_dataclass(config_class):
        raise TypeError("config_class must be a dataclass")
    # 1. 自动注册配置项
    for reg_field in fields(config_class):
        default_val = None
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
        plugin.register_config(reg_field.name, default_val)
    # 2. 从 plugin.config 读取最终值 (YAML > 默认值)
    loaded_data = {}
    for reg_field in fields(config_class):
        if reg_field.name in plugin.config:
            loaded_data[reg_field.name] = plugin.config[reg_field.name]
    # 3. 实例化 Dataclass
    return config_class(**loaded_data)


# noinspection PyDataclass,PyTypeChecker
def sync_config[T](plugin: NcatBotPlugin, config_instance: T) -> None:
    """
    将 Dataclass 的当前值反向同步回 plugin.config。
    用于运行时修改配置并持久化。
    """
    if not is_dataclass(config_instance):
        return
    current_data = asdict(config_instance)
    for key, value in current_data.items():
        plugin.config[key] = value


@dataclass
class PixivConfig:
    refresh_token: str = field(default='')
    proxy_server: str = field(default='')


class UnnamedPixivIntegrate(NcatBotPlugin):
    name = "UnnamedPixivIntegrate"  # 必须，插件名称，要求全局独立
    version = "0.0.1"  # 必须，插件版本
    dependencies = {}  # 必须，依赖的其他插件和版本
    description = "集成pixiv功能"  # 可选
    author = "default_user"  # 可选

    init: bool = False
    pixiv_api: Optional[BetterPixiv] = None

    async def on_load(self) -> None:
        pixiv_config = bind_config(self, PixivConfig)
        if pixiv_config.refresh_token:
            logger.info(f'正在使用token: {pixiv_config.refresh_token}')
            self.init = True
        else:
            logger.error(f'必须配置refresh_token, 将不会初始化pixiv功能')
            return
        if pixiv_config.proxy_server:
            logger.info(f'检测到代理服务器: {pixiv_config.proxy_server}')
        self.pixiv_api = BetterPixiv(proxy=pixiv_config.proxy_server if pixiv_config.proxy_server else None,
                                     logger=get_log('pixiv'))
        await self.pixiv_api.api_login(refresh_token=pixiv_config.refresh_token)
        await self.pixiv_api.get_work_details(115081727)
        cur_loop = asyncio.get_running_loop()
        logger.debug(f'on_load使用的loop: {cur_loop}, id: {id(cur_loop)}')
        await super().on_load()

    async def on_close(self) -> None:
        await self.pixiv_api.shutdown()
        await super().on_close()

    @group_filter
    @command_registry.command('pixiv', description='根据id获取对应illust')
    @param(name='work_id', help='作品id', default=-1)
    async def get_illust_work(self, event: GroupMessageEvent, work_id: int = -1):
        if not self.init:
            await event.reply(f'未配置refresh_token, 联系管理员进行配置后重启尝试')
            return
        if work_id == -1:
            await event.reply(f'未输入作品id,重试')
            return
        self.pixiv_api.set_storge_path(self.workspace / Path('temp_dl'))
        download_result = await self.pixiv_api.download([work_id])
        if download_result.total != download_result.success:
            logger.error(f'{work_id}下载失败')
            await event.reply(f'下载失败')
            return
        group_id = event.group_id
        for path in download_result.paths:
            await self.api.send_group_image(group_id, str(path))
            await asyncio.sleep(1)
        await event.reply(f'发送完成')
