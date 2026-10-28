"""视频分镜插件的配置模型。

思路来自 dsh-video-storyboard：把一段视频压成一张带编号与时间戳的分镜图，
再交给宿主的视觉模型读成一段中文；B 站链接还会顺带抓弹幕与热评，
让纯文本的聊天模型也能"看"视频、听懂梗。
"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础信息"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    # enabled 是宿主保留键，禁止在模型里声明
    config_version: str = Field(
        default="1.3.0",
        description="配置版本，请勿手动修改。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class TriggerSection(PluginConfigBase):
    """什么时候去看视频"""

    __ui_label__ = "触发"
    __ui_icon__ = "play"
    __ui_order__ = 1

    trigger_mode: str = Field(
        default="all",
        description="all=看到视频就看；at=只在被@时看；off=关闭",
        json_schema_extra={
            "label": "触发范围",
            "order": 0,
            "hint": "填 all / at / off 三者之一",
        },
    )

    allow_groups: list[str] = Field(
        default_factory=list,
        description="只在这些群里看（群号字符串）；留空=不限",
        json_schema_extra={"label": "群白名单", "order": 1},
    )

    deny_groups: list[str] = Field(
        default_factory=list,
        description="这些群不看",
        json_schema_extra={"label": "群黑名单", "order": 2},
    )

    max_videos_per_message: int = Field(
        default=1,
        ge=1,
        le=3,
        description="一条消息里最多看几个视频",
        json_schema_extra={"label": "单条上限", "order": 3},
    )

    skip_short_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="短于这个秒数的视频跳过",
        json_schema_extra={"label": "太短就跳过（秒）", "order": 4},
    )

    report_failure: bool = Field(
        default=True,
        description="失败时注入一句「没看清」，关掉则静默跳过",
        json_schema_extra={"label": "失败要说话", "order": 5},
    )

    link_videos: bool = Field(
        default=True,
        description="消息里贴了 B 站等视频链接时，也去下载来看",
        json_schema_extra={"label": "看链接视频", "order": 6, "hint": "关掉则只处理 QQ 原生视频"},
    )

    link_domains: list[str] = Field(
        default_factory=lambda: [
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
        ],
        description="认哪些域名的链接（域名后缀匹配）",
        json_schema_extra={"label": "链接域名", "order": 7},
    )


class MediaSection(PluginConfigBase):
    """怎么把视频文件弄到手"""

    __ui_label__ = "取文件"
    __ui_icon__ = "download"
    __ui_order__ = 2

    download_timeout_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="下载视频的超时（秒）",
        json_schema_extra={"label": "下载超时（秒）", "order": 0},
    )

    max_file_mb: int = Field(
        default=80,
        ge=5,
        le=500,
        description="超过这个大小的视频不看",
        json_schema_extra={"label": "体积上限（MB）", "order": 1},
    )

    napcat_cache_dir: str = Field(
        default="",
        description="可选：只有一个 file_id 时，去这个目录里按文件名找视频",
        json_schema_extra={"label": "NapCat 缓存目录", "order": 2, "hint": "留空则只认链接/本地路径"},
    )

    link_quality: int = Field(
        default=480,
        ge=144,
        le=1080,
        description="链接视频下载的最高分辨率（越低越快越省流）",
        json_schema_extra={"label": "链接清晰度", "order": 3},
    )

    link_max_seconds: int = Field(
        default=600,
        ge=15,
        le=7200,
        description="链接视频超过这个时长就不看（秒）",
        json_schema_extra={"label": "链接时长上限（秒）", "order": 4},
    )

    link_timeout_seconds: int = Field(
        default=180,
        ge=30,
        le=900,
        description="yt-dlp 下载链接视频的超时（秒）",
        json_schema_extra={"label": "链接下载超时（秒）", "order": 5},
    )

    max_link_mb: int = Field(
        default=80,
        ge=5,
        le=500,
        description="链接视频下载后的体积上限（MB）",
        json_schema_extra={"label": "链接体积上限（MB）", "order": 6},
    )

    cookies_file: str = Field(
        default="",
        description="可选：yt-dlp 用的 cookies.txt（B 站高清/字幕需要登录态）",
        json_schema_extra={"label": "cookies 文件", "order": 7, "hint": "留空则匿名下载，通常只有 360p"},
    )


class StoryboardSection(PluginConfigBase):
    """怎么切帧、怎么拼图"""

    __ui_label__ = "分镜"
    __ui_icon__ = "grid-3x3"
    __ui_order__ = 3

    ffmpeg_path: str = Field(
        default="tools/ffmpeg/bin/ffmpeg.exe",
        description="ffmpeg 可执行文件；相对路径会依次在数据目录、MaiBot 目录、实例根目录下找",
        json_schema_extra={"label": "ffmpeg 路径", "order": 0},
    )

    ffprobe_path: str = Field(
        default="tools/ffmpeg/bin/ffprobe.exe",
        description="ffprobe 可执行文件",
        json_schema_extra={"label": "ffprobe 路径", "order": 1},
    )

    cells: int = Field(
        default=9,
        ge=4,
        le=16,
        description="分镜格数",
        json_schema_extra={"label": "格数", "order": 2},
    )

    columns: int = Field(
        default=3,
        ge=2,
        le=6,
        description="每行几格",
        json_schema_extra={"label": "列数", "order": 3},
    )

    max_seconds: int = Field(
        default=180,
        ge=10,
        le=900,
        description="只看视频开头这么多秒",
        json_schema_extra={"label": "最多看多少秒", "order": 4},
    )

    cell_edge: int = Field(
        default=420,
        ge=200,
        le=900,
        description="每格长边像素",
        json_schema_extra={"label": "单格长边", "order": 5},
    )

    font_path: str = Field(
        default="C:/Windows/Fonts/msyhbd.ttc",
        description="标注用的字体文件",
        json_schema_extra={"label": "字体", "order": 6},
    )

    show_timestamp: bool = Field(
        default=True,
        description="在每格右下角标出时间点",
        json_schema_extra={"label": "标时间戳", "order": 7},
    )

    ffmpeg_timeout_seconds: int = Field(
        default=90,
        ge=10,
        le=600,
        description="ffmpeg/ffprobe 的超时（秒）",
        json_schema_extra={"label": "ffmpeg 超时（秒）", "order": 8},
    )

    jpeg_quality: int = Field(
        default=85,
        ge=50,
        le=95,
        description="分镜图 JPEG 质量",
        json_schema_extra={"label": "图片质量", "order": 9},
    )


class VlmSection(PluginConfigBase):
    """交给哪个视觉模型看图"""

    __ui_label__ = "视觉描述"
    __ui_icon__ = "eye"
    __ui_order__ = 4

    task_name: str = Field(
        default="vlm",
        description="宿主模型任务名，vlm 即视觉任务",
        json_schema_extra={"label": "模型任务", "order": 0},
    )

    model_name: str = Field(
        default="",
        description="可选：指定具体模型名，留空走任务默认",
        json_schema_extra={"label": "指定模型", "order": 1},
    )

    temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=1.5,
        description="采样温度",
        json_schema_extra={"label": "温度", "order": 2},
    )

    max_tokens: int = Field(
        default=2048,
        ge=512,
        le=8192,
        description="输出额度；思考模型会吃掉一部分",
        json_schema_extra={"label": "最大 token", "order": 3},
    )

    max_chars: int = Field(
        default=480,
        ge=80,
        le=2000,
        description="描述最大字数",
        json_schema_extra={"label": "描述字数上限", "order": 4},
    )

    style: str = Field(
        default="chat",
        description="chat=聊天气口吻（默认）；detail=尽量详细",
        json_schema_extra={"label": "描述风格", "order": 5},
    )


class ExtraSection(PluginConfigBase):
    """B 站弹幕与热评：光看画面不够，弹幕和评论才懂梗"""

    __ui_label__ = "弹幕评论"
    __ui_icon__ = "message-circle"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description="给 B 站链接顺带抓弹幕和热评（其它站点不支持）",
        json_schema_extra={"label": "启用", "order": 0},
    )

    danmaku_count: int = Field(
        default=60,
        ge=0,
        le=500,
        description="取多少条弹幕（0=不取），会去重后均匀抽样",
        json_schema_extra={"label": "弹幕条数", "order": 1},
    )

    danmaku_max_chars: int = Field(
        default=40,
        ge=4,
        le=120,
        description="单条弹幕太长就丢掉",
        json_schema_extra={"label": "弹幕字数上限", "order": 2},
    )

    comment_count: int = Field(
        default=20,
        ge=0,
        le=100,
        description="取多少条热评（0=不取）",
        json_schema_extra={"label": "热评条数", "order": 3},
    )

    comment_max_chars: int = Field(
        default=100,
        ge=10,
        le=400,
        description="单条热评截断到多少字",
        json_schema_extra={"label": "热评字数上限", "order": 4},
    )

    comment_mode: int = Field(
        default=3,
        ge=1,
        le=3,
        description="3=按热度（默认）；2=按时间；1=按回复",
        json_schema_extra={"label": "热评排序", "order": 5},
    )

    timeout_seconds: float = Field(
        default=20.0,
        ge=3.0,
        le=120.0,
        description="抓弹幕/评论的总超时",
        json_schema_extra={"label": "超时", "order": 6},
    )


class CacheSection(PluginConfigBase):
    """同一个视频不重复看"""

    __ui_label__ = "缓存"
    __ui_icon__ = "database"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description="按视频地址/文件名缓存描述",
        json_schema_extra={"label": "启用缓存", "order": 0},
    )

    max_entries: int = Field(
        default=200,
        ge=10,
        le=5000,
        description="最多缓存多少条",
        json_schema_extra={"label": "缓存条数", "order": 1},
    )


class DebugSection(PluginConfigBase):
    """排查用"""

    __ui_label__ = "调试"
    __ui_icon__ = "bug"
    __ui_order__ = 7

    log_ffmpeg: bool = Field(
        default=False,
        description="把 ffmpeg 的 stderr 也写进日志",
        json_schema_extra={"label": "记录 ffmpeg 输出", "order": 0},
    )

    keep_temp_files: bool = Field(
        default=False,
        description="保留抽出来的帧与分镜图（调试用）",
        json_schema_extra={"label": "保留临时文件", "order": 1},
    )


class VideoStoryboardConfig(PluginConfigBase):
    """视频分镜插件的完整配置。"""

    __ui_label__ = "视频分镜"
    __ui_icon__ = "film"
    __ui_order__ = 91

    plugin: PluginSection = Field(default_factory=PluginSection)
    trigger: TriggerSection = Field(default_factory=TriggerSection)
    media: MediaSection = Field(default_factory=MediaSection)
    storyboard: StoryboardSection = Field(default_factory=StoryboardSection)
    vlm: VlmSection = Field(default_factory=VlmSection)
    extra: ExtraSection = Field(default_factory=ExtraSection)
    cache: CacheSection = Field(default_factory=CacheSection)
    debug: DebugSection = Field(default_factory=DebugSection)
