"""插件层冒烟测试：用假的 astrbot 模块真跑 /赏歌 全流程（不需要装 AstrBot）。

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


REGISTERED_COMMANDS: list[dict] = []
REGISTERED_LLM_TOOLS: list[str] = []


def _install_astrbot_stubs() -> None:
    def _passthrough(*args, **kwargs):
        def deco(func):
            return func

        return deco

    def _record_command(name, alias=None, *args, **kwargs):
        """记录指令注册，用于断言「旧指令没注册 / 新指令只有一个 handler」。"""
        aliases = tuple(alias or ())

        def deco(func):
            REGISTERED_COMMANDS.append(
                {"name": name, "alias": aliases, "func": func},
            )
            return func

        return deco

    def _record_llm_tool(name=None, *args, **kwargs):
        def deco(func):
            REGISTERED_LLM_TOOLS.append(name)
            return func

        return deco

    fake_filter = types.SimpleNamespace(
        command=_record_command,
        llm_tool=_record_llm_tool,
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


# ---------------------------------------------------------------- 指令冲突验证
# 复刻框架侧行为，用来证明「旧指令不再注册、新指令只有一个 handler」。
# 依据：astrbot/core/pipeline/waking_check/stage.py（剥唤醒前缀 wake_prefix）、
#       astrbot/core/star/filter/command.py:210（message_str.startswith(full_cmd + " ") 或相等）。
MUSIC_PLUGIN_COMMAND = "点歌"
MUSIC_PLUGIN_KEYWORDS = ("youtube点歌", "油管点歌", "yt点歌")


def wake_and_strip(text: str, wake_prefixes=("/",)) -> tuple[str, bool]:
    s = (text or "").strip()
    for prefix in wake_prefixes:
        if s.startswith(prefix):
            return s[len(prefix):].strip(), True
    return s, False


def _full_names(reg: dict) -> list[str]:
    return [reg["name"], *reg["alias"]]


def dispatch(text: str) -> list[dict]:
    """复刻 CommandFilter 的匹配：命中的 handler 全部返回（用来数「有几个 handler 会跑」）。"""
    import re

    msg, woke = wake_and_strip(text)
    if not woke:
        return []
    msg = re.sub(r"\s+", " ", msg.strip())
    hits = []
    for reg in REGISTERED_COMMANDS:
        for full in _full_names(reg):
            if msg == full or msg.startswith(full + " "):
                hits.append(reg)
    return hits


def music_plugin_takes(text: str) -> bool:
    """复刻 astrbot_plugin_music_youtube.on_search_song 的前置判断（cmd == 点歌 / 平台关键词）。"""
    msg, woke = wake_and_strip(text)
    if not woke:
        return False
    cmd, _, arg = msg.partition(" ")
    if not arg:
        return False
    if cmd == MUSIC_PLUGIN_COMMAND:
        return True
    return any(kw.lower() in cmd.lower() for kw in MUSIC_PLUGIN_KEYWORDS)


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
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌"))))
        self.assertTrue(any("赏歌 <歌名>" in str(x) for x in items), items)

    def test_empty_song_shows_help(self) -> None:
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌    "))))
        self.assertTrue(any("赏歌 <歌名>" in str(x) for x in items), items)

    def test_success_deducts_and_plays(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天"))))
        joined = "\n".join(str(x) for x in items)
        self.assertIn("扣 10 分", joined)
        self.assertIn("剩余 20 分", joined)
        self.assertEqual(self.points(), 20)
        self.assertEqual(self.calls, [{"song_name": "晴天"}])

    def test_insufficient_is_friendly(self) -> None:
        self.grant(points=3)
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天"))))
        joined = "\n".join(str(x) for x in items)
        self.assertIn("积分不够", joined)
        self.assertIsNone(next((i for i in items if isinstance(i, Exception)), None))
        self.assertEqual(self.points(), 3)
        self.assertEqual(self.calls, [])

    def test_duplicate_message_not_double_charged(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        first = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", message_id="same"))))
        second = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", message_id="same"))))
        self.assertIn("扣 10 分", "\n".join(map(str, first)))
        self.assertIn("已经处理过", "\n".join(map(str, second)))
        self.assertEqual(self.points(), 20)
        self.assertEqual(len(self.calls), 1)

    def test_blocked_group(self) -> None:
        self.grant(group="66666", points=30)
        plugin = self.make_plugin(blocked_groups=["66666"])
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", group="66666"))))
        self.assertIn("没有开通点歌", "\n".join(map(str, items)))
        self.assertEqual(self.points(group="66666"), 30)

    def test_allowed_groups_only(self) -> None:
        self.grant(group="77777", points=30)
        plugin = self.make_plugin(allowed_groups=["77777"])
        blocked = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", group="88888"))))
        self.assertIn("没有开通点歌", "\n".join(map(str, blocked)))
        allowed = asyncio.run(
            _collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", group="77777", message_id="m2"))),
        )
        self.assertIn("扣 10 分", "\n".join(map(str, allowed)))
        self.assertEqual(self.points(group="77777"), 20)

    def test_music_tool_missing_no_charge(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(with_tool=False)
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天"))))
        self.assertIn("找不到音乐播放工具", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 30)

    def test_playback_failure_refunds(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        self.fail_play = True
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天"))))
        joined = "\n".join(map(str, items))
        self.assertIn("退回", joined)
        self.assertEqual(self.points(), 30)

    def test_disabled(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(enabled=False)
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天"))))
        self.assertIn("未开启", "\n".join(map(str, items)))
        self.assertEqual(self.points(), 30)

    def test_private_blocked_by_default(self) -> None:
        plugin = self.make_plugin()
        items = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", private=True))))
        self.assertIn("私聊", "\n".join(map(str, items)))

    def test_artist_split_passed_to_tool(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天 | 周杰伦"))))
        self.assertEqual(self.calls, [{"song_name": "晴天", "artist": "周杰伦"}])

    def test_history_command(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", message_id="h1"))))
        items = asyncio.run(_collect(plugin.cmd_song_history(FakeEvent("/点歌账单"))))
        joined = "\n".join(map(str, items))
        self.assertIn("点歌账单", joined)
        self.assertIn("晴天", joined)
        self.assertIn("余额 20 分", joined)

    def test_rank_command(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", message_id="r1"))))
        items = asyncio.run(_collect(plugin.cmd_song_rank(FakeEvent("/点歌榜"))))
        joined = "\n".join(map(str, items))
        self.assertIn("点歌消费榜", joined)
        self.assertIn("阿一", joined)

    def test_free_quota(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(free_daily_quota=1)
        first = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 晴天", message_id="f1"))))
        second = asyncio.run(_collect(plugin.cmd_song(FakeEvent("/赏歌 七里香", message_id="f2"))))
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
    def test_initialize_runs_without_error(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        asyncio.run(plugin.initialize())  # 启动清扫不应炸
        self.assertEqual(self.points(), 30)

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


class TestCommandSurface(PluginTestBase):
    """v1.0.2：主指令改成 /赏歌，证明旧 /点歌 不再注册、新指令只有一个 handler。"""

    def full_name_set(self) -> set[str]:
        names: set[str] = set()
        for reg in REGISTERED_COMMANDS:
            names.update(_full_names(reg))
        return names

    def test_old_song_command_not_registered(self) -> None:
        names = self.full_name_set()
        for old in ("点歌", "song", "点歌台"):
            self.assertNotIn(old, names, f"旧指令 {old} 仍在注册表里")
        # 也不允许用「/点歌 xxx」式的新名字绕过
        self.assertNotIn("/点歌", names)

    def test_new_song_command_has_exactly_one_handler(self) -> None:
        song_handlers = [r for r in REGISTERED_COMMANDS if r["name"] == "赏歌"]
        self.assertEqual(len(song_handlers), 1, song_handlers)
        self.assertEqual(set(song_handlers[0]["alias"]), {"积分点歌", "金曲"})

    def test_dispatch_sing_command_hits_one_handler_and_charges_once(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin()
        hits = dispatch("/赏歌 晴天")
        self.assertEqual(len(hits), 1, [h["name"] for h in hits])
        self.assertEqual(hits[0]["func"].__name__, "cmd_song")
        items = asyncio.run(_collect(hits[0]["func"](plugin, FakeEvent("/赏歌 晴天"))))
        joined = "\n".join(map(str, items))
        self.assertIn("扣 10 分", joined)
        self.assertEqual(self.points(), 20)
        self.assertEqual(len(self.calls), 1, self.calls)  # 只播一次

    def test_dispatch_old_command_hits_nothing_from_plugin(self) -> None:
        for text in ("/点歌 晴天", "/song 晴天", "/点歌台 晴天", "/点歌"):
            self.assertEqual(dispatch(text), [], text)

    def test_aliases_hit_song_handler(self) -> None:
        for text in ("/积分点歌 晴天", "/金曲 晴天"):
            hits = dispatch(text)
            self.assertEqual(len(hits), 1, text)
            self.assertEqual(hits[0]["func"].__name__, "cmd_song", text)

    def test_bill_and_rank_kept_with_single_handler(self) -> None:
        self.assertIn("点歌账单", self.full_name_set())
        self.assertIn("点歌榜", self.full_name_set())
        bill = dispatch("/点歌账单")
        rank = dispatch("/点歌榜 今日")
        self.assertEqual([h["func"].__name__ for h in bill], ["cmd_song_history"])
        self.assertEqual([h["func"].__name__ for h in rank], ["cmd_song_rank"])

    def test_music_plugin_ignores_all_our_commands(self) -> None:
        """音乐插件只看 cmd == 点歌 / 平台关键词；我们的每个名字都要让它判定「不归我」。"""
        for name in sorted(self.full_name_set()):
            self.assertFalse(music_plugin_takes(f"/{name} 晴天"), name)
        # 反向确认这套判定确实能认出音乐插件的指令（否则上面的断言是空转）
        self.assertTrue(music_plugin_takes("/点歌 晴天"))
        self.assertTrue(music_plugin_takes("/youtube点歌 晴天"))

    def test_at_mention_yields_to_music_plugin(self) -> None:
        self.grant(points=30)
        plugin = self.make_plugin(at_mention_enabled=True)
        # 「@bot 点歌 X」整句让给音乐插件：不扣分、不出消息、不 stop_event
        event = FakeEvent("点歌 晴天", chain=[FakeAt("99999")])
        items = asyncio.run(_collect(plugin.on_at_mention(event)))
        self.assertEqual(items, [])
        self.assertEqual(self.points(), 30)
        self.assertFalse(event.stopped)
        # 老别名 song / 点歌台 谁都不认，跳过而不是把「song 晴天」当歌名
        for legacy in ("song 晴天", "点歌台 晴天"):
            ev = FakeEvent(legacy, chain=[FakeAt("99999")])
            self.assertEqual(asyncio.run(_collect(plugin.on_at_mention(ev))), [], legacy)
            self.assertEqual(self.points(), 30, legacy)
        # 本插件自己的 @ 触发照旧可用
        event2 = FakeEvent("赏歌 晴天", chain=[FakeAt("99999")])
        items2 = asyncio.run(_collect(plugin.on_at_mention(event2)))
        self.assertIn("扣 10 分", "\n".join(map(str, items2)))
        self.assertEqual(self.points(), 20)

    def test_llm_tool_names_unchanged(self) -> None:
        self.assertIn("song_with_points", REGISTERED_LLM_TOOLS)
        self.assertIn("song_points_balance", REGISTERED_LLM_TOOLS)


class TestSchemaAndMetadata(unittest.TestCase):
    def test_conf_schema_matches_plugin_reads(self) -> None:
        with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        for key in (
            "enabled", "cost_per_song", "free_daily_quota", "admin_free",
            "music_backend", "music_tool_name", "music_default_platform",
            "music_send_mode", "music_timeout", "refund_on_failure",
            "stale_pending_seconds", "at_mention_enabled", "allow_private", "allowed_groups",
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