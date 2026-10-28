"""B 站弹幕与评论抓取。

只处理 B 站：其它站点（抖音/YouTube 等）拿不到弹幕和评论，直接跳过。
全部走匿名接口，不带 cookie 也能取到公开数据；如果哪天被风控，日志会写明原因。
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from typing import Any
from urllib.parse import quote

import aiohttp

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
HEADERS = {
    "User-Agent": MOBILE_UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "*/*",
}

VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
DANMAKU_API = "https://comment.bilibili.com/{cid}.xml"
REPLY_API = "https://api.bilibili.com/x/v2/reply/main?type=1&oid={aid}&mode={mode}&ps={ps}"
REPLY_LEGACY_API = "https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps={ps}&pn={pn}"
SEARCH_API = (
    "https://api.bilibili.com/x/web-interface/search/type"
    "?search_type=video&keyword={keyword}&page={page}&page_size={size}"
)

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_DANMAKU_RE = re.compile(r'<d p="([^"]*)">(.*?)</d>', re.S)


class BiliError(RuntimeError):
    """B 站接口不可用。"""


def extract_bvid(text: str) -> str:
    """从链接或文本里抠 BV 号。"""

    match = _BVID_RE.search(text or "")
    return match.group(1) if match else ""


async def _get(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, headers=HEADERS) as response:
        if response.status != 200:
            raise BiliError(f"HTTP {response.status}")
        return await response.read()


async def resolve_bvid(session: aiohttp.ClientSession, url: str) -> str:
    """短链（b23.tv）先跟一跳，再抠 BV 号。"""

    bvid = extract_bvid(url)
    if bvid:
        return bvid
    try:
        async with session.get(url, headers=HEADERS, allow_redirects=True) as response:
            final = str(response.url)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise BiliError(f"短链解析失败：{exc}") from exc
    return extract_bvid(final)


def _inflate(raw: bytes, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    """弹幕 XML 是裸 deflate 压的，先解压再当文本。

    解压设了上限（默认 8MB）：弹幕池可以被塞成压缩炸弹，
    没上限的话一个小响应就能把内存吃满。
    """

    if not raw:
        return ""
    if raw[:5] == b"<?xml":
        return raw[:max_bytes].decode("utf-8", "ignore")
    for wbits in (-zlib.MAX_WBITS, 16 + zlib.MAX_WBITS, zlib.MAX_WBITS):
        try:
            decompressor = zlib.decompressobj(wbits)
            chunks: list[bytes] = []
            total = 0
            data = raw
            while data and total < max_bytes:
                chunk = decompressor.decompress(data, max_bytes - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                data = decompressor.unconsumed_tail
            return b"".join(chunks).decode("utf-8", "ignore")
        except zlib.error:
            continue
    return ""


def parse_danmaku(xml_text: str, *, count: int, max_chars: int) -> list[tuple[float, str]]:
    """解析弹幕，按时间排序、去重，再均匀取样 count 条。"""

    items: list[tuple[float, str]] = []
    seen: set[str] = set()
    for param, body in _DANMAKU_RE.findall(xml_text or ""):
        text = re.sub(r"\s+", " ", body or "").strip()
        if not text or len(text) > max_chars:
            continue
        if text in seen:
            continue
        seen.add(text)
        try:
            seconds = float(param.split(",")[0])
        except (ValueError, IndexError):
            continue
        items.append((seconds, text))

    items.sort(key=lambda item: item[0])
    if count <= 0 or len(items) <= count:
        return items
    step = len(items) / float(count)
    return [items[min(len(items) - 1, int(index * step))] for index in range(count)]


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\[[^\]]{1,10}\]", "", text or "")  # 去掉 [doge] 这类表情占位
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _reply_rows(payload: dict[str, Any], *, count: int, max_chars: int, out: list[tuple[int, str, str]]) -> None:
    data = payload.get("data") or {}
    for reply in data.get("replies") or []:
        content = ((reply.get("content") or {}).get("message") or "").strip()
        text = _clean(content, max_chars)
        if not text:
            continue
        member = (reply.get("member") or {}).get("uname") or "匿名"
        out.append((int(reply.get("like") or 0), str(member), text))
        if len(out) >= count:
            return


async def _fetch_comments_main(
    session: aiohttp.ClientSession,
    aid: int,
    *,
    count: int,
    max_chars: int,
    mode: int,
) -> list[tuple[int, str, str]]:
    """新版热评接口（x/v2/reply/main），带游标翻页。风控时可能返回 code=-352。"""

    results: list[tuple[int, str, str]] = []
    pagination = ""
    remaining = max(1, min(int(count), 100))
    for _ in range(6):
        url = REPLY_API.format(aid=aid, mode=mode, ps=min(20, remaining))
        if pagination:
            url += "&pagination_str=" + quote(pagination, safe="")
        try:
            payload: dict[str, Any] = json.loads((await _get(session, url)).decode("utf-8", "ignore"))
        except (BiliError, ValueError, aiohttp.ClientError, TimeoutError):
            break
        if payload.get("code") not in (0, None):
            break
        _reply_rows(payload, count=count, max_chars=max_chars, out=results)
        if len(results) >= count:
            return results
        cursor = (payload.get("data") or {}).get("cursor") or {}
        pagination = str(cursor.get("pagination_reply") or "")
        remaining = max(1, min(int(count) - len(results), 100))
        if cursor.get("is_end") or not pagination:
            break
    return results


async def _fetch_comments_legacy(
    session: aiohttp.ClientSession,
    aid: int,
    *,
    count: int,
    max_chars: int,
) -> list[tuple[int, str, str]]:
    """旧热评接口（x/v2/reply?sort=2），匿名请求反而不会被 -352 风控。"""

    results: list[tuple[int, str, str]] = []
    for page in range(1, 4):
        url = REPLY_LEGACY_API.format(aid=aid, ps=20, pn=page)
        try:
            payload: dict[str, Any] = json.loads((await _get(session, url)).decode("utf-8", "ignore"))
        except (BiliError, ValueError, aiohttp.ClientError, TimeoutError):
            break
        if payload.get("code") not in (0, None):
            break
        before = len(results)
        _reply_rows(payload, count=count, max_chars=max_chars, out=results)
        if len(results) >= count or len(results) == before:
            break
    results.sort(key=lambda item: item[0], reverse=True)
    return results


async def fetch_comments(
    session: aiohttp.ClientSession,
    aid: int,
    *,
    count: int,
    max_chars: int,
    mode: int = 3,
) -> list[tuple[int, str, str]]:
    """取热评：``(点赞数, 昵称, 内容)``。

    先走 ``x/v2/reply/main``（最热、带游标），匿名被风控（code=-352）时
    回退到旧接口 ``x/v2/reply?sort=2``——两条路都空才算真的取不到。
    """

    results = await _fetch_comments_main(session, aid, count=count, max_chars=max_chars, mode=mode)
    if len(results) < count:
        fallback = await _fetch_comments_legacy(session, aid, count=count, max_chars=max_chars)
        seen = {(likes, text) for likes, _user, text in results}
        for row in fallback:
            if (row[0], row[2]) in seen:
                continue
            results.append(row)
            if len(results) >= count:
                break
    return results[:count]


def format_extras(
    *,
    danmaku: list[tuple[float, str]],
    comments: list[tuple[int, str, str]],
) -> str:
    """把弹幕和评论拼成给视觉模型看的一段文本。"""

    blocks: list[str] = []
    if danmaku:
        lines = [f"{_mmss(seconds)} {text}" for seconds, text in danmaku]
        blocks.append("【弹幕样本】观众实时吐槽，按时间排序，可能有重复和黑话\n" + "\n".join(lines))
    if comments:
        lines = [f"{index}. （{likes} 赞）{text}" for index, (likes, _user, text) in enumerate(comments, 1)]
        blocks.append("【热评】评论区点赞最高的几条\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _mmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


async def fetch_extras(
    url: str,
    *,
    danmaku_count: int,
    comments_count: int,
    danmaku_max_chars: int,
    comment_max_chars: int,
    comment_mode: int,
    timeout: float,
) -> str:
    """抓一条 B 站链接的弹幕 + 热评，返回拼好的文本（可能为空）。"""

    if danmaku_count <= 0 and comments_count <= 0:
        return ""

    client_timeout = aiohttp.ClientTimeout(total=max(5.0, timeout))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
            bvid = await resolve_bvid(session, url)
            if not bvid:
                return ""
            payload = json.loads((await _get(session, VIEW_API.format(bvid=bvid))).decode("utf-8", "ignore"))
            data = payload.get("data") or {}
            aid = int(data.get("aid") or 0)
            cid = int(data.get("cid") or 0)
            if not aid or not cid:
                return ""

            danmaku: list[tuple[float, str]] = []
            if danmaku_count > 0:
                raw = await _get(session, DANMAKU_API.format(cid=cid))
                danmaku = parse_danmaku(_inflate(raw), count=danmaku_count, max_chars=danmaku_max_chars)

            comments: list[tuple[int, str, str]] = []
            if comments_count > 0:
                comments = await fetch_comments(
                    session,
                    aid,
                    count=comments_count,
                    max_chars=comment_max_chars,
                    mode=comment_mode,
                )
    except (BiliError, aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        return ""

    return format_extras(danmaku=danmaku, comments=comments)


# ===== 搜索 =====

_TAG_RE = re.compile(r"<[^>]+>")


def _unescape(text: str) -> str:
    return (
        (text or "")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _clean_str(text: str) -> str:
    """搜索结果里的标题带 <em class="keyword"> 高亮和 HTML 实体，先洗一遍。"""

    return re.sub(r"\s+", " ", _unescape(_TAG_RE.sub("", text or ""))).strip()


def _seconds(duration: str) -> int:
    """搜索接口给的是 "12:34" 这种字符串。"""

    parts = [p for p in (duration or "").split(":") if p.isdigit()]
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _cover_url(pic: str) -> str:
    pic = (pic or "").strip()
    if not pic:
        return ""
    if pic.startswith("//"):
        return "https:" + pic
    if pic.startswith("http"):
        return pic
    return ""


async def search_videos(
    keyword: str,
    *,
    limit: int = 5,
    timeout: float = 15.0,
    page: int = 1,
    retries: int = 2,
) -> list[dict[str, Any]]:
    """按关键词搜 B 站视频，返回候选（标题/BV 号/时长/播放量/UP 主/封面/链接）。

    匿名搜索偶尔会被风控成空结果（code=-412），空的时候隔一秒再试一次。
    """

    keyword = (keyword or "").strip()
    if not keyword:
        return []
    limit = max(1, min(int(limit), 20))
    url = SEARCH_API.format(keyword=quote(keyword, safe=""), page=max(1, int(page)), size=limit)
    client_timeout = aiohttp.ClientTimeout(total=max(5.0, timeout))
    payload: dict[str, Any] = {}
    for attempt in range(max(1, int(retries)) + 1):
        try:
            async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
                payload = json.loads((await _get(session, url)).decode("utf-8", "ignore"))
        except (BiliError, ValueError, aiohttp.ClientError, TimeoutError):
            payload = {}
        if payload.get("code") in (0, None) and ((payload.get("data") or {}).get("result")):
            break
        if attempt < retries:
            await asyncio.sleep(0.9)
    if payload.get("code") not in (0, None):
        return []

    items: list[dict[str, Any]] = []
    for row in ((payload.get("data") or {}).get("result") or [])[:limit]:
        bvid = str(row.get("bvid") or "").strip()
        title = _clean_str(row.get("title") or "")
        if not bvid or not title:
            continue
        items.append(
            {
                "bvid": bvid,
                "title": title,
                "author": _clean_str(row.get("author") or ""),
                "duration": str(row.get("duration") or "").strip(),
                "seconds": _seconds(str(row.get("duration") or "")),
                "play": int(row.get("play") or 0),
                "danmaku": int(row.get("video_review") or 0),
                "cover": _cover_url(str(row.get("pic") or "")),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "description": _clean_str(row.get("description") or "")[:160],
            }
        )
    return items


async def video_info(bvid: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """拿单条视频的基础信息（标题/时长/UP 主/封面/aid/cid）。"""

    bvid = extract_bvid(bvid or "") or (bvid or "").strip()
    if not bvid:
        return {}
    client_timeout = aiohttp.ClientTimeout(total=max(5.0, timeout))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
            payload: dict[str, Any] = json.loads(
                (await _get(session, VIEW_API.format(bvid=bvid))).decode("utf-8", "ignore")
            )
    except (BiliError, ValueError, aiohttp.ClientError, TimeoutError):
        return {}
    data = payload.get("data") or {}
    if not data:
        return {}
    owner = data.get("owner") or {}
    return {
        "bvid": bvid,
        "aid": int(data.get("aid") or 0),
        "cid": int(data.get("cid") or 0),
        "title": _clean_str(data.get("title") or ""),
        "duration": int(data.get("duration") or 0),
        "author": _clean_str(owner.get("name") or ""),
        "cover": _cover_url(str(data.get("pic") or "")),
        "desc": _clean_str(data.get("desc") or "")[:160],
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


async def resolve_by_title(title: str, *, timeout: float = 15.0) -> str:
    """小程序卡片只有标题、没有链接时，用标题反查 BV 号。"""

    title = _clean_str(title or "")
    if not title:
        return ""
    items = await search_videos(title, limit=8, timeout=timeout)
    if not items:
        return ""

    def score(item: dict[str, Any]) -> float:
        name = item["title"]
        if name == title:
            return 3.0
        if name.startswith(title) or title.startswith(name):
            return 2.0
        return len(set(title) & set(name)) / max(1, len(set(title)))

    best = max(items, key=score)
    return best["bvid"] if score(best) >= 0.5 else ""
