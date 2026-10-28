# 视频分镜 Video Storyboard · MaiBot 插件

让麦麦真的能「看」视频。

纯文本的聊天模型看不了视频，插件把视频压成**一张带编号与时间戳的分镜图**，交给宿主的视觉模型读成一段中文，再把这段中文注入到消息里——模型于是有了画面。B 站链接还会顺带抓**弹幕**和**热评**，因为很多视频的笑点根本不在画面里，而在弹幕和评论区。

> 思路来自 [SAOS-3624/dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)（DeepSeek Harness 的分镜工具），这个插件是把它搬到 MaiBot 上的重写版：不再依赖 Node，抽帧/拼图用纯 Python + Pillow，注入走宿主的 Hook。

---

## 它做了什么

```
群里有人发视频 / 发 B 站链接
        │
        ├─ QQ 原生视频 → 从适配器给的链接或本地路径取文件
        └─ B 站链接 → yt-dlp 下载（≤480p），顺带抓弹幕 + 热评
        │
   ffmpeg 按时间均匀抽 N 帧 → Pillow 拼成一张图
   （格编号 + 时间戳 + 顶部信息条，一张图 ≈ 几百 token）
        │
   视觉模型读图 + 读弹幕/热评 → 1~3 句中文
        │
   以 [视频内容] … 的形式注入到这条消息里
        │
   麦麦的规划器看到的就是「有画面、有梗」的消息
```

实测（B 站 50 秒竖屏片）：从收到链接到注入文本约 12 秒，之后她的回复是「大鱼小鱼那段还行」「前面有点用力过猛了」——确实在看内容，而不是在猜。

---

## 安装

### 方式一：丢进插件目录（推荐，最省事）

1. 装依赖：宿主 `python-env` 里需要 `Pillow`、`aiohttp`（一般都有）。
2. 准备 ffmpeg：把 `ffmpeg.exe` / `ffprobe.exe` 放到
   `MaiBotOneKeyDesktop/<实例 id>/tools/ffmpeg/bin/`，
   或任意位置然后在 `config.toml` 里写绝对路径。
3. 克隆到插件目录：

```bash
cd MaiBotOneKeyDesktop/<实例 id>/modules/MaiBot/plugins
git clone https://github.com/Netsysn/maibot-video-storyboard video-storyboard
```

4. 改个名字也行，宿主按 `_manifest.json` 认插件，不认目录名。
5. 宿主会自己热加载，日志里出现这句就成了：

```
[plugin.netsysn.video-storyboard] 视频分镜 v1.3.0 已加载（…，9 格，每张最多看 180 秒，视觉任务=vlm）
```

### 方式二：插件市场

已向 MaiBot 插件中心提交登记（[Mai-with-u/plugin-repo#1097](https://github.com/Mai-with-u/plugin-repo/issues/1097)），审核通过后在「插件市场」里搜 **视频分镜** 就能直接装。等不及就先用方式一。

### 依赖清单

| 依赖 | 必需 | 说明 |
| --- | --- | --- |
| `Pillow >= 10` | ✅ | 拼分镜图 |
| `aiohttp >= 3.8` | ✅ | 下载视频、抓弹幕评论 |
| `ffmpeg` / `ffprobe` | ✅ | 抽帧与探测时长 |
| `yt-dlp` | B 站链接必需 | 宿主的 `python-env` 一般自带；没有就 `pip install yt-dlp` |
| 视觉模型 | ✅ | `[model_task_config.vlm]` 指向的模型必须支持图片输入 |

---

## 配置

WebUI →「插件扩展」→「视频分镜」里可视化改，或直接编辑插件目录下的 `config.toml`。

### `[plugin]`
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 宿主开关（不要写进配置模型，宿主保留键） |
| `config_version` | `1.3.0` | 配置版本，别手动改 |

### `[trigger]` 什么时候去看
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `trigger_mode` | `all` | `all` 全部消息 / `at` 只有 @ 或私聊 |
| `allow_groups` | `[]` | 只处理这些群（空=不限制） |
| `deny_groups` | `[]` | 这些群不处理 |
| `max_videos_per_message` | `1` | 一条消息最多看几个视频 |
| `skip_short_seconds` | `1.0` | 太短的视频跳过 |
| `report_failure` | `true` | 看失败时在消息里留一句说明 |
| `link_videos` | `true` | 认消息里的视频站链接 |
| `link_domains` | 12 个 | 支持 b23.tv / bilibili / 抖音 / 小红书 / YouTube / 微博 / 腾讯视频 / 西瓜 等 |

### `[media]` 取文件
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `download_timeout_seconds` | `60` | 直链下载超时 |
| `max_file_mb` | `80` | 单个文件上限 |
| `napcat_cache_dir` | `""` | 只有文件 ID 时去这个目录翻 |
| `link_quality` | `480` | 网页视频清晰度上限（越高越慢） |
| `link_max_seconds` | `600` | 超过这个长度不下载 |
| `link_timeout_seconds` | `180` | yt-dlp 下载超时 |
| `max_link_mb` | `80` | 网页视频体积上限 |
| `cookies_file` | `""` | cookies.txt 路径；配了才拿得到 B 站字幕/更高清 |

### `[storyboard]` 分镜
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ffmpeg_path` / `ffprobe_path` | 实例 `tools/ffmpeg/bin` | 可写绝对路径 |
| `cells` | `9` | 拼几格（9 / 12 常用，太多会糊） |
| `columns` | `3` | 列数 |
| `max_seconds` | `180` | 只看前 N 秒 |
| `cell_edge` | `420` | 单格长边像素 |
| `font_path` | 微软雅黑粗体 | 编号与时间戳用的字体 |
| `show_timestamp` | `true` | 每格右下角时间戳 |
| `ffmpeg_timeout_seconds` | `90` | 抽帧超时 |
| `jpeg_quality` | `85` | 输出质量 |

### `[vlm]` 视觉描述
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `task_name` | `vlm` | 宿主模型任务名 |
| `model_name` | `""` | 指定模型，留空走任务默认 |
| `temperature` | `0.3` | |
| `max_tokens` | `2048` | 思考模型会吃掉一部分 |
| `max_chars` | `480` | 描述字数上限 |
| `style` | `chat` | `chat` 口语 / `detail` 详细 |

### `[extra]` 弹幕与热评（B 站专属）★
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `danmaku_count` | `60` | 取多少条弹幕，`0` = 不取 |
| `danmaku_max_chars` | `40` | 单条弹幕字数上限 |
| `comment_count` | `20` | 取多少条热评，`0` = 不取 |
| `comment_max_chars` | `100` | 单条热评截断长度 |
| `comment_mode` | `3` | `3` 热度 / `2` 时间 / `1` 回复数 |
| `timeout_seconds` | `20` | 抓取超时，失败不影响看画面 |

取到的样子（会拼进视觉模型的提示词）：

```
【弹幕样本】观众实时吐槽，按时间排序，可能有重复和黑话
00:12 牛的
00:16 就这会好在锅没掉，掉了后果就严重了
00:23 弹幕里加油灭火是认真的嘛

【热评】评论区点赞最高的几条
1. （1024 赞）这是我姐的煎饼，因为时代久远，只找到这个
2. （584 赞）两女婿回到客厅后
```

### `[cache]` / `[debug]`
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `cache.enabled` | `true` | 同一个视频/链接不重复看 |
| `cache.max_entries` | `200` | 缓存条数 |
| `debug.log_ffmpeg` | `false` | 把 ffmpeg 输出写日志 |
| `debug.keep_temp_files` | `false` | 保留抽出的帧（排查用） |

---

## 用法

插件挂的是 Hook `chat.receive.before_process`，**不需要模型主动调用**，收到视频/链接就自己看。另外给了两个手动入口：

| 入口 | 说明 |
| --- | --- |
| 工具 `video_storyboard(url)` | 模型自己判断要用时调用（deferred 工具，先 `tool_search` 找出来） |
| 命令 `/看视频 <链接>`（或 `/video`、`/看片`） | 手动触发，回一句摘要 |

> 工具被标成 **deferred** 是有意的：它不进 planner 的常驻工具表，只在「未暴露工具清单」里出现，模型需要时自己 `tool_search` 拉起来。这样不会每轮都塞一份 schema 进去。

### QQ 原生视频需要适配器给链接

`snowluma-adapter` 默认把视频压成 `[视频] 文件: x.mp4，大小: N`，**没有下载地址**，插件就没法取文件。两种解决办法：

1. **打个小补丁**（推荐）——在 `codecs/inbound/message_codec.py` 的 `_build_video_text_segment` 里补两行：

```python
media_url = str(segment_data.get("url") or "").strip()
if media_url:
    parts.append(f"链接: {media_url}")
```

2. `napcat-adapter` 或新版适配器如果已经把 `url` 写进文本，插件能直接认。

### 让它「主动」想到看视频

模型看不见工具就会说「我看不了视频」。在两处提示词里各加一句就顺了（`config/bot_config.toml` 的 `chat.reply_style.group_chat_prompt` 与 `private_chat_prompts`）：

```
你也能看视频。别人发来视频链接（b23.tv、bilibili、抖音、小红书、YouTube 等），
或者问你「这个视频讲了啥」「能不能看视频」时，这一轮先在 tool_search 里搜
「视频 video」把 video_storyboard 找出来，紧接着调用它，把链接放进 url 参数。
绝不能说「看不了」「没这个工具」——那是错的。
```

---

## 成本与耗时

| 项目 | 量级 |
| --- | --- |
| 抽帧 + 拼图（9 格，180 秒内） | 2~5 秒 |
| B 站链接下载（480p，50 秒片） | 5~10 秒 |
| 弹幕 + 热评 | 0.5~2 秒 |
| 视觉模型一次 | ≈ 一张图 + 1~2k 字文本 |
| 总耗时 | 通常 10~20 秒，命中缓存 < 1 秒 |

比「逐帧喂图」便宜一个数量级：一张 9 格分镜图约等于 384~600 token，逐帧要 3000+。

---

## 排错

**日志里出现「拿不到视频文件（只有文件名/ID，没有可用链接）」**
适配器没给 url。见上面「QQ 原生视频需要适配器给链接」。

**「视频分镜：用 yt-dlp 下载链接 …」之后没动静**
多半是 yt-dlp 太旧或没装：`python-env\python.exe -m yt_dlp --version`。也可能是竖屏片把格式选择器坑了（1.1.0 已修）。

**视觉模型报「不支持图片」**
`[model_task_config.vlm]` 指向的模型得是多模态的；可以把 `[vlm] model_name` 单独指定成一个确定支持图片的模型。

**弹幕是空的**
① 不是 B 站；② `[extra] enabled=false`；③ 接口被风控（换 IP / 稍后再试）。失败只会让描述少点梗，不会让视频看不了。

**说「看不了视频」但其实已经注入**
模型不知道工具存在 → 照上面「让它主动想到看视频」加提示词；工具本身是 deferred 的，它得靠 `tool_search` 找。

**群里刷屏**
把 `[trigger] allow_groups` 填成只允许的群，或把 `trigger_mode` 改成 `at`。

---

## 已知限制

- 只支持 B 站的弹幕/热评，其它站点只有画面（YouTube 以后可以接，接口不通用）。
- 分镜是**采样**，不是连续画面：帧间的快速动作看不出来，所以提示词里明确写了「不要臆测」。
- 不看音轨。B 站匿名下载通常拿不到字幕，想让她「听懂」得配 `cookies_file` 拿字幕，或另有 ASR 插件。
- 一条消息只看 `max_videos_per_message` 个视频，避免一条消息拖住整轮对话（总预算 75 秒）。

---

## 更新日志

**1.3.0**
- 新增 `[extra]`：B 站弹幕（默认 60 条，均匀抽样、去重）+ 热评（默认 20 条，可改排序），数量与字数都可配。
- 视觉提示词里区分「画面事实」与「观众观点」，弹幕热评只用来理解梗，不写成画面描述。
- 插件 id 改为 `netsysn.video-storyboard`，仓库公开。

**1.2.2**
- `video_storyboard` 工具改为 deferred，避免「工具存在但模型看不见」。
- 修 `self.ctx.paths.data_dir` 为 `str` 导致的崩溃。

**1.2.1**
- 新增工具 `video_storyboard(url)` 与命令 `/看视频`。
- Hook 改名 `video_storyboard_hook`，避免与同名工具冲突。

**1.1.0**
- 支持网页视频链接（yt-dlp），带标题与字幕。
- 修竖屏视频格式选择器。

**1.0.0**
- QQ 原生视频分镜：抽帧、拼图、视觉描述、注入、缓存。

---

## License

MIT © [Netsysn](https://github.com/Netsysn)

分镜思路参考 [SAOS-3624/dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)（MIT）。
