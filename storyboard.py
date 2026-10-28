"""视频分镜：ffprobe 取元信息、ffmpeg 抽帧、Pillow 拼成带编号与时间戳的分镜图。

所有函数都是同步阻塞实现，调用方负责丢进 ``asyncio.to_thread``。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 找不到配置的字体时按平台依次尝试（先 Windows，再 Linux/macOS）
FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)


class StoryboardError(RuntimeError):
    """分镜生成失败。"""


@dataclass(frozen=True)
class VideoMeta:
    """视频元信息。"""

    duration: float
    width: int
    height: int
    has_audio: bool

    @property
    def summary(self) -> str:
        """给日志用的一行摘要。"""

        audio = "有声" if self.has_audio else "无声"
        return f"{self.width}x{self.height} {self.duration:.1f}s {audio}"


@dataclass(frozen=True)
class FrameSample:
    """一帧采样结果。"""

    path: Path
    timestamp: float


def resolve_executable(
    value: str,
    *,
    search_roots: Sequence[Path],
    default_names: Sequence[str] = (),
) -> str:
    """把配置里的可执行文件路径解析成可用的绝对路径。

    解析顺序：绝对路径 → PATH 中的命令名 → 各候选根目录下的相对路径
    → 各候选根目录下 ``tools/ffmpeg/bin`` 里的默认文件名。

    Args:
        value: 配置值，可能是绝对路径、相对路径、PATH 中的命令名，或留空表示自动查找。
        search_roots: 相对路径的候选根目录，按顺序尝试。
        default_names: 配置留空时按顺序尝试的文件名（如 ``ffmpeg``、``ffprobe``）。

    Returns:
        str: 可直接执行的文件路径。

    Raises:
        StoryboardError: 找不到可执行文件。
    """

    raw = (value or "").strip()
    names = [raw] if raw else [str(name) for name in default_names]
    if not names:
        raise StoryboardError("未配置 ffmpeg/ffprobe 路径")

    def _variants(name: str) -> list[str]:
        """Windows 上配置写 ``ffmpeg`` 时要能匹配到 ``ffmpeg.exe``。"""

        items = [name]
        lowered = name.lower()
        if os.name == "nt" and not lowered.endswith((".exe", ".cmd", ".bat")):
            items.append(name + ".exe")
        return items

    for name in names:
        candidate = Path(name)
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)
        if "/" not in name and "\\" not in name:
            for variant in _variants(name):
                found = shutil.which(variant)
                if found:
                    return found

    for name in names:
        if Path(name).is_absolute():
            continue
        for variant in _variants(name):
            for root in search_roots:
                for probe in ((root / variant), (root / "tools" / "ffmpeg" / "bin" / variant)):
                    try:
                        resolved = probe.resolve()
                    except OSError:  # pragma: no cover - 路径过长等
                        continue
                    if resolved.is_file():
                        return str(resolved)

    hint = raw or "/".join(str(name) for name in default_names)
    roots = ", ".join(str(root) for root in search_roots)
    raise StoryboardError(f"找不到可执行文件: {hint}（候选根目录: {roots}）")


def _run(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """执行外部命令并返回结果。"""

    try:
        return subprocess.run(  # noqa: S603 - 参数由本模块拼装，不经过 shell
            list(command),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - 已在 resolve 阶段拦过
        raise StoryboardError(f"命令不存在: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StoryboardError(f"命令超时（{timeout:.0f}s）: {Path(command[0]).name}") from exc


def probe_video(ffprobe: str, video: Path, *, timeout: float) -> VideoMeta:
    """读视频的时长、分辨率、是否带音轨。"""

    completed = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,duration",
            "-of",
            "json",
            str(video),
        ],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise StoryboardError(f"ffprobe 读取失败: {completed.stderr.decode('utf-8', 'ignore')[:200]}")

    try:
        payload: dict[str, Any] = json.loads(completed.stdout.decode("utf-8", "ignore") or "{}")
    except json.JSONDecodeError as exc:
        raise StoryboardError("ffprobe 输出解析失败") from exc

    streams = payload.get("streams") or []
    container_duration = float((payload.get("format") or {}).get("duration") or 0.0)
    stream_duration = 0.0
    width = 0
    height = 0
    has_audio = False
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "")
        try:
            stream_seconds = float(stream.get("duration") or 0.0)
        except (TypeError, ValueError):
            stream_seconds = 0.0
        if codec_type == "video":
            if not width:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            stream_duration = max(stream_duration, stream_seconds)
        elif codec_type == "audio":
            has_audio = True
            stream_duration = max(stream_duration, stream_seconds)

    # 有些容器（flv/ts/部分 webm）format 里没有 duration，用流时长兜底
    duration = container_duration or stream_duration
    if duration <= 0:
        raise StoryboardError("读不到视频时长")
    if width <= 0 or height <= 0:
        width, height = 0, 0

    return VideoMeta(duration=duration, width=width, height=height, has_audio=has_audio)


def extract_frames(
    ffmpeg: str,
    ffprobe: str,
    video: Path,
    out_dir: Path,
    *,
    cells: int,
    max_seconds: int,
    cell_edge: int,
    timeout: float,
) -> tuple[list[FrameSample], VideoMeta]:
    """按等间隔抽 ``cells`` 帧，返回帧文件与元信息。

    时间点取每格中心（第 i 格 = 有效时长 * (i + 0.5) / cells），
    用 ``fps`` 滤镜的 ``start_time`` 让首帧就落在半格处，
    这样一次 ffmpeg 调用即可精确覆盖每一格的中间时刻（含最后一格）。
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)

    meta = probe_video(ffprobe, video, timeout=timeout)
    span = min(meta.duration, float(max_seconds))
    if span <= 0.1:
        raise StoryboardError("视频太短，没有可抽的帧")

    cells = max(1, int(cells))
    step = span / cells
    fps = 1.0 / step
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0",
        "-t",
        f"{span:.3f}",
        "-i",
        str(video),
        "-an",
        "-vf",
        (
            f"fps=fps={fps:.6f}:start_time={step / 2:.6f},"
            f"scale='if(gt(iw,ih),min({cell_edge},iw),-2)':'if(gt(iw,ih),-2,min({cell_edge},ih))'"
        ),
        "-frames:v",
        str(cells),
        "-q:v",
        "3",
        str(out_dir / "frame_%02d.jpg"),
    ]
    completed = _run(command, timeout=timeout)
    if completed.returncode != 0:
        raise StoryboardError(f"ffmpeg 抽帧失败: {completed.stderr.decode('utf-8', 'ignore')[:200]}")

    frames = sorted(out_dir.glob("frame_*.jpg"))
    if not frames:
        raise StoryboardError("ffmpeg 没有产出任何帧")

    # 时间戳按「格中心」算，而不是按实际产出帧数，避免掉帧时整体偏早
    samples = [
        FrameSample(path=path, timestamp=min(span, step * (index + 0.5)))
        for index, path in enumerate(frames)
    ]
    return samples, meta


def build_sheet(
    samples: Sequence[FrameSample],
    *,
    columns: int,
    cell_edge: int,
    font_path: str,
    show_timestamp: bool,
    jpeg_quality: int,
    header: str,
) -> bytes:
    """把抽好的帧拼成一张分镜图（JPEG 字节）。"""

    from PIL import Image, ImageDraw, ImageFont

    if not samples:
        raise StoryboardError("没有帧可以拼图")

    columns = max(1, min(int(columns), len(samples)))
    rows = (len(samples) + columns - 1) // columns

    gap = max(6, cell_edge // 40)
    header_h = max(26, cell_edge // 12)
    background = (18, 18, 20)
    accent = (255, 214, 0)

    configured = (font_path or "").strip()
    loaded_font = None
    for candidate in ((configured,) if configured else ()) + FONT_CANDIDATES:
        try:
            loaded_font = ImageFont.truetype(candidate, 24)
            break
        except Exception:  # noqa: BLE001 - 字体缺失就试下一个
            continue
    if loaded_font is None:
        loaded_font = ImageFont.load_default()

    def _font(size: int) -> Any:
        try:
            return loaded_font.font_variant(size=size)  # FreeType 字体支持任意字号
        except Exception:  # noqa: BLE001 - 位图默认字体没有变体，只能将就
            return loaded_font

    number_font = _font(max(18, cell_edge // 14))
    stamp_font = _font(max(13, cell_edge // 22))
    header_font = _font(max(14, cell_edge // 24))

    def _text_size(text: str, font: Any) -> tuple[int, int]:
        box = font.getbbox(text)
        return box[2] - box[0], box[3] - box[1]

    # 位图默认字体可能画不出中文，退化成 ASCII 才不至于满屏豆腐块
    header_text = header
    if not isinstance(header_font, ImageFont.FreeTypeFont) and not header_text.isascii():
        header_text = header_text.encode("ascii", "replace").decode("ascii")

    # 先按第一张算格子尺寸，保证等宽等高，避免拼接错位
    with Image.open(samples[0].path) as first:
        first.load()
        base_w, base_h = first.size
    cell_w = base_w
    cell_h = base_h

    total_w = columns * cell_w + (columns + 1) * gap
    total_h = header_h + rows * cell_h + (rows + 1) * gap
    sheet = Image.new("RGB", (total_w, total_h), background)
    draw = ImageDraw.Draw(sheet)
    header_width, header_height = _text_size(header_text, header_font)
    del header_width
    draw.text(
        (gap, max(2, (header_h - header_height) // 2)),
        header_text,
        font=header_font,
        fill=(235, 235, 235),
    )

    for index, sample in enumerate(samples, start=1):
        row = (index - 1) // columns
        column = (index - 1) % columns
        x = gap + (column * (cell_w + gap))
        y = header_h + gap + (row * (cell_h + gap))

        with Image.open(sample.path) as frame:
            frame = frame.convert("RGB")
            if frame.size != (cell_w, cell_h):
                frame = frame.resize((cell_w, cell_h))
            sheet.paste(frame, (x, y))

        # 编号：黄色方块 + 黑字
        badge = max(18, cell_edge // 16)
        draw.rectangle([x, y, x + badge, y + badge], fill=accent)
        draw.text(
            (x + badge * 0.18, y + badge * 0.06),
            str(index),
            font=number_font,
            fill=(15, 15, 15),
        )

        if show_timestamp:
            label = f"{int(sample.timestamp // 60):02d}:{sample.timestamp % 60:04.1f}"
            text_w, text_h = _text_size(label, stamp_font)
            pad = max(3, gap // 2)
            draw.rectangle(
                [x + cell_w - text_w - pad * 3, y + cell_h - text_h - pad * 3, x + cell_w, y + cell_h],
                fill=(0, 0, 0),
            )
            draw.text(
                (x + cell_w - text_w - pad * 2, y + cell_h - text_h - pad * 2),
                label,
                font=stamp_font,
                fill=(255, 255, 255),
            )

    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=max(50, min(95, int(jpeg_quality))), optimize=True)
    return buffer.getvalue()


def clean_frames(paths: Iterable[Path]) -> None:
    """删除抽出来的临时帧。"""

    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
