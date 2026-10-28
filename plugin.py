"""视频分镜插件入口。

拦截入站消息里的视频（适配器会把它转成 ``[视频] 文件: ...，链接: ...`` 的文本），
下载后用 ffmpeg 抽帧、Pillow 拼成一张带编号与时间戳的分镜图，
再交给宿主的视觉模型读成一段中文，作为文本段注入消息。

让只有文本能力的聊天模型也能"看"群里发的视频。
"""

from __future__ import annotations

import base64
import io
import json
import re
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

_SUSPECT_WORDS = (
    "擦边", "福利", "写真", "私房", "露点", "裸露", "走光", "内衣", "低胸", "制服诱惑",
    "色情", "援交", "涩涩", "涩图", "开车", "里番", "本子", "cos福利", "勾引",
    "睡", "床照", "湿身", "真空", "巨乳", "爆乳", "下衣失踪", "腿照", "泳装",
)

PLUGIN_VERSION = "1.7.0"


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
            "插件会先审一遍封面，合适才发图，然后发一段「为什么转这个 + UP 主是谁 + 标题简介 + 链接」。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="BV 号、视频链接，或搜索关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="你为什么挑这个（一句话，会原样显示在卡片最上面），比如「播放量最高、猫梗最正」",
                required=False,
            ),
        ],
        visibility="deferred",
    )
    async def handle_bili_share(
        self,
        target: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
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

        cfg = self.config.share
        storyboard = ""
        if cfg.watch_before_share and self._processor is not None:
            try:
                storyboard = await self._processor.describe_url(info["url"])
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("转发前看片失败（%s）：%s", bvid, str(exc)[:80])
        if storyboard:
            self.ctx.logger.info("转发前先看了：%s（%d 字）", bvid, len(storyboard))

        body = await self._compose_caption(info, storyboard, reason)
        title = info["title"]
        shown = title if ("《" in title or "「" in title) else f"《{title}》"
        caption = f"{body}\n{shown}\n{info['url']}"

        image_b64 = ""
        if info.get("cover"):
            jpeg = await self._prepare_cover(info["cover"], max_edge=cfg.max_cover_edge)
            if jpeg:
                if cfg.review_cover:
                    context_text = f"{info['title']} {info.get('desc') or ''}"
                    hit = next((w for w in _SUSPECT_WORDS if w in context_text), "")
                    if hit:
                        ok, why = False, f"标题/简介含敏感词「{hit}」"
                    else:
                        ok, why = await self._review_cover(
                            jpeg,
                            task_name=cfg.review_task,
                            model_name=cfg.review_model,
                            context_text=context_text,
                        )
                    if not ok:
                        self.ctx.logger.info("封面没过审，只发文字：%s（%s）", info["title"][:30], why)
                        jpeg = b""
                if jpeg:
                    image_b64 = base64.b64encode(jpeg).decode("ascii")

        sent_image = False
        if image_b64:
            try:
                await self.ctx.send.hybrid(
                    [
                        {"type": "image", "content": image_b64},
                        {"type": "text", "content": caption},
                    ],
                    stream_id,
                )
                sent_image = True
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("封面图文混排失败，退回纯文字：%s", exc)
        if not sent_image:
            await self.ctx.send.text(caption, stream_id)
        self.ctx.logger.info("B站转发：%s %s（图=%s）", bvid, info["title"][:40], sent_image)
        return {
            "success": True,
            "content": f"转过去了：{info['title']}（{bvid}）" + ("" if sent_image else "，封面没过审就没发图"),
        }

    async def _compose_caption(self, info: dict[str, Any], storyboard: str, reason: str) -> str:
        """用自己的口气写卡片正文：不套模板标签，看过片就按看的写。"""

        cfg = self.config.share
        desc = (info.get("desc") or "")[:200]
        lines = [
            f"标题：{info['title']}",
            f"UP 主：{info.get('author') or '未知'}",
        ]
        if desc:
            lines.append(f"简介：{desc}")
        if reason.strip():
            lines.append(f"你挑它的理由：{reason.strip()[:80]}")
        if storyboard:
            lines.append(f"你看完的分镜描述：{storyboard[:1200]}")
        else:
            lines.append("（你没看成分镜，只能按标题和简介说，别装作看过）")

        prompt: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "你在聊天里给朋友转一个视频。用你平时的口气写两三句话（不超过"
                    f"{cfg.caption_max_chars} 字），说清这是什么、你为什么想转给他。"
                    "看过分镜就按看到的画面讲，可以点一个具体细节；没看成就别装看过。"
                    "禁止：写「为什么转这个」「UP 主」「标题」这类标签，禁止分点列举，禁止复述整段剧情，"
                    "禁止「这是一段…的视频」这种报告腔。直接说话，不加开场白。"
                ),
            },
            {"role": "user", "content": "\n".join(lines)},
        ]
        call_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "temperature": 0.8,
            "max_tokens": 400,
        }
        if cfg.caption_task:
            call_kwargs["task_name"] = cfg.caption_task
        if cfg.caption_model:
            call_kwargs["model_name"] = cfg.caption_model
        try:
            result = await self.ctx.llm.generate(**call_kwargs)
            text = str((result or {}).get("response") or "").strip()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("卡片文案生成失败，用兜底写法：%s", str(exc)[:60])
            text = ""
        if not text:
            text = f"这个挺乐的，你看看" if not storyboard else storyboard[: cfg.caption_max_chars]
        text = text.strip().strip('"“”')
        return text[: cfg.caption_max_chars]

    async def _prepare_cover(self, url: str, *, max_edge: int) -> bytes:
        """下封面并缩到合理尺寸，返回 JPEG 字节；失败给空。"""

        try:
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(url, headers={"Referer": "https://www.bilibili.com/"}) as response:
                    if response.status != 200:
                        return b""
                    raw = await response.read()
        except Exception:  # noqa: BLE001
            return b""
        if not raw or len(raw) > 6 * 1024 * 1024:
            return b""
        try:
            from PIL import Image  # noqa: PLC0415 - 只有转发时才需要

            with Image.open(io.BytesIO(raw)) as image:
                image = image.convert("RGB")
                image.thumbnail((max_edge, max_edge))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85, optimize=True)
                out = buffer.getvalue()
        except Exception:  # noqa: BLE001
            return b""
        return out if out and len(out) <= 4 * 1024 * 1024 else b""

    async def _review_cover(
        self,
        jpeg: bytes,
        *,
        task_name: str,
        model_name: str,
        context_text: str = "",
    ) -> tuple[bool, str]:
        """用视觉模型审封面：擦边、血腥、敏感一律不发。解析失败按「不通过」处理。"""

        prompt: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "你是聊天群的内容审核员，标准偏严：宁可错杀，不要放过。"
                    "先看画面本身，再读画面里的文字，最后结合给出的标题/简介，给这张图打 0-100 的风险分。"
                    "以下情况一律算高风险（≥60）：裸露或衣着暴露（低胸、深V、内衣、泳装、胸口/大腿特写）、"
                    "暧昧姿势或表情、二次元角色的擦边构图（同样适用，不因为是动画就放宽）、"
                    "画面内出现性暗示文字（如「想和你睡觉」「福利」「私房」）、血腥暴力、政治敏感、违法犯罪、猎奇不适。"
                    "以下算中风险（30-59）：软色情暗示、暧昧双关台词、角色姿势暧昧但衣着正常。"
                    "0-29：正常人物、动物、风景、游戏截图、表情包、普通文字图。"
                    '只输出一行 JSON：{"ok": true, "risk": 10, "why": ""} 或 {"ok": false, "risk": 75, "why": "原因"}。'
                    "ok 为 true 仅当风险分 < 30。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "这张图能直接发到群里吗？"
                            + (f"（视频标题/简介：{context_text[:120]}）" if context_text else "")
                        ),
                    },
                    {
                        "type": "image",
                        "image_format": "jpeg",
                        "image_base64": base64.b64encode(jpeg).decode("ascii"),
                    },
                ],
            },
        ]
        call_kwargs: dict[str, Any] = {"prompt": prompt, "temperature": 0.1, "max_tokens": 200}
        if task_name:
            call_kwargs["task_name"] = task_name
        if model_name:
            call_kwargs["model_name"] = model_name
        try:
            result = await self.ctx.llm.generate(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            return False, f"审核调用失败：{str(exc)[:60]}"
        if not isinstance(result, dict) or not bool(result.get("success", False)):
            return False, str((result or {}).get("error") or "审核没有返回结果")[:60]
        text = str(result.get("response") or "").strip()
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return False, f"审核输出看不懂：{text[:40]}"
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            return False, f"审核输出不是 JSON：{text[:40]}"
        why = str(payload.get("why") or "")[:80]
        ok = bool(payload.get("ok"))
        try:
            risk = int(payload.get("risk", 0))
        except (TypeError, ValueError):
            risk = 0
        if not ok or risk >= 30:  # 双重保险：模型嘴上说通过但风险分不低的，也不发
            return False, why or f"风险分 {risk}"
        return True, why

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
