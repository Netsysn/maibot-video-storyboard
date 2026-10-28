"""链接视频：用 yt-dlp 把网页视频（B 站等）落成本地 mp4，并尽量捞回字幕。

群里更常见的不是 QQ 原生视频，而是一条分享链接（``https://b23.tv/xxx``）。
这种链接直接 HTTP 抓页面只能拿到标题和简介，视频本体要交给 yt-dlp。

实现上走 yt-dlp 的 Python API 而不是命令行：一次 ``extract_info(download=False)``
就拿到标题/时长/落地页，超长超大可以在下载前拒掉，不用为了探测再跑一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_DOMAINS: tuple[str, ...] = (
    "b23.tv",
    "bilibili.com",
    "bili2233.cn",
    "youtu.be",
    "youtube.com",
    "v.douyin.com",
    "douyin.com",
    "xhslink.com",
    "xiaohongshu.com",
    "weibo.com",
    "v.qq.com",
    "ixigua.com",
)
"""默认认这些站点；其它站点靠配置里的额外域名。"""

_URL_RE = re.compile(r"https?://[^\s\"'<>）)，,。、]+", re.IGNORECASE)


class LinkVideoError(RuntimeError):
    """链接视频拿不到。"""


def find_links(text: str, domains: list[str] | tuple[str, ...] | None = None, *, limit: int = 1) -> list[str]:
    """从一段文本里挑出属于视频站点的链接。"""

    if limit <= 0:
        return []
    normalized = [str(item).strip().lower().lstrip(".") for item in (domains or DEFAULT_DOMAINS)]
    normalized = [item for item in normalized if item]
    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?)]}）】》”’")
        host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if not any(host == item or host.endswith("." + item) for item in normalized):
            continue
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def _vtt_to_text(path: Path, *, max_chars: int = 4000) -> str:
    """把 vtt 字幕压成一行纯文本。"""

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("WEBVTT") or "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        stripped = re.sub(r"\{[^}]*\}", "", stripped).strip()
        if not stripped:
            continue
        if lines and lines[-1] == stripped:
            continue
        lines.append(stripped)
    text = " ".join(lines)
    return text[:max_chars]


def download_video(
    url: str,
    *,
    out_dir: Path,
    ffmpeg_dir: str = "",
    quality: int = 480,
    max_seconds: int = 600,
    timeout: float = 180.0,
    max_mb: int = 80,
    cookies_file: str = "",
) -> tuple[Path, str, str, str]:
    """下载链接指向的视频，返回 ``(本地路径, 标题, 字幕文本, 落地页 URL)``。

    ``timeout`` 只作用在单次网络读写上（yt-dlp 的 ``socket_timeout``）；
    整体超时由调用方（``pipeline``）用 ``asyncio.wait_for`` 兜住。
    """

    try:
        import yt_dlp  # noqa: PLC0415 - 可选依赖，缺失时要给出可读的提示
    except ImportError as exc:  # pragma: no cover - 宿主一般自带
        raise LinkVideoError("宿主环境里没有 yt-dlp，装一下才能看网页视频") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    limit = int(quality)
    # 竖屏视频的 height 可能远大于 width（如 480x708），所以宽高都要试；最后兜底 b
    format_selector = (
        f"bv*[height<={limit}]+ba/bv*[width<={limit}]+ba/"
        f"b[height<={limit}]/b[width<={limit}]/b"
    )

    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": format_selector,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh.*", "ai-zh", "en.*"],
        "subtitlesformat": "vtt",
        "retries": 2,
        "socket_timeout": max(5.0, min(30.0, float(timeout))),
    }
    if ffmpeg_dir:
        options["ffmpeg_location"] = ffmpeg_dir
    if cookies_file:
        options["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise LinkVideoError("解析链接失败：没有拿到视频信息")

            title = str(info.get("title") or "").strip()
            webpage_url = str(info.get("webpage_url") or url)
            try:
                duration = float(info.get("duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration and duration > max_seconds:
                raise LinkVideoError(f"视频太长（{duration / 60:.0f} 分），先不看")

            approx = info.get("filesize_approx") or info.get("filesize") or 0
            try:
                approx_bytes = int(approx)
            except (TypeError, ValueError):
                approx_bytes = 0
            if approx_bytes and approx_bytes > max_mb * 1024 * 1024:
                raise LinkVideoError(f"视频大约 {approx_bytes / 1024 / 1024:.0f}MB，超过上限先不看")

            ydl.download([webpage_url])
    except LinkVideoError:
        raise
    except Exception as exc:  # noqa: BLE001 - yt-dlp 的异常类型太多，统一转成用户可读的
        message = str(exc).strip().splitlines()
        raise LinkVideoError(f"下载失败：{message[0][:120] if message else '未知错误'}") from exc

    videos = sorted(out_dir.glob("*.mp4"), key=lambda item: item.stat().st_size, reverse=True)
    if not videos:
        raise LinkVideoError("下载完成但没有找到视频文件")
    video = videos[0]
    size = video.stat().st_size
    if size > max_mb * 1024 * 1024:
        raise LinkVideoError(f"文件太大（{size / 1024 / 1024:.0f}MB）")

    transcript = ""
    for subtitle in sorted(out_dir.glob("*.vtt")):
        transcript = _vtt_to_text(subtitle)
        if transcript:
            break
    return video, title, transcript, webpage_url
