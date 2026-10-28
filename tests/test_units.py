"""离线单元测试（不需要 MaiBot 宿主、不需要 ffmpeg、不联网）。

跑法（在插件目录里）::

    python -m unittest discover -s tests -v

测试只覆盖纯逻辑：链接解析、缓存键、落盘文件名、弹幕解析与解压、ffmpeg 路径解析。
真正抽帧/拼图/调模型的行为靠宿主里实跑验证（见 README「排障」）。
"""

from __future__ import annotations

import sys
import types
import unittest
import zlib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# 插件目录不含合法包名（有连字符），用 stub 包把相对 import 接起来；
# 只导入不需要 maibot_sdk 的模块（config/plugin 需要宿主 SDK，这里不碰）。
if "vsb" not in sys.modules:
    package = types.ModuleType("vsb")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["vsb"] = package

from vsb import bili, linkvid, pipeline, storyboard  # noqa: E402


def _raw_deflate(data: bytes) -> bytes:
    """按 B 站弹幕接口的格式做裸 deflate 压缩（zlib 的 -15 只能用于 decompressobj）。"""

    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


class ParseVideoTextTest(unittest.TestCase):
    def test_text_with_link_and_size(self) -> None:
        source = pipeline.parse_video_text("[视频] 文件: a.mp4，大小: 1.2MB，链接: https://example.com/a.mp4")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.file_name, "a.mp4")
        self.assertEqual(source.url, "https://example.com/a.mp4")

    def test_marker_variants(self) -> None:
        for marker in ("[视频文件]", "[ 视频 ]"):
            source = pipeline.parse_video_text(f"{marker} 文件: b.mov，路径: D:/tmp/b.mov")
            self.assertIsNotNone(source, marker)
            assert source is not None
            self.assertEqual(source.path, "D:/tmp/b.mov")

    def test_plain_marker_without_payload(self) -> None:
        self.assertIsNone(pipeline.parse_video_text("[视频]"))
        self.assertIsNone(pipeline.parse_video_text("普通聊天，没有视频"))


class VideoSourceTest(unittest.TestCase):
    def test_cache_key_priority(self) -> None:
        same_id_other_url = pipeline.VideoSource(file_name="a.mp4", url="https://cdn.qq.com/a.mp4?sig=1", path="", file_id="FILEID")
        same_id_other_url2 = pipeline.VideoSource(file_name="a.mp4", url="https://cdn.qq.com/a.mp4?sig=2", path="", file_id="FILEID")
        by_bv = pipeline.VideoSource(file_name="a.mp4", url="https://www.bilibili.com/video/BV1kQna6JEXA?p=1", path="", file_id="")
        by_bv_short = pipeline.VideoSource(file_name="a.mp4", url="https://www.bilibili.com/video/BV1kQna6JEXA?p=9&share=1", path="", file_id="")
        by_url = pipeline.VideoSource(file_name="a.mp4", url="https://cdn.example.com/a.mp4?token=abc", path="", file_id="")
        by_url2 = pipeline.VideoSource(file_name="a.mp4", url="https://cdn.example.com/a.mp4?token=def", path="", file_id="")

        # 同一个 file_id 换了签名参数也算同一个视频
        self.assertEqual(same_id_other_url.cache_key, same_id_other_url2.cache_key)
        # 同一个 BV 号换了查询串也算同一个视频
        self.assertEqual(by_bv.cache_key, by_bv_short.cache_key)
        # 签名参数不进缓存键
        self.assertEqual(by_url.cache_key, by_url2.cache_key)
        # 不同视频不会撞键
        self.assertNotEqual(by_bv.cache_key, by_url.cache_key)
        self.assertEqual(len(by_url.cache_key), 24)

    def test_safe_name_blocks_traversal(self) -> None:
        source = pipeline.VideoSource(
            file_name="../../evil.exe", url="", path="", file_id=""
        )
        self.assertTrue(source.safe_name.endswith(".mp4"))
        self.assertNotIn("..", source.safe_name)
        self.assertNotIn("/", source.safe_name)

    def test_alias_key_from_webpage_url(self) -> None:
        self.assertEqual(pipeline._alias_key("https://www.bilibili.com/video/BV1kQna6JEXA"), "bv:BV1kQna6JEXA")
        self.assertEqual(pipeline._alias_key("https://example.com/x"), "")


class FindLinksTest(unittest.TestCase):
    def test_only_known_domains(self) -> None:
        text = "看看 https://example.com/a 和 https://b23.tv/LjDHk2l 还有 https://youtu.be/abc123"
        links = linkvid.find_links(text, linkvid.DEFAULT_DOMAINS, limit=5)
        self.assertEqual(len(links), 2)
        self.assertIn("b23.tv", links[0])
        self.assertIn("youtu.be", links[1])

    def test_limit_zero(self) -> None:
        self.assertEqual(linkvid.find_links("https://b23.tv/x", linkvid.DEFAULT_DOMAINS, limit=0), [])


class DanmakuTest(unittest.TestCase):
    XML = (
        '<?xml version="1.0" encoding="UTF-8"?><i>'
        '<d p="2.57,1,25,16777215,0,0,0,0">哈哈</d>'
        '<d p="0.76,1,25,16777215,0,0,0,0">开场</d>'
        '<d p="0.76,1,25,16777215,0,0,0,0">开场</d>'
        '<d p="5.00,1,25,16777215,0,0,0,0">' + "超长" * 40 + '</d>'
        "</i>"
    )

    def test_dedupe_sort_and_length_filter(self) -> None:
        items = bili.parse_danmaku(self.XML, count=10, max_chars=20)
        self.assertEqual([text for _, text in items], ["开场", "哈哈"])

    def test_uniform_sample(self) -> None:
        items = bili.parse_danmaku(self.XML, count=1, max_chars=20)
        self.assertEqual(len(items), 1)

    def test_inflate_raw_deflate(self) -> None:
        self.assertIn("开场", bili._inflate(_raw_deflate(self.XML.encode("utf-8"))))

    def test_inflate_respects_limit(self) -> None:
        raw = _raw_deflate(b"x" * (2 * 1024 * 1024))
        self.assertLessEqual(len(bili._inflate(raw, max_bytes=4096)), 4096)


class ResolveExecutableTest(unittest.TestCase):
    def test_relative_name_found_under_search_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
            binary.parent.mkdir(parents=True)
            binary.write_text("stub", encoding="utf-8")
            resolved = storyboard.resolve_executable(
                "ffmpeg.exe", search_roots=[Path(tmp)], default_names=("ffmpeg.exe",)
            )
            self.assertEqual(Path(resolved).resolve(), binary.resolve())

    def test_missing_binary_raises(self) -> None:
        with self.assertRaises(storyboard.StoryboardError):
            storyboard.resolve_executable(
                "definitely-not-a-real-binary-xyz",
                search_roots=[Path(".")],
                default_names=("definitely-not-a-real-binary-xyz",),
            )


if __name__ == "__main__":
    unittest.main()
