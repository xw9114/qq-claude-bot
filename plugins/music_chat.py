"""点歌插件。

网易云音乐的免鉴权旧版搜索接口现在会对未授权请求返回乱码歌名（反爬），
因此按歌名搜索依赖用户自行配置一个兼容 NeteaseCloudMusicApi（Binaryify）
协议的服务地址；不配置时退化为只接受歌曲 ID 或分享链接，直接走网易云的
公开直链下载，无需搜索接口。

版权提醒：把完整歌曲音频发到群里属于未经授权的音乐分发，存在版权风险，
使用者需自行承担。
"""

import base64
import re
from typing import Any

import httpx
from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from plugins.command_cooldown import SessionCooldown

config = get_driver().config


def _read_str(name: str, default: str | None) -> str | None:
    value = getattr(config, name, default)
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _read_int(name: str, default: int) -> int:
    value = getattr(config, name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        logger.warning("配置 {}={} 无效，使用默认值 {}", name.upper(), value, default)
        return default


def _read_float(name: str, default: float) -> float:
    value = getattr(config, name, default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        logger.warning("配置 {}={} 无效，使用默认值 {}", name.upper(), value, default)
        return default


MUSIC_API_BASE_URL = _read_str("music_api_base_url", None)
if MUSIC_API_BASE_URL:
    MUSIC_API_BASE_URL = MUSIC_API_BASE_URL.rstrip("/")
MUSIC_MAX_SIZE_BYTES = _read_int("music_max_size_mb", 15) * 1024 * 1024
MUSIC_COMMAND_COOLDOWN = _read_float("music_command_cooldown", 30.0)

MIN_AUDIO_BYTES = 50 * 1024  # 低于这个大小的响应视为下架/会员占位音频

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0)
DOWNLOAD_HEADERS = {"Referer": "https://music.163.com/"}

SONG_ID_PATTERN = re.compile(r"id=(\d+)")
music_cooldown = SessionCooldown(MUSIC_COMMAND_COOLDOWN)


def parse_song_id(text: str) -> int | None:
    """从裸数字 ID 或网易云分享链接里提取歌曲 ID。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = SONG_ID_PATTERN.search(text)
    if match:
        return int(match.group(1))
    return None


def music_session_key(event: MessageEvent) -> tuple:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return ("group", group_id)
    return ("private", event.user_id)


def build_direct_download_url(song_id: int) -> str:
    return f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"


def parse_search_result(payload: Any) -> tuple[int, str] | None:
    """解析 NeteaseCloudMusicApi /search 响应，取第一条结果。"""
    if not isinstance(payload, dict):
        return None
    songs = (payload.get("result") or {}).get("songs")
    if not isinstance(songs, list) or not songs:
        return None
    song = songs[0]
    if not isinstance(song, dict):
        return None
    song_id = song.get("id")
    name = str(song.get("name") or "").strip()
    if not isinstance(song_id, int) or not name:
        return None
    artists = song.get("artists")
    artist_names = []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict):
                artist_name = str(artist.get("name") or "").strip()
                if artist_name:
                    artist_names.append(artist_name)
    label = f"{name} - {'/'.join(artist_names)}" if artist_names else name
    return song_id, label


def parse_song_url_result(payload: Any) -> str | None:
    """解析 NeteaseCloudMusicApi /song/url/v1 响应，取播放直链。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    entry = data[0]
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip()


def validate_audio_response(content_type: str | None, size: int, max_bytes: int) -> bool:
    if not content_type or not content_type.lower().startswith("audio"):
        return False
    return MIN_AUDIO_BYTES <= size <= max_bytes


def parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length >= 0 else None


async def search_song(client: httpx.AsyncClient, keyword: str) -> tuple[int, str] | None:
    response = await client.get(
        f"{MUSIC_API_BASE_URL}/search", params={"keywords": keyword, "limit": 1}
    )
    response.raise_for_status()
    return parse_search_result(response.json())


async def resolve_playable_url(client: httpx.AsyncClient, song_id: int) -> str:
    if MUSIC_API_BASE_URL:
        try:
            response = await client.get(
                f"{MUSIC_API_BASE_URL}/song/url/v1",
                params={"id": song_id, "level": "standard"},
            )
            response.raise_for_status()
            url = parse_song_url_result(response.json())
            if url:
                return url
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("查询播放直链失败，回退公开直链：{}", error)
    return build_direct_download_url(song_id)


async def download_audio(client: httpx.AsyncClient, url: str) -> bytes | None:
    async with client.stream("GET", url, headers=DOWNLOAD_HEADERS) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type")
        content_length = parse_content_length(response.headers.get("content-length"))

        if content_length is not None and content_length > MUSIC_MAX_SIZE_BYTES:
            return None
        if not content_type or not content_type.lower().startswith("audio"):
            return None

        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MUSIC_MAX_SIZE_BYTES:
                return None

    if not validate_audio_response(content_type, len(content), MUSIC_MAX_SIZE_BYTES):
        return None
    return bytes(content)


point_song_cmd = on_command("点歌", priority=3, block=True)


@point_song_cmd.handle()
async def handle_point_song(event: MessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    if not text:
        await point_song_cmd.finish("用法：/点歌 [歌名/网易云歌曲ID/分享链接]")

    song_label: str | None = None
    song_id = parse_song_id(text)
    if song_id is None and not MUSIC_API_BASE_URL:
        await point_song_cmd.finish(
            "❌ 未配置搜索源，请直接发送网易云歌曲 ID 或分享链接"
            "（如 https://music.163.com/song?id=xxxxx）"
        )

    retry_after = music_cooldown.retry_after(music_session_key(event))
    if retry_after is not None:
        await point_song_cmd.finish(f"点歌太频繁了，请 {retry_after} 秒后再试")

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
    ) as client:
        if song_id is None:
            try:
                result = await search_song(client, text)
            except httpx.HTTPError as error:
                logger.warning("点歌搜索失败：{}", error)
                await point_song_cmd.finish("❌ 搜索失败，请稍后重试")
            if result is None:
                await point_song_cmd.finish("没找到这首歌，换个关键词或直接发链接试试")
            song_id, song_label = result

        try:
            play_url = await resolve_playable_url(client, song_id)
            audio = await download_audio(client, play_url)
        except httpx.HTTPError as error:
            logger.warning("点歌下载失败：{}", error)
            await point_song_cmd.finish("❌ 下载失败，请稍后重试")

    if audio is None:
        await point_song_cmd.finish("这首歌可能是会员专享或已下架，换一首试试")

    if song_label:
        await point_song_cmd.send(f"🎵 {song_label}")

    encoded = base64.b64encode(audio).decode()
    await point_song_cmd.finish(MessageSegment.record(f"base64://{encoded}"))
