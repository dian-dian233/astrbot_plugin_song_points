"""插件层冒烟测试：用假的 astrbot 模块真跑 /点歌 全流程（不需要装 AstrBot）。

必测五条 + 附加：
  1 无参数 → 帮助
  2 缺参（空歌名） → 友好提示，不是 traceback
  3 非白名单群 → 被拦截
  4 白名单/正常群 → 真跑通（扣分 + 真调音乐工具）
  5 解析函数单测（见 test_song_points_core.py）
附加：幂等防重、余额不足、播放失败退分、音乐工具缺失不扣分、账单/榜单、@机器人
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import types
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

CHECKIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_users (
    group_id     TEXT    NOT NULL,
    user_id      TEXT    NOT NULL,
    user_name    TEXT    NOT NULL DEFAULT '',
    total_points INTEGER NOT NULL DEFAULT 0,
    streak       INTEGER NOT NULL DEFAULT 0,
    max_streak   INTEGER NOT NULL DEFAULT 0,
    total_days   INTEGER NOT NULL DEFAULT 0,
    crit_count   INTEGER NOT NULL DEFAULT 0,
    last_date    TEXT,
    updated_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);
"""


def _install_astrbot_stubs() -> None:
    def _passthrough(*args, **kwargs):
        def deco(func):
            return func

        return deco

    fake_filter = types.SimpleNamespace(
        command=_passthrough,
        llm_tool=_passthrough,
        event_message_type=_passthrough,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )

    class AstrMessageEvent:  # 仅用于类型标注
        pass

    class Context:
        def __init__(self, config=None):
            self._config = config or {}
            self._mgr = None

        def get_llm_tool_manager(self):
            return self._mgr

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config or {}

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = fake_filter
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api.logger = logging.getLogger("astrbot.stub")

    astrbot.api = api
    astrbot.api.event = event_mod
    astrbot.api.star = star_mod
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
    sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")


_install_astrbot_stubs()

import main as plugin_main  # noqa: E402


class FakeAt:
    type = "At"

    def __init__(self, qq):
        self.qq = qq


class FakeMessageObj:
    def __init__(self, group_id, sender, self_id, message_id, chain=None):
        self.group_id = group_id
        self.sender = sender
        self.self_id = self_id
        self.message_id = message_id
        self.message = chain or []


class FakeEvent:
    def __init__(self, text, group="88888", sender="10001", sender_name="阿一",
                 admin=False, message_id="m1", chain=None, private=False):
        self.message_str = text
        self.message_obj = FakeMessageObj(
            "" if private else group, type("S", (), {"name": sender_name, "user_id": sender})(),
            "99999", message_id, chain,
        )
        self._admin = admin
        self.stopped = False

    def plain_result(self, text):
        return text

    def get_group_id(self):
        return self.message_obj.group_id

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.name

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True


class FakeTool:
    def __init__(self, name, handler, props=("song_name", "artist", "platform", "send_mode")):
        self.name = name
        self.handler = handler
        self.parameters = {
            "type": "object",
            "properties": {p: {"type": "string"} for p in props},
        }


class FakeManager:
    def __init__(self, tools=None):
        self.tools = tools or {}

    def get_func(self, name):
        return self.tools.get(name)


async def _collect(agen):
    return [item async for item in agen]


class PluginTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SONG_POINTS_DATA_DIR"] = self.tmp.name
        self.db = os.path.join(self.tmp.name, "checkin.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(CHECKIN_SCHEMA)
        conn.commit()
        conn.close()
        self.calls = []
        self.fail_play = False

        async def handler(event, **kwargs):
            self.calls.append(kwargs)
            if self.fail_play:
                raise RuntimeError("music backend blew up")
            return f"正在播放：{kwargs.get('song_name')}"

        self.tool = FakeTool("play_song_by_name", handler)

    def tearDown(self) -> None:
        os.environ.pop("SONG_POINTS_DATA_DIR", None)
        self.tmp.cleanup()

    def grant(self, group="88888", user="10001", points=30):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO checkin_users (group_id, user_id, user_name, total_points, updated_at)"
            " VALUES (?,?,?,?,0)"
            " ON CONFLICT(group_id, user_id) DO UPDATE SET total_points=?",
            (group, user, user, points, points),
        )
        conn.commit()
        conn.close()

    def points(self, group="88888", user="10001"):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT total_points FROM checkin_users WHERE group_id=? AND user_id=?",
            (group, user),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0

    def make_plugin(self, with_tool=True, **cfg):
        context = types.SimpleNamespace()
        tools = {"play_song_by_name": self.tool} if with_tool else {}
        manager = FakeManager(tools)
        context.get_llm_tool_manager = lambda: manager
        config = {"cost_per_song": 10, "timezone": "Asia/Shanghai", **cfg}
        return plugin_main.SongPointsPlugin(context, config)


class TestCommands(PluginTestBase):
    def test_no_args_shows_help(self) -> None:
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌"))))
        self.assertTrue(any("点歌 <歌名>" in str(x) for x in items), items)

    def test_empty_song_shows_help(self) -> None:
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌    "))))
        self.assertTrue(any("点歌 <歌名>" in str(x) for x in items), items)

    def test_success_deducts_and_plays(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天"))))
        joined = "\n".join(str(x) for x in items)
        self.assertIn("扣 10 分", joined)
        self.assertIn("剩余 20 分", joined)
        self.assertEqual(self.points(), 20)
        self.assertEqual(self.calls, [{"song_name": "晴天"}])

    def test_insufficient_is_friendly(self) -> None:
        self.grant(points=3)
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天"))))
        joined = "\n".join(str(x) for x in items)
        self.assertIn("积分不够", joined)
        self.assertIsNone(next((i for i in items if isinstance(i, Exception)), None))
        self.assertEqual(self.points(), 3)
        self.assertEqual(self.calls, [])

    def test_duplicate_message_not_double_charged(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        first = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", message_id="same"))))
        second = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", message_id="same"))))
        self.assertIn("扣 10 分", "\n".join(map(str, first)))
        self.assertIn("已经处理过", "\n".join(map(str, second)))
        self.assertEqual(self.points(), 20)
        self.assertEqual(len(self.calls), 1)

    def test_blocked_group(self) -> None:
        self.grant(group="66666", points=30)
        plugin = self.make_plugin(blocked_groups=["66666"])
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", group="66666"))))
        self.assertIn("没有开通点歌", "\n".join(map(str, items)))
        self.assertEqual(self.points(group="66666"), 30)

    def test_allowed_groups_only(self) -> None:
        self.grant(group="77777", points=30)
        plugin = self.make_plugin(allowed_groups=["77777"])
        blocked = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", group="88888"))))
        self.assertIn("没有开通点歌", "\n".join(map(str, blocked)))
        allowed = asyncio.run(
            _collect(plugin.cmd_song(FakeEvent("/点歌 晴天", group="77777", message_id="m2"))),
        )
        self.assertIn("扣 10 分", "\n".join(map(str, allowed)))
        self.assertEqual(self.points(group="77777"), 20)

    def test_music_tool_missing_no_charge(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(with_tool=False)
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天"))))
        self.assertIn("找不到音乐播放工具", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 30)

    def test_playback_failure_refunds(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        self.fail_play = True
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天"))))
        joined = "\n".join(map(str, items))
        self.assertIn("退回", joined)
        self.assertEqual(self.points(), 30)

    def test_disabled(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(enabled=False)
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天"))))
        self.assertIn("未开启", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 30)

    def test_private_blocked_by_default(self) -> None:
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", private=True))))
        self.assertIn("私聊", "\n".join(map(str, items)))

    def test_artist_split_passed_to_tool(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天 | 周杰伦"))))
        self.assertEqual(self.calls, [{"song_name": "晴天", "artist": "周杰伦"}])

    def test_history_command(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", message_id="h1"))))
        items = asyncio.run(_collect(plugin.cmd_song_history(FakeEvent("/点歌账单"))))
        joined = "\n".join(map(str, items))
        self.assertIn("点歌账单", joined)
        self.assertIn("晴天", joined)
        self.assertIn("余额 20 分", joined)

    def test_rank_command(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", message_id="r1"))))
        items = asyncio.run(_collect(plugin.cmd_song_rank(FakeEvent("/点歌榜"))))
        joined = "\n".join(map(str, items))
        self.assertIn("点歌消费榜", joined)
        self.assertIn("阿一", joined)

    def test_free_quota(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(free_daily_quota=1)
        first = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 晴天", message_id="f1"))))
        second = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/点歌 七里香", message_id="f2"))))
        self.assertIn("今日免费点歌", "\n".join(map(str, first)))
        self.assertIn("扣 10 分", "\n".join(map(str, second)))
        self.assertEqual(self.points(), 20)


class TestAtMention(PluginTestBase):
    def test_disabled_by_default(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        event = FakeEvent("晴天", chain=[FakeAt("99999")])
        items = asyncio.run(_collect(plugin.on_at_mention(event)))
        self.assertEqual(items, [])
        self.assertEqual(self.points(), 30)

    def test_enabled_handles_song(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(at_mention_enabled=True)
        event = FakeEvent("晴天", chain=[FakeAt("99999")])
        items = asyncio.run(_collect(plugin.on_at_mention(event)))
        self.assertIn("扣 10 分", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 20)
        self.assertTrue(event.stopped)

    def test_ignores_other_at(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(at_mention_enabled=True)
        event = FakeEvent("晴天", chain=[FakeAt("12345")])
        items = asyncio.run(_collect(plugin.on_at_mention(event)))
        self.assertEqual(items, [])
        self.assertEqual(self.points(), 30)


class TestLlmTools(PluginTestBase):
    def test_balance_tool(self) -> None:
        self.grant(points=42)
        plugin = self.make_plugin()
        text = asyncio.run(plugin.llm_song_points_balance(FakeEvent("查分")))
        self.assertIn("42", text)

    def test_song_tool_deducts(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()

        async def run():
            return [item async for item in plugin.llm_song_with_points(FakeEvent("点歌"), "晴天")]

        items = asyncio.run(run())
        self.assertIn("扣 10 分", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 20)


class TestSchemaAndMetadata(unittest.TestCase):
    def test_conf_schema_matches_plugin_reads(self) -> None:
        with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        for key in (
            "enabled", "cost_per_song", "free_daily_quota", "admin_free",
            "music_backend", "music_tool_name", "music_default_platform",
            "music_send_mode", "music_timeout", "refund_on_failure",
            "at_mention_enabled", "allow_private", "allowed_groups",
            "blocked_groups", "dedupe_seconds", "leaderboard_size",
            "history_size", "success_template", "free_template",
            "not_enough_template", "timezone", "checkin_plugin_name",
            "db_path_override",
        ):
            self.assertIn(key, schema)
        for key, spec in schema.items():
            self.assertIn(spec.get("type"), ("bool", "int", "float", "string", "text", "list", "dict"), key)

    def test_metadata(self) -> None:
        import re

        with open(os.path.join(PLUGIN_DIR, "metadata.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("name: astrbot_plugin_song_points", text)
        self.assertTrue(re.search(r"version: v\d+\.\d+\.\d+", text))


if __name__ == "__main__":
    unittest.main()