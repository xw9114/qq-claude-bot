import unittest

import httpx
import nonebot


nonebot.init()

import plugins.music_chat as music_chat  # noqa: E402


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.iterated_chunks = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.iterated_chunks += 1
            yield chunk

    async def aclose(self):
        pass


class ParseSongIdTest(unittest.TestCase):
    def test_bare_digits(self):
        self.assertEqual(music_chat.parse_song_id("3339230677"), 3339230677)

    def test_song_page_link(self):
        self.assertEqual(
            music_chat.parse_song_id("https://music.163.com/song?id=3339230677"),
            3339230677,
        )

    def test_hash_link(self):
        self.assertEqual(
            music_chat.parse_song_id("https://music.163.com/#/song?id=3339230677"),
            3339230677,
        )

    def test_share_text_with_extra_content(self):
        text = "分享周杰伦的单曲《晴天》http://y.music.163.com/m/song?id=3339230677&app_version=8 (@网易云音乐)"
        self.assertEqual(music_chat.parse_song_id(text), 3339230677)

    def test_song_name_without_id_returns_none(self):
        self.assertIsNone(music_chat.parse_song_id("晴天 周杰伦"))

    def test_empty_text_returns_none(self):
        self.assertIsNone(music_chat.parse_song_id("   "))


class BuildDirectDownloadUrlTest(unittest.TestCase):
    def test_builds_expected_url(self):
        self.assertEqual(
            music_chat.build_direct_download_url(123),
            "https://music.163.com/song/media/outer/url?id=123.mp3",
        )


class ParseSearchResultTest(unittest.TestCase):
    def test_parses_song_with_artists(self):
        payload = {
            "result": {
                "songs": [
                    {
                        "id": 3339230677,
                        "name": "晴天",
                        "artists": [{"name": "周杰伦"}, {"name": "A-LNK"}],
                    }
                ]
            }
        }
        self.assertEqual(
            music_chat.parse_search_result(payload),
            (3339230677, "晴天 - 周杰伦/A-LNK"),
        )

    def test_parses_song_without_artists(self):
        payload = {"result": {"songs": [{"id": 1, "name": "无名"}]}}
        self.assertEqual(music_chat.parse_search_result(payload), (1, "无名"))

    def test_missing_songs_returns_none(self):
        self.assertIsNone(music_chat.parse_search_result({"result": {}}))
        self.assertIsNone(music_chat.parse_search_result({"result": {"songs": []}}))

    def test_malformed_payload_returns_none(self):
        self.assertIsNone(music_chat.parse_search_result(None))
        self.assertIsNone(music_chat.parse_search_result("oops"))
        self.assertIsNone(
            music_chat.parse_search_result({"result": {"songs": [{"name": "缺id"}]}})
        )
        self.assertIsNone(
            music_chat.parse_search_result({"result": {"songs": [{"id": 1}]}})
        )


class ParseSongUrlResultTest(unittest.TestCase):
    def test_parses_valid_url(self):
        payload = {"data": [{"id": 1, "url": "https://example.com/a.mp3"}]}
        self.assertEqual(
            music_chat.parse_song_url_result(payload), "https://example.com/a.mp3"
        )

    def test_null_url_returns_none_for_vip_only_tracks(self):
        payload = {"data": [{"id": 1, "url": None}]}
        self.assertIsNone(music_chat.parse_song_url_result(payload))

    def test_malformed_payload_returns_none(self):
        self.assertIsNone(music_chat.parse_song_url_result(None))
        self.assertIsNone(music_chat.parse_song_url_result({"data": []}))
        self.assertIsNone(music_chat.parse_song_url_result({"data": ["oops"]}))
        self.assertIsNone(music_chat.parse_song_url_result({"data": [{"url": "  "}]}))


class ValidateAudioResponseTest(unittest.TestCase):
    def test_valid_audio(self):
        self.assertTrue(
            music_chat.validate_audio_response("audio/mpeg", 200 * 1024, 15 * 1024 * 1024)
        )

    def test_rejects_non_audio_content_type(self):
        self.assertFalse(
            music_chat.validate_audio_response("text/html", 200 * 1024, 15 * 1024 * 1024)
        )

    def test_rejects_missing_content_type(self):
        self.assertFalse(
            music_chat.validate_audio_response(None, 200 * 1024, 15 * 1024 * 1024)
        )

    def test_rejects_placeholder_sized_audio(self):
        self.assertFalse(
            music_chat.validate_audio_response("audio/mpeg", 1024, 15 * 1024 * 1024)
        )

    def test_rejects_oversized_audio(self):
        self.assertFalse(
            music_chat.validate_audio_response(
                "audio/mpeg", 20 * 1024 * 1024, 15 * 1024 * 1024
            )
        )


class ParseContentLengthTest(unittest.TestCase):
    def test_parses_valid_content_length(self):
        self.assertEqual(music_chat.parse_content_length("123"), 123)

    def test_ignores_missing_invalid_or_negative_content_length(self):
        self.assertIsNone(music_chat.parse_content_length(None))
        self.assertIsNone(music_chat.parse_content_length("abc"))
        self.assertIsNone(music_chat.parse_content_length("-1"))


class DownloadAudioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._max_size = music_chat.MUSIC_MAX_SIZE_BYTES

    def tearDown(self):
        music_chat.MUSIC_MAX_SIZE_BYTES = self._max_size

    async def test_downloads_audio_within_limit(self):
        music_chat.MUSIC_MAX_SIZE_BYTES = 100 * 1024
        audio = b"a" * (60 * 1024)
        stream = CountingStream([audio])

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "audio/mpeg"},
                stream=stream,
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual(await music_chat.download_audio(client, "https://test"), audio)

    async def test_rejects_oversized_content_length_without_reading_body(self):
        music_chat.MUSIC_MAX_SIZE_BYTES = 100
        stream = CountingStream([b"a" * 101])

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "audio/mpeg", "content-length": "101"},
                stream=stream,
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertIsNone(await music_chat.download_audio(client, "https://test"))
        self.assertEqual(stream.iterated_chunks, 0)

    async def test_stops_stream_when_body_exceeds_limit(self):
        music_chat.MUSIC_MAX_SIZE_BYTES = 100
        stream = CountingStream([b"a" * 80, b"b" * 21, b"c"])

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "audio/mpeg"},
                stream=stream,
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertIsNone(await music_chat.download_audio(client, "https://test"))
        self.assertEqual(stream.iterated_chunks, 2)


if __name__ == "__main__":
    unittest.main()
