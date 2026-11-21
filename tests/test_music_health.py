"""music_health 单测（纯标准库，不需要 AstrBot、不需要 pytest）。

运行：python3 tests/test_music_health.py
覆盖：真实音乐插件的成功/失败返回串 → 判定；音源探测的 4 种来源；工具实例反查。
"""

from __future__ import annotations

import functools
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music_health import (  # noqa: E402
    SourceProbe,
    classify_play_result,
    extract_sources_from_instance,
    looks_like_no_source,
    parse_sources_from_text,
    tool_instance,
)


class FakePlayer:
    def __init__(self, name, display_name, priority=1):
        self.platform = types.SimpleNamespace(name=name, display_name=display_name)
        self.platform_cfg = types.SimpleNamespace(priority=priority)


class FakeInstance:
    """复刻 astrbot_plugin_music v3.2.6 的实例结构（三种查询方式都有）。"""

    def __init__(self, players, mode="priority"):
        self.players = list(players)
        self.mode = mode

    def get_players_by_priority(self):
        if self.mode != "priority":
            raise AttributeError("nope")
        return sorted(self.players, key=lambda p: p.platform_cfg.priority)

    def get_player(self, name=None, word=None, default=False):
        if self.mode != "default":
            raise AttributeError("nope")
        if not default:
            return None
        return self.players[0] if self.players else None


class TestClassifyRealWorldStrings(unittest.TestCase):
    """判定依据全部来自 astrbot_plugin_music v3.2.6 的真实 return 语句。"""

    def test_success_string(self) -> None:
        verdict, _ = classify_play_result(
            "已通过音源「酷我音乐（kuwo）」发送《晴天 - 周杰伦》"
        )
        self.assertEqual(verdict, "ok")

    def test_playing_string(self) -> None:
        verdict, _ = classify_play_result("正在播放: 晴天 - 周杰伦")
        self.assertEqual(verdict, "ok")

    def test_platform_unavailable_is_failure(self) -> None:
        verdict, note = classify_play_result("音源「netease」不可用；指定音源时不会自动回退")
        self.assertEqual(verdict, "failed")
        self.assertIn("不可用", note)

    def test_not_found_listing_sources_is_failure(self) -> None:
        text = "未找到与《晴天》可靠匹配的歌曲；已尝试音源：网易云音乐、酷狗音乐"
        verdict, _ = classify_play_result(text)
        self.assertEqual(verdict, "failed")
        self.assertEqual(parse_sources_from_text(text), ["网易云音乐", "酷狗音乐"])

    def test_no_player_is_failure_and_no_source(self) -> None:
        verdict, _ = classify_play_result("无可用播放器")
        self.assertEqual(verdict, "failed")
        self.assertTrue(looks_like_no_source("无可用播放器"))

    def test_no_source_in_attempted_list(self) -> None:
        text = "未找到与《x》可靠匹配的歌曲；已尝试音源：无可用音源"
        self.assertTrue(looks_like_no_source(text))
        self.assertEqual(classify_play_result(text)[0], "failed")

    def test_send_failed_is_failure(self) -> None:
        verdict, _ = classify_play_result("音源「网易云音乐（netease）」发送《晴天》失败")
        self.assertEqual(verdict, "failed")

    def test_traditional_chinese_failure(self) -> None:
        for text in ("播放失敗", "無可用播放器", "沒找到 '晴天'"):
            self.assertEqual(classify_play_result(text)[0], "failed", text)

    def test_unknown_send_mode_is_failure(self) -> None:
        verdict, _ = classify_play_result(
            "未知发送方式，请使用 voice、record_link、ark_card、card、file_local、file_link 或 text"
        )
        self.assertEqual(verdict, "failed")

    def test_success_marker_beats_failure_word_in_song_name(self) -> None:
        verdict, _ = classify_play_result("已通过音源「a」发送《未找到的你 - 张三》")
        self.assertEqual(verdict, "ok")

    def test_none_is_success_by_default(self) -> None:
        # astrbot_plugin_music 关闭 return_play_song_result 时，发送成功就 return None
        self.assertEqual(classify_play_result(None)[0], "ok")
        self.assertEqual(classify_play_result("")[0], "ok")

    def test_none_refund_when_configured(self) -> None:
        self.assertEqual(classify_play_result(None, empty_action="refund")[0], "failed")

    def test_media_yield_counts_as_success(self) -> None:
        verdict, note = classify_play_result(None, extra_count=2)
        self.assertEqual(verdict, "ok")
        self.assertEqual(note, "media delivered")

    def test_unknown_text_default_ok_but_refund_option(self) -> None:
        self.assertEqual(classify_play_result("好的呀")[0], "ok")
        self.assertEqual(classify_play_result("好的呀", unknown_action="refund")[0], "failed")


class TestParseSources(unittest.TestCase):
    def test_parse_with_colon_variants(self) -> None:
        self.assertEqual(
            parse_sources_from_text("xx；已尝试音源: A、B。"), ["A", "B"]
        )
        self.assertEqual(parse_sources_from_text("已尝试音源：无可用音源"), ["无可用音源"])

    def test_parse_nothing(self) -> None:
        self.assertEqual(parse_sources_from_text("已通过音源「a」发送《晴天》"), [])
        self.assertEqual(parse_sources_from_text(None), [])


class TestExtractSources(unittest.TestCase):
    def test_priority_method_lists_sources(self) -> None:
        inst = FakeInstance([FakePlayer("kuwo", "酷我音乐"), FakePlayer("netease", "网易云音乐", 2)])
        probe = extract_sources_from_instance(inst)
        self.assertTrue(probe.available)
        self.assertEqual(probe.method, "get_players_by_priority")
        self.assertEqual(probe.sources, ["酷我音乐(kuwo)", "网易云音乐(netease)"])

    def test_empty_players_means_no_source(self) -> None:
        probe = extract_sources_from_instance(FakeInstance([]))
        self.assertFalse(probe.available)
        self.assertEqual(probe.sources, [])
        self.assertIn("没有", probe.summary())

    def test_get_player_fallback(self) -> None:
        inst = FakeInstance([FakePlayer("qqmusic", "QQ音乐")], mode="default")
        probe = extract_sources_from_instance(inst)
        self.assertTrue(probe.available)
        self.assertEqual(probe.method, "get_player")

    def test_players_attr_fallback(self) -> None:
        inst = types.SimpleNamespace(players=[FakePlayer("kugou", "酷狗音乐")])
        probe = extract_sources_from_instance(inst)
        self.assertTrue(probe.available)
        self.assertEqual(probe.method, "players")

    def test_cfg_enabled_platforms_fallback(self) -> None:
        inst = types.SimpleNamespace(
            cfg=types.SimpleNamespace(
                players=types.SimpleNamespace(enabled_platforms=lambda: ["netease"])
            )
        )
        probe = extract_sources_from_instance(inst)
        self.assertTrue(probe.available)
        self.assertEqual(probe.method, "cfg.players.enabled_platforms")
        self.assertEqual(probe.sources, ["netease"])

    def test_no_instance_is_unknown(self) -> None:
        probe = extract_sources_from_instance(None)
        self.assertIsNone(probe.available)
        self.assertIn("未知", probe.summary())

    def test_broken_instance_is_unknown_not_crash(self) -> None:
        class Broken:
            def get_players_by_priority(self):
                raise RuntimeError("boom")

            get_player = None
            players = None

        probe = extract_sources_from_instance(Broken())
        self.assertIsNone(probe.available)

    def test_async_getter_is_unknown(self) -> None:
        class AsyncBroken:
            async def get_players_by_priority(self):
                return []

        probe = extract_sources_from_instance(AsyncBroken())
        self.assertIsNone(probe.available)


class TestToolInstance(unittest.TestCase):
    def test_partial_gives_plugin_instance(self) -> None:
        inst = FakeInstance([])

        async def handler(self_, event, **kwargs):
            return "ok"

        tool = types.SimpleNamespace(handler=functools.partial(handler, inst))
        self.assertIs(tool_instance(tool), inst)

    def test_bound_method_gives_instance(self) -> None:
        class Holder:
            async def handler(self, event):
                return "ok"

        holder = Holder()
        tool = types.SimpleNamespace(handler=holder.handler)
        self.assertIs(tool_instance(tool), holder)

    def test_missing_handler(self) -> None:
        self.assertIsNone(tool_instance(types.SimpleNamespace(handler=None)))


class TestProbeSummary(unittest.TestCase):
    def test_summary_variants(self) -> None:
        self.assertIn("音源可用", SourceProbe(available=True, sources=["a", "b"]).summary())
        self.assertIn("音源不可用", SourceProbe(available=False).summary())
        self.assertIn("未知", SourceProbe(available=None, detail="x").summary())


if __name__ == "__main__":
    unittest.main()