from ncatbot.plugin_system import NcatBotPlugin, on_group_at
from .config_proxy import ProxiedPluginConfig
from dataclasses import dataclass, field
from ncatbot.core.event import GroupMessageEvent, Reply
from ncatbot.core.event.message_segment.message_segment import Image, Text
from ncatbot.utils import get_log
import httpx
from pydantic import BaseModel, Field
from typing import Optional, Any
import asyncio

PLUGIN_NAME = "UnnamedImageSearchIntegrate"
logger = get_log(PLUGIN_NAME)


class UnifiedImageResult(BaseModel):
    """
    反向搜图统一结果模型
    设计目标：兼容 SauceNAO, TraceMoe, IQDB, Google Lens 等不同来源
    """
    # 核心元数据
    engine: str = Field(..., description="来源引擎名称，如 'SauceNAO'")
    similarity: float = Field(..., description="相似度百分比，0.0-100.0")
    # 内容描述
    title: Optional[str] = Field(None, description="作品标题")
    author: Optional[str] = Field(None, description="作者/画师/社团名称")
    # 链接资源
    source_url: Optional[str] = Field(None, description="原始来源链接 (如 Pixiv, Twitter 详情页)")
    thumbnail_url: Optional[str] = Field(None, description="缩略图链接")
    # 扩展字段：用于存储各引擎特有的、无法标准化的数据
    # 例如：SauceNAO 的 member_id, index_id; TraceMoe 的 episode, timestamp
    extra_info: dict[str, Any] = Field(default_factory=dict, description="引擎私有数据")

    class Config:
        # 允许 str 自动转 HttpUrl 等宽容模式，防止部分非标 URL 报错
        populate_by_name = True


class SauceNAOClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://saucenao.com/search.php"
        # 记录剩余配额，初始化为一个安全值
        self.short_remaining = 4
        self.long_remaining = 100
        # 并发锁，防止多协程同时修改状态或导致雪崩
        self._lock = asyncio.Lock()

    async def _handle_rate_limit(self):
        """
        限流控制器：在请求前检查状态
        如果短周期配额耗尽，强制休眠以平滑流量
        """
        async with self._lock:
            if self.long_remaining <= 0:
                raise RuntimeError("SauceNAO 24小时配额已耗尽")
            if self.short_remaining <= 1:
                # 激进策略：如果短周期仅剩1次或更少，主动休眠
                # SauceNAO 短周期通常是 30秒 4-6次，这里休眠 10秒缓冲
                logger.warning("SauceNAO rate limit approaching. Sleeping for 10s...")
                await asyncio.sleep(10)

    @staticmethod
    def _parse_sauce_response(raw_result: dict) -> UnifiedImageResult:
        """
        修正后的数据清洗层：适配 SauceNAO 多变的返回格式
        """
        header = raw_result.get("header", {})
        data = raw_result.get("data", {})
        # 1. 提取相似度 (JSON中是字符串 "14.93"，需要转 float)
        similarity = float(header.get("similarity", 0.0))
        # 2. 提取 URL
        # 注意：很多结果(如Index 0, 1)没有 ext_urls，或者 ext_urls 为 None
        ext_urls = data.get("ext_urls") or []
        source_url = ext_urls[0] if ext_urls else None
        # 3. 提取标题 (Title Logic Revised)
        # 优先级: title > source (对应Anime/Doujin) > jp_name > eng_name > material
        # 样例对照:
        #   Index 21 (Anime): 无title, source="One Piece" -> 命中
        #   Index 38 (H-Misc): 无title, source="The Costume 2" -> 命中
        #   Index 5 (Pixiv): 有title="trans ?" -> 命中
        title = (
                data.get("title") or
                data.get("source") or
                data.get("jp_name") or
                data.get("eng_name") or
                data.get("material")
        )
        # 4. 提取作者 (Author Logic Revised)
        # 处理 creator 是 list 的情况 (Index 38)
        creator_field = data.get("creator")
        if isinstance(creator_field, list) and creator_field:
            creator_str = creator_field[0]
        elif isinstance(creator_field, str):
            creator_str = creator_field
        else:
            creator_str = None
        # 优先级: member_name (Pixiv) > author_name (Danbooru) > user_name (Patreon) > creator_name (Skeb) > artist > creator
        author = (
                data.get("member_name") or
                data.get("author_name") or
                data.get("user_name") or
                data.get("creator_name") or
                data.get("artist") or
                creator_str
        )
        # 5. 组装 Extra Info
        # 自动提取所有 ID 结尾的字段 (pixiv_id, member_id, mal_id, getchu_id 等)
        # 以及关键元数据如 part (集数), est_time (时间戳), created_at (时间)
        extra = {
            "index_id": header.get("index_id"),
            "thumbnail": header.get("thumbnail"),  # 缩略图也可放这里备用
            "part": data.get("part"),  # 动画集数 或 漫画卷数
            "est_time": data.get("est_time"),  # 动画具体时间戳
            "type": data.get("type"),  # 资源类型 (Manga/Anime)
        }
        # 动态将 data 中所有 _id 结尾的字段放入 extra，避免硬编码
        for k, v in data.items():
            if k.endswith("_id") or k.endswith("_aid"):
                extra[k] = v
        return UnifiedImageResult(
            engine="SauceNAO",
            similarity=similarity,
            title=str(title) if title else None,
            author=str(author) if author else None,
            source_url=source_url,
            thumbnail_url=header.get("thumbnail"),
            extra_info=extra
        )

    async def search(self, image_url: str, min_similarity: float = 70.0) -> list[UnifiedImageResult]:
        """
        执行搜索 (纯 URL 模式)
        """
        # 1. 检查并执行限流等待
        await self._handle_rate_limit()
        params = {
            "api_key": self.api_key,
            "output_type": 2,  # JSON
            "testmode": 0,
            "db": 999,  # 全库搜索
            "numres": 10,
            "hide": 0,  # 隐藏明确的限制级内容
            "url": image_url  # URL 模式
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                # 使用 GET 发送 URL 搜索请求
                resp = await client.get(self.base_url, params=params)
                # 更新限流状态 (从 Header 读取权威数据)
                # Header Key 示例: 'X-RateLimit-Remaining-Short' (标准)
                # 但 SauceNAO 是在 JSON 响应体或者特殊的 Header 中，
                # SauceNAO 的 API 文档指出 Remaining 信息在 JSON 的 "header" 字段里，
                # 而不是 HTTP Headers。我们需要解析 JSON 后更新。
                resp.raise_for_status()
                json_body = resp.json()
                # 业务级状态码检查
                api_header = json_body.get("header", {})
                if api_header.get("status", 0) != 0:
                    # 可以在这里处理特定错误，如 -2 (Bad Image)
                    logger.error(f"SauceNAO API Error: {api_header.get('message')}")
                    return []
                # **关键：更新限流计数器**
                # SauceNAO 返回的是 remaining (剩余次数)
                self.short_remaining = int(api_header.get("short_remaining", 4))
                self.long_remaining = int(api_header.get("long_remaining", 100))
                # 解析结果
                results = json_body.get("results", [])
                parsed_results = []
                for res in results:
                    try:
                        model = self._parse_sauce_response(res)
                        if model.similarity >= min_similarity:
                            parsed_results.append(model)
                    except Exception as e:
                        logger.warning(f"Failed to parse a result item: {e}")
                        continue
                return parsed_results
            except httpx.HTTPStatusError as e:
                # 专门处理 429 Too Many Requests
                if e.response.status_code == 429:
                    logger.error("Hit HTTP 429. Forcing sleep.")
                    self.short_remaining = 0  # 强制下次请求休眠
                    await asyncio.sleep(30)  # 惩罚性休眠
                raise e
            except Exception as e:
                logger.error(f"Request failed: {e}")
                raise


@dataclass
class BasicSearchConfig(ProxiedPluginConfig):
    api_token: str = field(default='')
    min_similarity: float = field(default=70.0)


@dataclass
class SaucenaoConfig(BasicSearchConfig):
    pass


@dataclass
class ImageSearchConfig(ProxiedPluginConfig):
    saucenao_config: SaucenaoConfig = field(default_factory=SaucenaoConfig)


class UnnamedImageSearchIntegrate(NcatBotPlugin):
    name = PLUGIN_NAME  # 必须，插件名称，要求全局独立
    version = "0.0.1"  # 必须，插件版本
    dependencies = {}  # 必须，依赖的其他插件和版本
    description = "集成搜图功能"  # 可选
    author = "default_user"  # 可选

    saucenao_client: Optional[SauceNAOClient] = None
    search_config: Optional[ImageSearchConfig] = None

    async def on_load(self) -> None:
        self.search_config = ImageSearchConfig(self)
        if self.search_config.saucenao_config.api_token:
            logger.info(f'SauceNAO API Token: {self.search_config.saucenao_config.api_token}')
            self.saucenao_client = SauceNAOClient(self.search_config.saucenao_config.api_token)
        await super().on_load()

    async def on_close(self) -> None:
        await super().on_close()

    @on_group_at
    async def search_image(self, event: GroupMessageEvent):
        image_message: Optional[Image] = None
        has_command = False
        logger.debug(f'收到at消息, 开始解析')
        for message_segment in event.message:
            if message_segment.msg_seg_type == 'text':
                logger.debug(f'解析到文本消息段, 类型: {type(message_segment)}')
                assert isinstance(message_segment, Text)
                if message_segment.text.replace(' ', '') == 'si':
                    has_command = True
                continue
            if message_segment.msg_seg_type != 'reply':
                continue
            logger.debug(f'解析到引用消息段')
            assert isinstance(message_segment, Reply)
            cited_message = await self.api.get_msg(message_segment.id)
            logger.debug(f'获取到被引用消息id: {cited_message.message_id}')
            if len(cited_message.message.messages) != 1:
                logger.debug(f'被引用消息非单条消息, 数量: {len(cited_message.message.messages)}')
                continue
            if cited_message.message.messages[0].msg_seg_type != 'image':
                logger.debug(f'被引用消息非图片, 实际类型: {cited_message.message.messages[0].msg_seg_type}')
            logger.debug(f'引用消息类型校验通过, {type(cited_message.message.messages[0])}')
            single_cited_message = cited_message.message.messages[0]
            if not isinstance(single_cited_message, Image):
                logger.error('消息类型不匹配')
                raise AssertionError('消息类型不匹配')
            image_message = single_cited_message
        if image_message is None:
            logger.debug(f'未置值')
            return
        if not has_command:
            logger.debug(f'未使用指令')
            return
        logger.debug(f'通过所有消息校验')
        await event.reply(f'收到搜图请求')
        logger.debug(f'图片url为: {image_message.url}')
        if self.saucenao_client is None:
            await event.reply(f'未配置搜图引擎, 联系管理员')
            return
        image_url = image_message.url
        try:
            search_results = await self.saucenao_client.search(image_url, self.search_config.saucenao_config.min_similarity)
        except Exception as ise:
            await event.reply(f'搜图时发生错误')
            logger.exception('搜图错误', exc_info=ise)
            return
        if not search_results:
            await event.reply(f'没有结果')
            return
        await event.reply(f'作品标题: {search_results[0].title}\n链接:{search_results[0].source_url}')
