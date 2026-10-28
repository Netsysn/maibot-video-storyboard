"""视频分镜的处理流水线：从消息组件里认出视频 → 取文件 → 抽帧拼图 → 视觉描述 → 注入文本。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import aiohttp

from .bili import extract_bvid, fetch_extras
from .linkvid import LinkVideoError, download_video, find_links
from .storyboard import (
    StoryboardError,
    build_sheet,
    clean_frames,
    extract_frames,
    resolve_executable,
)
from .vlm import VlmError, describe_storyboard

VIDEO_MARKER = "[视频]"
"""适配器把 QQ 视频转成文本时用的标记。"""

_VIDEO_MARKER_RE = re.compile(r"\[\s*视频(?:文件)?\s*\]")
"""兼容适配器可能写出的 ``[视频]`` / ``[视频文件]`` / ``[ 视频 ]``。"""

_SAFE_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".flv", ".ts", ".avi", ".3gp"})
"""允许落盘的视频扩展名；其它一律按 mp4 处理，避免写出可执行文件。"""

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
"""直链下载时禁止碰的内网段（防 SSRF）。"""


def _alias_key(url: str) -> str:
    """把落地页 URL 归成「按视频 ID」的缓存键；拿不到 ID 就返回空串。"""

    bvid = extract_bvid(url or "")
    if bvid:
        return f"bv:{bvid}"
    return ""

INJECT_PREFIX = "[视频内容]"
"""注入到消息里的描述前缀。"""

CACHE_FILE_NAME = "video_storyboard_cache.json"
"""描述缓存文件名（放在插件数据目录下）。"""

TOTAL_BUDGET_SECONDS = 75.0
"""单条消息允许花在视频上的总时间，超过就不再尝试（避免拖住整条消息）。"""


@dataclass(frozen=True)
class VideoSource:
    """一条视频消息里能拿到的东西。"""

    file_name: str
    url: str
    path: str
    file_id: str

    @property
    def cache_key(self) -> str:
        """用于缓存描述的稳定键。

        优先级：``file_id``（QQ 里同一个文件 id 不变）→ BV 号（B 站换了短链也能对上）
        → 本地路径 → 去掉查询串的 URL。QQ 的 CDN 链接带签名参数、每条消息都不同，
        直接拿整条 URL 做键会几乎永远命中不了。
        """

        raw = ""
        if self.file_id:
            raw = f"id:{self.file_id}"
        elif self.url:
            bvid = extract_bvid(self.url)
            if bvid:
                raw = f"bv:{bvid}"
        if not raw and self.path:
            raw = f"path:{self.path}"
        if not raw and self.url:
            parts = urlsplit(self.url)
            raw = f"url:{parts.netloc}{parts.path}"
        if not raw:
            raw = "unknown"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @property
    def safe_name(self) -> str:
        """只保留文件名本身，并强制成允许的视频扩展名。

        消息里的文件名是外部可控的，直接拼进临时目录会路径穿越（``../../x``）。
        """

        base = Path(str(self.file_name or "").replace("\\", "/")).name
        if not base:
            return f"video_{self.cache_key}.mp4"
        stem, dot, suffix = base.rpartition(".")
        if not dot or not stem:
            stem, suffix = base, "mp4"
        suffix = suffix.lower()
        if f".{suffix}" not in _SAFE_EXTENSIONS:
            suffix = "mp4"
        cleaned = re.sub(r"[^\w\-. ()\u4e00-\u9fff]+", "_", stem).strip(" .") or f"video_{self.cache_key}"
        return f"{cleaned[:80]}.{suffix}"

    @property
    def label(self) -> str:
        """日志里显示的短名字。"""

        if self.file_name:
            return self.file_name
        if self.url:
            tail = self.url.split("//", 1)[-1]
            return tail[:40] if tail else "视频链接"
        return Path(self.path).name or "视频"


def parse_video_text(text: str) -> VideoSource | None:
    """从 ``[视频] 文件: a.mp4，大小: 123，链接: http://...`` 里解析出各字段。"""

    match = _VIDEO_MARKER_RE.search(text or "")
    if match is None:
        return None

    tail = text[match.end() :]
    fields: dict[str, str] = {}
    for piece in tail.replace("，", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        for key in ("文件", "大小", "链接", "路径", "文件ID", "文件id"):
            prefix = f"{key}:"
            if piece.startswith(prefix):
                fields[key] = piece[len(prefix) :].strip()
                break

    source = VideoSource(
        file_name=fields.get("文件", ""),
        url=fields.get("链接", ""),
        path=fields.get("路径", ""),
        file_id=fields.get("文件ID", "") or fields.get("文件id", ""),
    )
    if not (source.url or source.path or source.file_id):
        return None
    return source


class DescriptionCache:
    """把「这个视频已经看过什么」存成一个小 JSON。"""

    def __init__(self, path: Path, *, max_entries: int, logger: logging.Logger) -> None:
        self._path = path
        self._max_entries = max(10, int(max_entries))
        self._logger = logger
        self._data: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        """从磁盘读缓存；坏文件直接忽略。"""

        try:
            if not self._path.is_file():
                return
        except OSError:
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存坏了不该影响主流程
            self._logger.warning("视频分镜缓存读取失败，忽略：%s", self._path)
            return
        if isinstance(payload, dict):
            entries = payload.get("entries")
            if isinstance(entries, dict):
                self._data = {str(k): v for k, v in entries.items() if isinstance(v, dict)}

    async def save(self) -> None:
        """写回磁盘（放到线程里，别在事件循环上做同步 IO）。"""

        payload = json.dumps({"version": 1, "entries": self._data}, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._path.write_text, payload, "utf-8")
        except Exception:  # noqa: BLE001 - 缓存写失败只记日志
            self._logger.warning("视频分镜缓存写入失败：%s", self._path, exc_info=True)

    def get(self, key: str) -> str | None:
        """取已缓存的描述。"""

        entry = self._data.get(key)
        if not entry:
            return None
        desc = str(entry.get("description") or "").strip()
        return desc or None

    def put(self, key: str, *, description: str, label: str, meta: str) -> None:
        """写入一条描述，超量时丢掉最旧的。"""

        self._data[key] = {
            "description": description,
            "label": label,
            "meta": meta,
            "created_at": time.time(),
        }
        if len(self._data) > self._max_entries:
            ordered = sorted(self._data.items(), key=lambda kv: float(kv[1].get("created_at") or 0))
            for stale_key, _ in ordered[: len(self._data) - self._max_entries]:
                self._data.pop(stale_key, None)


class VideoStoryboardProcessor:
    """把一条入站消息里的视频换成「原组件 + 分镜描述」。"""

    def __init__(
        self,
        *,
        config: Any,
        llm_generate: Callable[..., Awaitable[Any]],
        logger: logging.Logger,
        data_dir: Path,
        cache_path: Path | None,
    ) -> None:
        self._config = config
        self._llm_generate = llm_generate
        self._logger = logger
        self._data_dir = data_dir
        self._cache_path = cache_path
        self._cache = self._build_cache(cache_path, config)
        self._semaphore = asyncio.Semaphore(max(1, int(getattr(config.trigger, "max_concurrency", 2) or 2)))
        self._warned_no_group = False

    async def initialize(self) -> None:
        """加载缓存。"""

        if self._cache is not None:
            await self._cache.load()

    async def close(self) -> None:
        """保存缓存。"""

        if self._cache is not None:
            await self._cache.save()

    async def update_config(self, config: Any) -> None:
        """配置热更新后重建缓存。"""

        old_cache = self._cache
        self._config = config
        if old_cache is not None:
            await old_cache.save()
        self._cache = self._build_cache(self._cache_path, config)
        if self._cache is not None:
            await self._cache.load()

    def _build_cache(self, cache_path: Path | None, config: Any) -> DescriptionCache | None:
        """按配置创建缓存对象。"""

        if cache_path is None or not bool(config.cache.enabled):
            return None
        return DescriptionCache(
            cache_path,
            max_entries=int(config.cache.max_entries),
            logger=self._logger,
        )

    async def flush(self) -> None:
        """把缓存刷到磁盘。"""

        if self._cache is not None:
            await self._cache.save()

    async def process_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条入站消息，返回注入描述后的副本；无需改动时返回 ``None``。"""

        cfg = self._config
        # 宿主的 [plugin] enabled 是保留键，不一定会出现在配置模型里，缺省按开启处理
        if not bool(getattr(cfg.plugin, "enabled", True)):
            return None
        mode = str(cfg.trigger.trigger_mode or "all").strip().lower()
        if mode == "off":
            return None
        if not self._group_allowed(message):
            return None
        if mode == "at" and not (bool(message.get("is_mentioned")) or bool(message.get("is_at"))):
            return None

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list) or not raw_message:
            return None

        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        budget = {"remaining": max(1, int(cfg.trigger.max_videos_per_message))}
        new_raw, changed = await self._process_components(raw_message, budget, deadline)
        if not changed:
            return None

        mutated = dict(message)
        mutated["raw_message"] = new_raw
        return mutated

    def _group_allowed(self, message: Mapping[str, Any]) -> bool:
        """按群白/黑名单过滤。

        拿不到群号时原来是静默放行；现在按配置决定，并把这种情况记一条日志，
        否则「白名单明明开了却还在别的群看视频」这种问题在日志里查不到线索。
        """

        cfg = self._config
        allow = {str(item).strip() for item in (cfg.trigger.allow_groups or []) if str(item).strip()}
        deny = {str(item).strip() for item in (cfg.trigger.deny_groups or []) if str(item).strip()}
        message_info = message.get("message_info")
        group_info = message_info.get("group_info") if isinstance(message_info, Mapping) else None
        group_id = ""
        if isinstance(group_info, Mapping):
            group_id = str(group_info.get("group_id") or "").strip()
        if not group_id:
            if (allow or deny) and not self._warned_no_group:
                self._warned_no_group = True
                self._logger.warning(
                    "视频分镜：这条消息里拿不到 group_id，群名单无法生效（按 %s 处理）。"
                    "如果你配了群白名单，检查适配器是否把 group_info 传进 hook。",
                    "放行" if bool(getattr(cfg.trigger, "unknown_group_allow", True)) else "拒绝",
                )
            return bool(getattr(cfg.trigger, "unknown_group_allow", True))
        if group_id in deny:
            return False
        if allow and group_id not in allow:
            return False
        return True

    async def _process_components(
        self,
        components: list[Any],
        budget: dict[str, int],
        deadline: float,
    ) -> tuple[list[Any], bool]:
        """遍历组件，给视频补一条描述文本。"""

        changed = False
        output: list[Any] = []
        for component in components:
            output.append(component)
            if not isinstance(component, Mapping):
                continue

            component_type = str(component.get("type") or "")
            source: VideoSource | None = None
            if component_type == "text":
                text = str(component.get("data") or "")
                source = parse_video_text(text)
                if source is None:
                    source = self._source_from_link(text)
            elif component_type == "video":
                source = self._source_from_video_component(component)

            if source is None:
                continue
            if budget["remaining"] <= 0:
                continue
            if deadline - time.monotonic() <= 5:
                self._logger.info("视频分镜：剩余时间不足，跳过 %s", source.label)
                continue

            budget["remaining"] -= 1
            description = await self._describe(source, deadline)
            if not description:
                continue
            output.append({"type": "text", "data": f"{INJECT_PREFIX} {description}"})
            changed = True

        return output, changed

    def _source_from_link(self, text: str) -> VideoSource | None:
        """消息里贴了视频站点链接（B 站等）时，转成一个待下载的源。"""

        cfg = self._config
        if not bool(getattr(cfg.trigger, "link_videos", True)):
            return None
        domains = list(getattr(cfg.trigger, "link_domains", []) or [])
        links = find_links(text, domains, limit=1)
        if not links:
            return None
        return VideoSource(file_name="", url=links[0], path="", file_id="")

    def _source_from_video_component(self, component: Mapping[str, Any]) -> VideoSource | None:
        """兼容真正出现 ``video`` 组件的情况。"""

        data = component.get("data")
        if isinstance(data, Mapping):
            url = str(data.get("url") or "").strip()
            path = str(data.get("path") or "").strip()
            file_id = str(data.get("file_id") or "").strip()
            file_name = str(data.get("file") or data.get("name") or "").strip()
            if url or path or file_id:
                return VideoSource(file_name=file_name, url=url, path=path, file_id=file_id)
        return None

    async def _describe(self, source: VideoSource, deadline: float) -> str | None:
        """对一个视频产出描述文本。"""

        cached = self._cache.get(source.cache_key) if self._cache is not None else None
        if cached:
            self._logger.info("视频分镜：命中缓存 %s", source.label)
            return cached

        # 排队等待也要有上限：消息已经等了很久就别再拖了
        wait_started = time.monotonic()
        wait_budget = max(5.0, min(60.0, deadline - wait_started))
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=wait_budget)
        except (asyncio.TimeoutError, TimeoutError):
            self._logger.info("视频分镜：排队超过 %.0fs，先不看 %s", wait_budget, source.label)
            return None

        # 排队花掉的时间不算在「看视频」的预算里，否则并发一多就必然超时
        deadline += time.monotonic() - wait_started
        try:
            try:
                description, meta, webpage_url = await self._run_pipeline(source, deadline)
            except (StoryboardError, VlmError, LinkVideoError) as exc:
                self._logger.warning("视频分镜失败（%s）：%s", source.label, exc)
                if bool(self._config.trigger.report_failure):
                    return f"（这段视频我没看清：{str(exc)[:40]}）"
                return None
            except Exception:  # noqa: BLE001 - 任何意外都不该炸掉消息链
                self._logger.warning("视频分镜异常（%s）", source.label, exc_info=True)
                if bool(self._config.trigger.report_failure):
                    return "（这段视频我没看清）"
                return None
        finally:
            self._semaphore.release()

        if self._cache is not None:
            self._cache.put(
                source.cache_key,
                description=description,
                label=source.label,
                meta=meta,
            )
            # 短链跳转出来的长链（含 BV 号）也记一份，下次换个短链分享同一条视频能直接命中
            alias = _alias_key(webpage_url)
            if alias and alias != source.cache_key:
                self._cache.put(alias, description=description, label=source.label, meta=meta)
            await self._cache.save()
        self._logger.info("视频分镜完成（%s）：%s", source.label, description[:40])
        return description

    async def _run_pipeline(self, source: VideoSource, deadline: float) -> tuple[str, str, str]:
        """取文件 → 抽帧 → 拼图 → 让视觉模型读。返回 ``(描述, 元信息, 落地页 URL)``。"""

        cfg = self._config
        temp_root = Path(tempfile.mkdtemp(prefix="maibot-video-"))
        video_path: Path | None = None
        samples: list[Any] = []
        try:
            video_path, title, transcript, extras, webpage_url = await self._fetch(source, temp_root)
            ffmpeg, ffprobe = self._resolve_tools()
            timeout = float(cfg.storyboard.ffmpeg_timeout_seconds)

            samples, meta = await asyncio.to_thread(
                extract_frames,
                ffmpeg,
                ffprobe,
                video_path,
                temp_root / "frames",
                cells=int(cfg.storyboard.cells),
                max_seconds=int(cfg.storyboard.max_seconds),
                cell_edge=int(cfg.storyboard.cell_edge),
                timeout=timeout,
            )
            if meta.duration < float(cfg.trigger.skip_short_seconds):
                raise StoryboardError(f"视频太短（{meta.duration:.1f}s）")

            span = min(meta.duration, float(cfg.storyboard.max_seconds))
            sheet_bytes = await asyncio.to_thread(
                build_sheet,
                samples,
                columns=int(cfg.storyboard.columns),
                cell_edge=int(cfg.storyboard.cell_edge),
                font_path=str(cfg.storyboard.font_path),
                show_timestamp=bool(cfg.storyboard.show_timestamp),
                jpeg_quality=int(cfg.storyboard.jpeg_quality),
                header=f"{source.label} · {meta.summary} · {len(samples)} 格",
            )

            if deadline - time.monotonic() < 5:
                raise StoryboardError("时间不够了，先不看")

            description = await describe_storyboard(
                self._llm_generate,
                sheet_bytes=sheet_bytes,
                cells=len(samples),
                duration=meta.duration,
                span=span,
                width=meta.width,
                height=meta.height,
                has_audio=meta.has_audio,
                task_name=str(cfg.vlm.task_name or ""),
                model_name=str(cfg.vlm.model_name or ""),
                temperature=float(cfg.vlm.temperature),
                max_tokens=int(cfg.vlm.max_tokens),
                max_chars=int(cfg.vlm.max_chars),
                style=str(cfg.vlm.style or "chat"),
                title=title,
                transcript=transcript,
                extras=extras,
            )
            return description, meta.summary, webpage_url
        finally:
            if not bool(cfg.debug.keep_temp_files):
                clean_frames([sample.path for sample in samples])
                await asyncio.to_thread(shutil.rmtree, temp_root, True)
            else:
                self._logger.info("视频分镜：临时文件保留在 %s", temp_root)

    async def describe_url(self, url: str, *, timeout_seconds: float | None = None) -> str:
        """给一个视频链接出描述（工具调用入口，不注入消息）。"""

        link = (url or "").strip()
        if not link:
            raise LinkVideoError("没给链接")
        if not link.lower().startswith(("http://", "https://")):
            raise LinkVideoError("这不是链接")
        source = VideoSource(file_name="", url=link, path="", file_id="")
        cfg = self._config
        budget = float(timeout_seconds or 0) or float(
            int(getattr(cfg.media, "link_timeout_seconds", 180))
            + int(cfg.storyboard.ffmpeg_timeout_seconds)
            + int(getattr(cfg.vlm, "timeout_seconds", 60))
            + 20
        )
        deadline = time.monotonic() + budget
        description = await self._describe(source, deadline)
        if not description:
            raise LinkVideoError("没拿到内容")
        return description

    def _search_roots(self) -> list[Path]:
        """ffmpeg 相对路径的候选根目录。

        包含插件数据目录往上的各级目录，以及每级下的 ``tools/ffmpeg/bin``，
        这样默认配置写 ``ffmpeg`` 也能在「一键包自带 ffmpeg」的环境里直接找到。
        """

        roots: list[Path] = []
        current = self._data_dir
        for _ in range(8):
            for candidate in (current, current / "tools" / "ffmpeg" / "bin"):
                if candidate not in roots:
                    roots.append(candidate)
            if current.parent == current:
                break
            current = current.parent
        return roots

    def search_roots(self) -> list[Path]:
        """对外暴露的 ffmpeg 搜索根目录。"""

        return self._search_roots()

    def _resolve_tools(self) -> tuple[str, str]:
        """解析 ffmpeg / ffprobe；ffprobe 实在没有就退化成 ffmpeg，并记一条警告。"""

        cfg = self._config
        roots = self._search_roots()
        ffmpeg = resolve_executable(
            str(cfg.storyboard.ffmpeg_path),
            search_roots=roots,
            default_names=("ffmpeg", "ffmpeg.exe"),
        )
        try:
            ffprobe = resolve_executable(
                str(cfg.storyboard.ffprobe_path),
                search_roots=roots,
                default_names=("ffprobe", "ffprobe.exe"),
            )
        except StoryboardError:
            self._logger.warning(
                "视频分镜：找不到 ffprobe（配置 %r），退化为用 ffmpeg 自己探测；"
                "ffprobe 与 ffmpeg 通常在同一目录，建议把 storyboard.ffprobe_path 配对填好",
                cfg.storyboard.ffprobe_path,
            )
            ffprobe = ffmpeg
        return ffmpeg, ffprobe

    async def _assert_public_host(self, url: str) -> None:
        """拒绝指向内网/本机的直链（防把插件当 SSRF 跳板）。

        只拦「解析到内网」的地址，外加可选的域名白名单；
        视频站链接走 yt-dlp 分支，不受这里限制。
        """

        parts = urlsplit(url)
        host = (parts.hostname or "").strip()
        if not host:
            raise StoryboardError("链接里没有主机名")

        allowed = [str(item).strip().lower().lstrip(".") for item in (getattr(self._config.media, "allow_hosts", []) or [])]
        allowed = [item for item in allowed if item]
        if allowed and not any(host == item or host.endswith("." + item) for item in allowed):
            raise StoryboardError(f"这个域名不在下载白名单里（{host}）")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        if literal is not None:
            addresses = [literal]
        else:
            try:
                infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443, 0, socket.SOCK_STREAM)
            except OSError as exc:
                raise StoryboardError(f"解析域名失败：{host}") from exc
            for info in infos:
                try:
                    addresses.append(ipaddress.ip_address(info[4][0]))
                except ValueError:
                    continue
        for address in addresses:
            if any(address in network for network in _PRIVATE_NETWORKS if network.version == address.version):
                raise StoryboardError(f"拒绝下载内网地址（{host}）")

    async def _fetch(self, source: VideoSource, temp_root: Path) -> tuple[Path, str, str, str, str]:
        """把视频落到临时目录。

        返回 ``(本地路径, 标题, 字幕文本, 弹幕评论文本, 落地页 URL)``。
        """

        cfg = self._config
        limit = int(cfg.media.max_file_mb) * 1024 * 1024

        if source.path:
            local = Path(source.path)
            if local.is_file():
                size = local.stat().st_size
                if size > limit:
                    raise StoryboardError(f"文件太大（{size / 1024 / 1024:.0f}MB）")
                return local, source.file_name, "", "", ""

        domains = list(getattr(cfg.trigger, "link_domains", []) or [])
        if source.url.startswith(("http://", "https://")) and find_links(source.url, domains, limit=1):
            return await self._fetch_link(source, temp_root)

        if source.url.startswith(("http://", "https://")):
            await self._assert_public_host(source.url)
            target = temp_root / source.safe_name
            timeout = aiohttp.ClientTimeout(total=float(cfg.media.download_timeout_seconds))
            try:
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.get(source.url) as response:
                        if response.status != 200:
                            raise StoryboardError(f"下载失败 HTTP {response.status}")
                        declared = int(response.headers.get("Content-Length") or 0)
                        if declared and declared > limit:
                            raise StoryboardError(f"视频体积超过上限（{declared / 1024 / 1024:.0f}MB）")
                        written = 0
                        with target.open("wb") as handle:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                written += len(chunk)
                                if written > limit:
                                    raise StoryboardError("视频体积超过上限")
                                handle.write(chunk)
            except aiohttp.ClientError as exc:
                raise StoryboardError(f"下载出错：{exc}") from exc
            if not target.is_file() or target.stat().st_size == 0:
                raise StoryboardError("下载下来的文件是空的")
            return target, source.file_name, "", "", ""

        cache_dir = str(cfg.media.napcat_cache_dir or "").strip()
        if source.file_id and cache_dir:
            folder = Path(cache_dir)
            if folder.is_dir():
                wanted = Path(str(source.file_name or source.file_id).replace("\\", "/")).name
                for candidate in folder.rglob("*"):
                    if candidate.is_file() and candidate.name == wanted:
                        return candidate, source.file_name, "", "", ""

        raise StoryboardError("拿不到视频文件（只有文件名/ID，没有可用链接）")

    async def _fetch_link(self, source: VideoSource, temp_root: Path) -> tuple[Path, str, str, str, str]:
        """网页视频（B 站等）交给 yt-dlp 下载；B 站顺带抓弹幕和热评。"""

        cfg = self._config
        ffmpeg_dir = ""
        try:
            ffmpeg, _ = self._resolve_tools()
            ffmpeg_dir = str(Path(ffmpeg).parent)
        except StoryboardError:
            ffmpeg_dir = ""

        self._logger.info("视频分镜：用 yt-dlp 下载链接 %s", source.url)
        link_timeout = float(getattr(cfg.media, "link_timeout_seconds", 180.0))
        try:
            video, title, transcript, webpage_url = await asyncio.wait_for(
                asyncio.to_thread(
                    download_video,
                    source.url,
                    out_dir=temp_root / "link",
                    ffmpeg_dir=ffmpeg_dir,
                    quality=int(getattr(cfg.media, "link_quality", 480)),
                    max_seconds=int(getattr(cfg.media, "link_max_seconds", 600)),
                    timeout=link_timeout,
                    max_mb=int(getattr(cfg.media, "max_link_mb", 80)),
                    cookies_file=str(getattr(cfg.media, "cookies_file", "") or ""),
                ),
                timeout=link_timeout + 30.0,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LinkVideoError(f"下载超时（{link_timeout:.0f}s）") from exc
        if transcript:
            self._logger.info("视频分镜：字幕 %d 字（%s）", len(transcript), source.url)

        extras = await self._fetch_extras(webpage_url or source.url)
        return video, title, transcript, extras, webpage_url

    async def _fetch_extras(self, url: str) -> str:
        """B 站弹幕 + 热评；别的站点或抓不到就返回空串，不影响看画面。"""

        cfg = self._config
        extra = getattr(cfg, "extra", None)
        if extra is None or not bool(getattr(extra, "enabled", False)):
            return ""
        danmaku_count = int(getattr(extra, "danmaku_count", 0) or 0)
        comment_count = int(getattr(extra, "comment_count", 0) or 0)
        if danmaku_count <= 0 and comment_count <= 0:
            return ""
        try:
            text = await fetch_extras(
                url,
                danmaku_count=danmaku_count,
                comments_count=comment_count,
                danmaku_max_chars=int(getattr(extra, "danmaku_max_chars", 40) or 40),
                comment_max_chars=int(getattr(extra, "comment_max_chars", 100) or 100),
                comment_mode=int(getattr(extra, "comment_mode", 3) or 3),
                timeout=float(getattr(extra, "timeout_seconds", 20.0) or 20.0),
            )
        except Exception as exc:  # noqa: BLE001 - 弹幕抓不到不该阻断视频
            self._logger.info("视频分镜：弹幕/评论抓取失败（%s）：%s", url[:60], exc)
            return ""
        if text:
            self._logger.info(
                "视频分镜：拿到底部材料 %d 字（弹幕 %d 条 / 热评 %d 条设定）",
                len(text),
                danmaku_count,
                comment_count,
            )
        return text
