import enum
from ncatbot.plugin_system import NcatBotPlugin, command_registry, param, admin_filter
from ncatbot.plugin_system.builtin_plugin.unified_registry.filter_system import filter_registry
from ncatbot.utils import get_log
from ncatbot.core.event import BaseMessageEvent, GroupMessageEvent
from dataclasses import dataclass, field
from .config_proxy import ProxiedPluginConfig
from typing import Optional
from .better_pixiv import BetterPixiv, Tag, DownloadResult, WorkDetail
from pathlib import Path
from .pixiv_db import PixivDB
from .pixiv_sqlmodel import DailyIllustSource
import random
from datetime import datetime

PLUGIN_NAME = 'UnnamedPixivIntegrate'

logger = get_log(PLUGIN_NAME)
enable_group_filter = False
filter_groups = []


class IllustSourceType(enum.Enum):
    user = 0


@dataclass
class IllustSource(ProxiedPluginConfig):
    source_type: IllustSourceType = field(default=IllustSourceType.user)
    source_content: str = field(default='')


@dataclass
class DailyIllustConfig(ProxiedPluginConfig):
    enable: bool = field(default=False)
    source: IllustSource = field(default_factory=IllustSource)


@dataclass
class PixivConfig(ProxiedPluginConfig):
    refresh_token: str = field(default='')
    proxy_server: str = field(default='')
    max_single_work_cnt: int = field(default=20)
    enable_group_filter: bool = field(default=False)
    filter_group: list[int] = field(default_factory=list)
    daily_illust_config: DailyIllustConfig = field(default_factory=DailyIllustConfig)


@filter_registry.register('group_filter')
def filter_group_by_config(event: BaseMessageEvent) -> bool:
    if not event.is_group_event():
        return False
    assert isinstance(event, GroupMessageEvent)
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
    pixiv_db: Optional[PixivDB] = None

    async def on_load(self) -> None:
        self.pixiv_config = PixivConfig(self)

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

        def init_group_filter():
            logger.info(f'启用指定群聊过滤: {self.pixiv_config.filter_group}')
            global enable_group_filter, filter_groups
            enable_group_filter = True
            filter_groups += self.pixiv_config.filter_group

        if self.pixiv_config.enable_group_filter:
            init_group_filter()

        # noinspection PyUnreachableCode
        async def init_daily_illust():
            logger.info('检测到每日插画功能已配置')
            logger.info(f'初始化pixiv数据库')
            if self.pixiv_db is None:
                db_url = self.workspace / Path('pixiv.db')
                self.pixiv_db = PixivDB(f'sqlite:///{str(db_url)}')
            # 目前只实现收藏拉取功能
            if self.pixiv_config.daily_illust_config.source.source_type == IllustSourceType.user.value:
                source_content = self.pixiv_config.daily_illust_config.source.source_content
                if not source_content:
                    logger.error(f'未配置源, 无法启用')
                    return
                if not source_content.isdigit():
                    logger.error(f'插画源配置无效: {source_content} 需为数字')
                    return
                source_id = int(source_content)
                test_source_list = await self.pixiv_api.get_favs(source_id, max_page_cnt=1)
                if not test_source_list:
                    logger.error(f'测试拉取每日插画源时出错, 无法启用')
                    return
            else:
                logger.error(f'每日插画源配置无效: {self.pixiv_config.daily_illust_config.source} 无法启用')
            self.add_scheduled_task(self.post_daily_illust, 'DailyIllustPost', '08:00',
                                    kwargs={'today': datetime.now()})
            logger.info(f'每日插画定时任务注册完成')

        if self.pixiv_config.daily_illust_config.enable:
            await init_daily_illust()

        await super().on_load()

    async def on_close(self) -> None:
        if self.init:
            await self.pixiv_api.shutdown()
        await super().on_close()

    async def get_aviliable_groups(self):
        if self.pixiv_config.enable_group_filter:
            aviliable_groups = self.pixiv_config.filter_group
        else:
            aviliable_groups = [int(str_group_id) for str_group_id in await self.api.get_group_list()]
        return aviliable_groups

    async def update_daily_illust_source(self):
        logger.info(f'开始更新每日插画源')
        logger.info(f'从 {self.pixiv_config.daily_illust_config.source} 拉取每日插画')

        async def fav_progress(favs: list[WorkDetail], now_page: int):
            print(f'\r拉取收藏第{now_page}页', end='')
            sources = [DailyIllustSource(work_id=fav.id, user_id=fav.user.id) for fav in favs]
            self.pixiv_db.insert_daily_illust_source_rows(sources)

        if self.pixiv_config.daily_illust_config.source.source_type == IllustSourceType.user.value:
            source_content = self.pixiv_config.daily_illust_config.source.source_content
            user_id = int(source_content)
            await self.pixiv_api.get_favs(user_id, hook_func=fav_progress)
        else:
            logger.error(f'每日插画源配置无效: {self.pixiv_config.daily_illust_config.source} 无法更新')
        logger.info(f'每日插画源更新完成')

    async def get_daily_illust(self) -> Optional[tuple[int, Path]]:
        logger.debug(f'请求提取每日插画')
        db_result = self.pixiv_db.get_random_daily_illust()
        if db_result is None:
            logger.warning(f'无法获取到任何有效插画源')
            return None
        logger.info(f'获取到随机插画id: {db_result.work_id} 开始拉取')
        work_detail = await self.pixiv_api.get_work_details(work_id=db_result.work_id)
        if work_detail is None:
            logger.warning(f'获取插画详情失败, 可能不存在')
            return None
        work_result = await self.pixiv_api.download(work_ids=[work_detail])
        if work_result.total != len(work_result.success_units):
            logger.warning(f'插画下载失败')
            return None
        single_result = work_result.success_units[0]
        assert isinstance(single_result, DownloadResult)
        file_path: Path = random.choice(single_result.success_units)
        return single_result.task_id, file_path

    async def post_daily_illust(self, today: datetime):
        logger.info(f'开始推送每日插画')
        result = await self.get_daily_illust()
        if result is None:
            logger.warning(f'获取插画失败')
            return
        work_id, work_path = result
        groups_to_post = await self.get_aviliable_groups()
        logger.info(f'将对群聊: {groups_to_post} 进行每日插画推送')
        for group_id in groups_to_post:
            await self.api.send_group_image(group_id, str(work_path))
            logger.debug(f'群 {group_id} 推送完成')
        logger.info(f'每日插画推送完成')

    @admin_filter
    @filter_registry.filters('group_filter')
    @command_registry.command('test_pdi')
    async def test_post_daily_illust(self, event: GroupMessageEvent):
        await event.reply(f'手动测试每日涩图推送')
        await self.post_daily_illust(datetime.now())
        await event.reply(f'执行完成')

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
        if work_details is None:
            await event.reply(f'无法获取作品详情, 可能是作品不存在')
            return
        if work_details.meta_pages:
            if len(work_details.meta_pages) > self.pixiv_config.max_single_work_cnt:
                await event.reply(f'超过单个作品数量限制({self.pixiv_config.max_single_work_cnt}),不下载')
                return
        download_result = await self.pixiv_api.download([work_details])
        if download_result.total != len(download_result.success_units):
            logger.error(f'{download_result}下载失败')
            await event.reply(f'下载失败')
            return
        assert len(download_result.success_units) > 0
        single_result: DownloadResult = download_result.success_units[0]
        for file_path in single_result.success_units:
            send_result = await self.api.send_group_image(event.group_id, str(file_path))
            logger.info(send_result)
        await event.reply('发送完成')

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

        await event.reply(
            f'\n{work_details.title=}\n{work_details.create_date=}\n{work_details.user.name=}\n{work_details.user.id=}\n{plain_tags(work_details.tags)=}')

    @admin_filter
    @filter_registry.filters('group_filter')
    @command_registry.command('update_illust_source', aliases=['uis'])
    async def request_update_daliy_illust(self, event: GroupMessageEvent):
        if not self.init:
            await event.reply(f'未配置refresh_token, 联系管理员进行配置后重启尝试')
            return
        logger.info(f'收到每日插画源更新请求')
        await event.reply('开始更新')
        await self.update_daily_illust_source()
        await event.reply('更新完成')
