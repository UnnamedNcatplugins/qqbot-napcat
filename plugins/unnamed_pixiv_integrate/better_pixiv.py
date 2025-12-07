import asyncio
import functools
import io
import logging
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Self
import natsort
from PIL import Image
from pixivpy_async import *
from tqdm import tqdm


@dataclass
class User:
    id: int
    name: str
    account: str
    profile_image_urls: list[str]
    is_followed: bool
    is_accept_request: bool


@dataclass
class Tag:
    name: str
    translated_name: Optional[str]


@dataclass
class MetaSinglePage:
    original_image_url: Optional[str] = field(default=None)  # 设为可选，兼容空对象


@dataclass
class MetaPageImageUrls:
    original: str
    square_medium: Optional[str] = field(default=None)
    medium: Optional[str] = field(default=None)
    large: Optional[str] = field(default=None)


@dataclass
class MetaPage:
    image_urls: MetaPageImageUrls


@dataclass
class WorkDetail:
    id: int
    title: str
    type: str
    caption: str
    user: User
    tags: list[Tag]
    create_date: str
    page_count: int
    width: int
    height: int
    meta_single_page: MetaSinglePage
    meta_pages: list[MetaPage]  # 存储每一页的详细信息
    total_view: int
    total_bookmarks: int
    sanity_level: Optional[int] = field(default=None)
    x_restrict: Optional[int] = field(default=None)
    restrict: Optional[int] = field(default=None)
    is_bookmarked: Optional[bool] = field(default=None)
    visible: Optional[bool] = field(default=None)
    is_muted: Optional[bool] = field(default=None)
    total_comments: Optional[int] = field(default=None)
    illust_ai_type: Optional[int] = field(default=None)
    illust_book_style: Optional[int] = field(default=None)
    comment_access_control: Optional[int] = field(default=None)
    restriction_attributes: Optional[list[str]] = field(default=None)


@dataclass
class DownloadResult:
    task_id: int
    total: int
    success: int
    paths: list[Path]
    extra_info: Optional[str]
    failed_unit: list[Self | str]


class PixivError(Exception):
    def __init__(self, message=''):
        self.message = message
        super().__init__(self.message)


class BetterPixiv:
    def __init__(self, proxy=None, bypass=False, logger: Optional[logging.Logger] = None):
        self.client = PixivClient(proxy=proxy, bypass=bypass)
        self.api = AppPixivAPI(client=self.client.start(), proxy=proxy, bypass=bypass)
        self.proxy = proxy
        self.bypass = bypass
        self.storge_path: Path = Path(os.path.curdir)
        self._token_refresh_lock = asyncio.Lock()
        if not logger:
            try:
                from .setup_logger import get_logger
            except ImportError:
                from setup_logger import get_logger
            self.logger = get_logger('pixiv')
        else:
            self.logger = logger

    async def ensure_connection(self):
        # 1. 获取当前 Loop
        current_loop = asyncio.get_running_loop()
        # 2. 检查是否需要重置 (Session 为空，或 Session 关闭，或 Loop 不一致)
        # 注意：这里深入检查了 client 内部的 session
        # 假设 self.api.client 是那个 aiohttp session 对象，或者是包装器
        # 如果是包装器，你可能需要 getattr(self.api.client, "session", None)
        # 为了通用性，我们直接假设只要 loop 变了就得重置
        is_loop_changed = False
        try:
            if self.api and self.client:
                # noinspection PyProtectedMember
                if self.api.session._loop is not current_loop:
                    is_loop_changed = True
        except Exception as e:
            self.logger.debug(e)
            pass
        if is_loop_changed:
            self.logger.debug("检测到 Event Loop 变更或 Session 失效，正在执行热重载...")
            saved_access_token = self.api.access_token
            saved_refresh_token = self.api.refresh_token
            # --- 步骤 B: 重塑肉身 (创建新 Session) ---
            # 这一步必须在当前 Loop 执行
            # 注意：PixivClient 只是配置容器，start() 才是创建 session
            # 我们重新创建一个 PixivClient 实例以防万一
            await self.client.close()
            self.client = PixivClient(proxy=self.proxy, bypass=self.bypass)
            # 创建新的 API 对象 (此时它是未登录状态)
            self.api = AppPixivAPI(client=self.client.start())
            # --- 步骤 C: 灵魂注入 (跳过登录，直接 Set Auth) ---
            self.logger.debug(f'寻获的token at: {saved_access_token} rt: {saved_refresh_token}')
            if saved_access_token and saved_refresh_token:
                self.logger.debug("恢复登录凭证，跳过网络登录步骤。")
                # 直接设置内存状态，不发包！
                self.api.set_auth(saved_access_token, saved_refresh_token)
            else:
                self.logger.warning("无缓存凭证，后续可能需要重新 api_login")

    @staticmethod
    def retry_on_error(func):
        """
        装饰器现在作为静态方法存在，不需要外部的 self。
        self 在 wrapper 被调用时作为第一个参数传入。
        """
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            try:
                try:
                    # 这里的 self 是运行时传入的 BetterPixiv 实例
                    return await func(self, *args, **kwargs)
                except RuntimeError as e:
                    # 捕获 Event loop is closed 异常
                    if str(e) == 'Event loop is closed':
                        self.logger.debug(f'检测到 Event loop is closed ({func.__name__})，正在尝试热重载 Session...')
                        # 关键点：调用上一轮我们定义的修复方法
                        # 如果你还没写 ensure_connection，请务必把上一轮的 ensure_connection 方法加到类里
                        await self.ensure_connection()
                        self.logger.debug(f'Session 热重载完成，正在重试: {func.__name__}')
                        # 修复后重试原函数
                        return await func(self, *args, **kwargs)
                    else:
                        # 如果是其他 Runtime 错误，直接抛出，不要吞掉
                        raise e
            except PixivError:
                self.logger.warning(f'Token可能过期, 尝试刷新后重试: {func.__name__}')
                # 使用实例中的锁
                async with self._token_refresh_lock:
                    await self.api_login(refresh=True)
                return await func(self, *args, **kwargs)
        return wrapper

    async def api_login(self, refresh_token='', refresh=False) -> str:
        if refresh:
            refresh_token = self.api.refresh_token
        if not refresh_token:
            raise PixivError('未提供refresh_token')
        token = await self.api.login(refresh_token=refresh_token)
        if 'access_token' in token and token['access_token']:
            self.api.set_auth(token['access_token'], refresh_token=refresh_token)
            self.logger.info('登录成功')
        else:
            raise PixivError('登录失败')
        return token['access_token']

    async def shutdown(self):
        await self.client.close()
        self.logger.info('关闭完成')

    def set_storge_path(self, path: Path):
        if path.is_absolute():
            self.storge_path = path
        else:
            self.storge_path = Path(os.path.curdir) / path
        if not self.storge_path.exists():
            try:
                self.storge_path.mkdir(parents=True)
                self.logger.info('未检测到设置的下载目录，已创建')
            except OSError as e:
                self.storge_path = Path(os.path.curdir)
                self.logger.warning('目录创建失败，将使用默认目录', e)

    @retry_on_error
    async def __download_single_file(self,
                                     sem: asyncio.Semaphore,
                                     url: str,
                                     file_downloaded_callback: Optional[Callable[[str], None]] = None) -> Path:
        async with sem:
            for retry_times in range(10):
                try:
                    await self.api.download(url, path=str(self.storge_path))
                    if file_downloaded_callback:
                        file_downloaded_callback(url)
                    break
                except Exception as dl_e:
                    # 使用 tqdm.write 防止打断进度条
                    self.logger.warning(f'下载{os.path.basename(url)} 异常: {dl_e}, 重试第{retry_times}次')
                    await asyncio.sleep(1)
            else:
                raise PixivError(f"Download failed after retries: {url}")
            return Path(self.storge_path) / Path(os.path.basename(url))

    async def __download_single_work(self,
                                     work_details: WorkDetail,
                                     sem: asyncio.Semaphore,
                                     phase_callback: Optional[Callable[[int, str], None]] = None) -> DownloadResult:
        download_result = DownloadResult(task_id=0, total=0, success=0, paths=[], extra_info=None, failed_unit=[])
        if work_details.type not in ("illust", "ugoira"):
            download_result.extra_info = 'work不是illust或ugoria'
            return download_result
        if work_details.type == 'illust':
            # 解析 URL 列表
            work_url_list = []
            if work_details.meta_pages:
                work_url_list = [cop.image_urls.original for cop in work_details.meta_pages]
            elif work_details.meta_single_page:
                work_url_list.append(work_details.meta_single_page.original_image_url)
            download_result.total = len(work_url_list)

            def _phase_callback(single_url: str):
                download_result.success += 1
                download_result.paths.append(Path(self.storge_path) / Path(os.path.basename(single_url)))
                if phase_callback:
                    phase_callback(work_details.id, single_url)
            tasks = [self.__download_single_file(sem, url, _phase_callback) for url in work_url_list]
            await asyncio.gather(*tasks)
        else:
            download_result.total = 1
            ugoira_metadata = await self.api.ugoira_metadata(work_details.id)
            zip_url = ugoira_metadata['ugoira_metadata']['zip_urls']['medium']
            filename = Path(zip_url.split('/')[-1])
            zip_path = self.storge_path / filename
            try:
                if not zip_path.exists():
                    if not await self.__download_single_file(sem, zip_url):
                        download_result.failed_unit.append(zip_url)
                        download_result.extra_info = f'Error in downloading {zip_url}'
                        return download_result
            except KeyboardInterrupt:
                raise KeyboardInterrupt
            except Exception as e:
                self.logger.warning(e)
                download_result.extra_info = str(e)
                download_result.failed_unit.append(zip_url)
                return download_result
                # 打开ZIP文件
            with zipfile.ZipFile(zip_path, 'r') as zip_file:
                # 过滤出图片文件（假设支持的图片格式为 .png 和 .jpg）
                image_files = [f for f in zip_file.namelist() if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                image_files = natsort.natsorted(image_files)
                # 加载图片到内存
                images = []
                for image_file in image_files:
                    with zip_file.open(image_file) as image_data:
                        images.append(Image.open(io.BytesIO(image_data.read())))
            # 确保有图片文件
            if not images:
                download_result.failed_unit.append(zip_url)
                download_result.extra_info = 'No images found in the ZIP file'
                return download_result
            # 将所有图片转换为 GIF 并保存
            gif_path = self.storge_path / Path(f'{filename}.gif')
            images[0].save(
                gif_path,
                save_all=True,
                append_images=images[1:],
                duration=100,
                loop=1
            )
            zip_path.unlink()
            download_result.paths.append(gif_path)
            download_result.success += 1
        return download_result

    async def download(self, work_ids: list[WorkDetail],
                       max_workers: int = 3,
                       phase_callback: Optional[Callable[[int, str], None]] = None) -> DownloadResult:
        if not isinstance(work_ids, list):
            work_ids = [work_ids]
        download_result = DownloadResult(task_id=0, total=0, success=0, paths=[], extra_info=None, failed_unit=[])
        if len(work_ids) == 0:
            return download_result
        assert isinstance(work_ids[0], WorkDetail)
        semaphore = asyncio.Semaphore(max_workers)
        self.logger.info(f'启动下载任务, 目标ID数: {len(work_ids)}, 最大并发: {max_workers}')
        pbar_works: Optional[tqdm] = None
        if phase_callback is None:
            pbar_works = tqdm(total=len(work_ids), desc="[作品进度]", position=0, leave=True, colour='green')

        def on_file_downloaded(work_id, url):
            if phase_callback:
                phase_callback(work_id, url)

        async def work_task_wrapper(wid):
            res = await self.__download_single_work(
                wid,
                semaphore,
                phase_callback=on_file_downloaded
            )
            if pbar_works:
                pbar_works.update(1)  # 完成一个作品，更新上面那个条
            return res
        tasks = [work_task_wrapper(work_id) for work_id in work_ids]
        task_results: list[DownloadResult] = await asyncio.gather(*tasks)
        if pbar_works:
            pbar_works.close()
        download_result.total = len(tasks)
        for task_result in task_results:
            if task_result.total > 0 and task_result.total == task_result.success:
                download_result.success += 1  # 成功下载的作品数
            else:
                download_result.failed_unit.append(task_result)
            download_result.paths += task_result.paths
        return download_result

    @retry_on_error
    async def get_work_details(self, work_id: int) -> Optional[WorkDetail]:
        self.logger.debug(f'正在获取作品详情: {work_id}')
        work_details_json = await self.api.illust_detail(work_id)
        self.logger.debug(f'底层返回: {work_details_json}')
        if isinstance(work_details_json, str):
            raise PixivError(work_details_json)
        if work_details_json.get('error', None):
            raise PixivError(work_details_json)
        illust_detail_json = work_details_json['illust']
        # noinspection PyArgumentList
        return WorkDetail(
            id=illust_detail_json['id'],
            title=illust_detail_json['title'],
            type=illust_detail_json['type'],
            caption=illust_detail_json['caption'],
            user=User(
                id=illust_detail_json['user']['id'],
                name=illust_detail_json['user']['name'],
                account=illust_detail_json['user']['account'],
                profile_image_urls=illust_detail_json['user']['profile_image_urls'],
                is_followed=illust_detail_json['user']['is_followed'],
                is_accept_request=illust_detail_json['user']['is_accept_request']
            ),
            tags=[
                Tag(name=tag['name'], translated_name=tag.get('translated_name'))
                for tag in illust_detail_json['tags']
            ],
            create_date=illust_detail_json['create_date'],
            page_count=illust_detail_json['page_count'],
            width=illust_detail_json['width'],
            height=illust_detail_json['height'],
            meta_single_page=MetaSinglePage(
                original_image_url=illust_detail_json['meta_single_page'].get('original_image_url')
            ),
            meta_pages=[
                MetaPage(
                    image_urls=MetaPageImageUrls(
                        original=page['image_urls']['original']
                    )
                )
                for page in illust_detail_json['meta_pages']
            ],
            total_view=illust_detail_json['total_view'],
            total_bookmarks=illust_detail_json['total_bookmarks'],
            is_bookmarked=illust_detail_json['is_bookmarked'],
        )

    @retry_on_error
    async def get_user_works(self, user_id: int) -> list:
        user_works = await self.api.user_illusts(user_id)
        if isinstance(user_works, str):
            return []
        return [work['id'] for work in user_works['illusts']]

    @retry_on_error
    async def get_favs(self, user_id=88725668, hook_func: Optional[Callable[..., Awaitable]] = None) -> list:
        fav_list = []
        max_mark = None
        try:
            while True:
                favs: dict = await self.api.user_bookmarks_illust(user_id, max_bookmark_id=int(max_mark))
                next_url: str = favs['next_url']
                if not next_url:
                    return fav_list
                self.logger.info('收藏翻页中')
                index = next_url.find('max_bookmark_id=') + len('max_bookmark_id=')
                max_mark = next_url[index:]
                fav_list += [work['id'] for work in favs['illusts']]
                await asyncio.sleep(0.5)
                if hook_func:
                    await hook_func(favs)
        except KeyError:
            return fav_list
        except Exception as e:
            self.logger.error(f'获取收藏时发生错误: {e}')
            return []

    @retry_on_error
    async def get_new_works(self):
        work_list: dict = await self.api.illust_follow()
        try:
            return [work['id'] for work in work_list['illusts']]
        except KeyError:
            return [work_list]

    @retry_on_error
    async def get_ranking(self, tag_filter='day_male'):
        rank_json = await self.api.illust_ranking(tag_filter)
        return rank_json.get('illusts', [])

    @retry_on_error
    async def search_works(self, word,
                           match_type='part',
                           sort='date_desc',
                           time_dist='month',
                           start_date=None,
                           end_date=None,
                           min_marks=None,
                           offset=None):
        if offset == 0:
            offset = None
        if match_type == 'content':
            search_target = "title_and_caption"
        elif match_type == 'all':
            search_target = "exact_match_for_tags"
        else:
            search_target = "partial_match_for_tags"
        if start_date:
            pass
        if end_date:
            pass
        if time_dist == 'day':
            duration = 'within_last_day'
        elif time_dist == 'week':
            duration = 'within_last_week'
        else:
            duration = 'within_last_month'

        search_result = await self.api.search_illust(word,
                                                     search_target,
                                                     sort,
                                                     duration,
                                                     min_bookmarks=min_marks,
                                                     offset=offset)
        return search_result
