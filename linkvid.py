"""链接视频：用 yt-dlp 把网页视频（B 站等）落成本地 mp4，并尽量捞回字幕。

群里更常见的不是 QQ 原生视频，而是一条分享链接（``https://b23.tv/xxx``）。
这种链接直接 HTTP 抓页面只能拿到标题和简介，视频本体要交给 yt-dlp。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
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
    """把 WebVTT 字幕压成一段连续文本（去掉时间轴、标签和重复行）。"""

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


def _run(cmd: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 参数固定由本模块构造
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _build_env(ffmpeg_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    if ffmpeg_dir:
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


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
) -> tuple[Path, str, str]:
    """下载链接指向的视频，返回 ``(本地路径, 标题, 字幕文本)``。"""

    out_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env(ffmpeg_dir)
    base = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--no-progress"]
    if cookies_file:
        base += ["--cookies", cookies_file]

    probe = _run(base + ["--skip-download", "--print", "%(title)s", "--print", "%(duration)s", url], timeout=timeout, env=env)
    if probe.returncode != 0:
        message = (probe.stderr or probe.stdout or "").strip().splitlines()
        raise LinkVideoError(f"解析链接失败：{message[-1][:120] if message else '未知错误'}")
    printed = [line for line in (probe.stdout or "").splitlines() if line.strip()]
    title = printed[0].strip() if printed else ""
    duration = 0.0
    if len(printed) > 1:
        try:
            duration = float(printed[1])
        except ValueError:
            duration = 0.0
    if duration and duration > max_seconds:
        raise LinkVideoError(f"视频太长（{duration / 60:.0f} 分），先不看")

    limit = int(quality)
    # 竖屏视频的 height 可能远大于 width（如 480x708），所以宽高都要试；最后兜底 b
    format_selector = (
        f"bv*[height<={limit}]+ba/bv*[width<={limit}]+ba/"
        f"b[height<={limit}]/b[width<={limit}]/b"
    )
    template = str(out_dir / "%(id)s.%(ext)s")
    download = _run(
        base
        + [
            "-f",
            format_selector,
            "--merge-output-format",
            "mp4",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "zh.*,ai-zh,en.*",
            "--sub-format",
            "vtt",
            "-o",
            template,
            url,
        ],
        timeout=timeout,
        env=env,
    )
    if download.returncode != 0:
        message = (download.stderr or download.stdout or "").strip().splitlines()
        raise LinkVideoError(f"下载失败：{message[-1][:120] if message else '未知错误'}")

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
    return video, title, transcript
