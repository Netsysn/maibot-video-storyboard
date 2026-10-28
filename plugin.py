"""视频分镜插件入口。

拦截入站消息里的视频（适配器会把它转成 ``[视频] 文件: ...，链接: ...`` 的文本），
下载后用 ffmpeg 抽帧、Pillow 拼成一张带编号与时间戳的分镜图，
再交给宿主的视觉模型读成一段中文，作为文本段注入消息。

让只有文本能力的聊天模型也能"看"群里发的视频。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import aiohttp

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .bili import extract_bvid, search_videos, video_info
from .config import VideoStoryboardConfig
from .pipeline import CACHE_FILE_NAME, VideoStoryboardProcessor
from .storyboard import StoryboardError, resolve_executable

HOOK_TIMEOUT_MS = 90000
PLUGIN_VERSION = "1.5.0"


class VideoStoryboardPlugin(MaiBotPlugin):
    """视频分镜插件。"""

    config_model = VideoStoryboardConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._processor: VideoStoryboardProcessor | None = None

    async def on_load(self) -> None:
        """插件加载：建处理器、载入缓存、检查 ffmpeg。"""

        raw_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", "") or ""
        data_dir = Path(raw_dir) if str(raw_dir).strip() else Path(__file__).resolve().parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_path = data_dir / CACHE_FILE_NAME if bool(self.config.cache.enabled) else None
        self._processor = VideoStoryboardProcessor(
            config=self.config,
            llm_generate=self.ctx.llm.generate,
            logger=self.ctx.logger,
            data_dir=data_dir,
            cache_path=cache_path,
        )
        await self._processor.initialize()

        ffmpeg_state = "ffmpeg 未找到（视频将无法处理）"
        try:
            resolved = resolve_executable(
                str(self.config.storyboard.ffmpeg_path),
                search_roots=self._processor.search_roots(),
            )
            ffmpeg_state = f"ffmpeg={resolved}"
        except StoryboardError as exc:
            self.ctx.logger.warning("视频分镜：%s", exc)

        self.ctx.logger.info(
            "视频分镜 v%s 已加载（%s，%s 格，每张最多看 %s 秒，视觉任务=%s）",
            PLUGIN_VERSION,
            ffmpeg_state,
            self.config.storyboard.cells,
            self.config.storyboard.max_seconds,
            self.config.vlm.task_name or "auto",
        )
        if int(self.config.vlm.max_tokens) < 1024:
            self.ctx.logger.warning(
                "视频分镜：vlm.max_tokens 当前为 %s，思考模型可能把额度吃光导致空描述，建议 2048 以上",
                self.config.vlm.max_tokens,
            )

    async def on_unload(self) -> None:
        """插件卸载：保存缓存。"""

        if self._processor is not None:
            await self._processor.close()
            self._processor = None
        self.ctx.logger.info("视频分镜插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热更新。"""

        del config_data, version
        if scope != CONFIG_RELOAD_SCOPE_SELF or self._processor is None:
            return
        await self._processor.update_config(self.config)
        self.ctx.logger.info("视频分镜配置已更新")

    @HookHandler(
        "chat.receive.before_process",
        name="video_storyboard_hook",
        description="识别入站消息里的视频，抽帧拼成带时间戳的分镜图，用视觉模型生成描述并注入消息文本。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_process(
        self,
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """在消息进入宿主处理链之前补充视频描述。"""

        del kwargs
        processor = self._processor
        if processor is None or not isinstance(message, dict):
            return None

        try:
            mutated = await processor.process_message(message)
        except Exception:  # noqa: BLE001 - Hook 里抛异常会打断消息链，这里必须吞
            self.ctx.logger.warning("处理入站消息中的视频时出现异常", exc_info=True)
            return None

        if mutated is None:
            return None
        return {"action": "continue", "modified_kwargs": {"message": mutated}}

    @Tool(
        "video_storyboard",
        description=(
            "看视频：给一个视频链接（B站/抖音/小红书/YouTube 等），插件会下载它、抽帧拼成带时间戳的分镜图，"
            "再用视觉模型读成中文描述返回。当聊天里有人发了视频链接、或你想评价某个视频却没看到内容时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="视频链接，例如 https://b23.tv/xxxx 或 https://www.bilibili.com/video/BVxxxx",
                required=True,
            ),
        ],
        visibility="deferred",
    )
    async def handle_video_storyboard(self, url: str = "", **kwargs: Any) -> dict[str, Any]:
        """工具：按链接看一段视频。"""

        del kwargs
        processor = self._processor
        if processor is None:
            return {"success": False, "content": "视频分镜还没准备好，等会儿再试"}
        link = str(url or "").strip()
        if not link:
            return {"success": False, "content": "没给链接"}
        self.ctx.logger.info("视频分镜：工具调用 %s", link[:120])
        try:
            text = await processor.describe_url(link)
        except Exception as exc:  # noqa: BLE001 - 工具失败要变成可读文本
            self.ctx.logger.warning("视频分镜工具失败（%s）：%s", link[:80], exc)
            return {"success": False, "content": f"没看成：{str(exc)[:80]}"}
        return {"success": True, "content": text}

    @Tool(
        "bili_search",
        description=(
            "在 B 站按关键词搜视频，返回候选列表（标题、时长、播放量、UP 主、链接）。"
            "当有人让你搜视频、找个片子，或者你想自己挑一个视频来发/看时，先用它拿候选。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="搜索词，例如「哈基米 猫」「原神 新版本」",
                required=True,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回几个候选，默认 5，最多 10",
                required=False,
            ),
        ],
        visibility="deferred",
    )
    async def handle_bili_search(self, keyword: str = "", limit: int = 5, **kwargs: Any) -> dict[str, Any]:
        """工具：按关键词搜 B 站视频。"""

        del kwargs
        word = str(keyword or "").strip()
        if not word:
            return {"success": False, "content": "没给搜索词"}
        self.ctx.logger.info("B站搜索：%s（%s 条）", word, limit)
        try:
            items = await search_videos(word, limit=max(1, min(int(limit or 5), 10)))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("B站搜索失败：%s", exc)
            return {"success": False, "content": f"搜不动：{str(exc)[:80]}"}
        if not items:
            return {"success": False, "content": f"没搜到关于「{word}」的视频"}
        lines = []
        for index, item in enumerate(items, 1):
            minutes = item["seconds"] // 60
            seconds = item["seconds"] % 60
            lines.append(
                f"{index}. {item['title']}（{minutes}:{seconds:02d}，播放 {item['play']:,}，UP {item['author']}）"
                f" {item['url']}"
            )
        return {
            "success": True,
            "content": f"搜到 {len(items)} 个，自己挑一个：\n" + "\n".join(lines),
            "items": [{k: v for k, v in item.items() if k != "description"} for item in items],
        }

    @Tool(
        "bili_share",
        description=(
            "把一条 B 站视频转到当前聊天：给 BV 号/链接，或者给关键词（会先搜再挑最合适的那个）。"
            "插件会发一张封面图 + 标题、时长、UP 主、链接，就像转了个视频卡片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="BV 号、视频链接，或搜索关键词",
                required=True,
            ),
        ],
        visibility="deferred",
    )
    async def handle_bili_share(self, target: str = "", **kwargs: Any) -> dict[str, Any]:
        """工具：把 B 站视频以封面卡片的形式转发到当前会话。"""

        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
        raw = str(target or "").strip()
        if not raw:
            return {"success": False, "content": "没给要转的视频"}
        bvid = extract_bvid(raw)
        if not bvid and raw.startswith("http"):
            items = await search_videos(raw, limit=1)
            bvid = items[0]["bvid"] if items else ""
        if not bvid:
            items = await search_videos(raw, limit=5)
            bvid = items[0]["bvid"] if items else ""
        if not bvid:
            return {"success": False, "content": f"没找到「{raw}」这条视频"}

        info = await video_info(bvid)
        if not info:
            return {"success": False, "content": "这条视频的信息拿不到，可能删了或者被限流"}
        minutes, seconds = divmod(int(info["duration"]), 60)
        caption = f"{info['title']}\n{minutes}:{seconds:02d} · UP {info['author']}\n{info['url']}"
        sent_image = False
        if info.get("cover"):
            data = await self._download_image(info["cover"])
            if data:
                try:
                    await self.ctx.send.hybrid(
                        [
                            {"type": "image", "content": data},
                            {"type": "text", "content": caption},
                        ],
                        stream_id,
                    )
                    sent_image = True
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("封面图文混排失败，退回纯文字：%s", exc)
        if not sent_image:
            await self.ctx.send.text(caption, stream_id)
        self.ctx.logger.info("B站转发：%s %s", bvid, info["title"][:40])
        return {"success": True, "content": f"转过去了：{info['title']}（{bvid}）"}

    async def _download_image(self, url: str) -> str:
        """把封面图下回来转成 base64，失败返回空串。"""

        try:
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(url, headers={"Referer": "https://www.bilibili.com/"}) as response:
                    if response.status != 200:
                        return ""
                    raw = await response.read()
        except Exception:  # noqa: BLE001
            return ""
        if not raw or len(raw) > 4 * 1024 * 1024:
            return ""
        return base64.b64encode(raw).decode("ascii")

    @Command(
        "video_storyboard_now",
        pattern=r"^/(?:看视频|看片|video)\s+(?P<url>\S+)$",
        description="立刻看一个视频链接（如 /看视频 https://b23.tv/xxx），返回分镜描述。",
    )
    async def cmd_watch_video(self, **kwargs: Any) -> tuple[bool, str, int]:
        """手动让插件看一个视频链接。"""

        matched = kwargs.get("matched_groups") or {}
        link = str(matched.get("url") or "").strip()
        processor = self._processor
        if processor is None:
            return False, "视频分镜还没准备好", 1
        if not link:
            return False, "用 /看视频 <链接> 的格式发给我", 1
        self.ctx.logger.info("视频分镜：命令调用 %s", link[:120])
        try:
            text = await processor.describe_url(link)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("视频分镜命令失败（%s）：%s", link[:80], exc)
            return False, f"没看成：{str(exc)[:80]}", 1
        return True, text, 1


def create_plugin() -> MaiBotPlugin:
    """插件工厂函数，供 Runner 调用。"""

    return VideoStoryboardPlugin()
