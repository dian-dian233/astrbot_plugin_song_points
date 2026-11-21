"""AstrBot 点歌消费积分插件（把签到积分当货币花）。

指令（v1.0.2 起主指令改为 /赏歌，避开音乐插件的 /点歌）：
  /赏歌 <歌名>    校验积分 → 扣分 → 调音乐插件真播放 → 只认「真的放出歌」才算成功
  /赏歌诊断       管理员音源诊断：工具是否注册、有没有可用音源、最近一次失败原因
  /点歌账单        查自己的点歌记录与余额（与音乐插件不冲突，保留原名）
  /点歌榜 [今日]   本群点歌消费榜（同上，保留原名）
  /赏歌            不带参 → 帮助

v1.0.3：修「扣了分却放不出歌」
  · 扣分前做音源预检（从音乐插件实例直接读音源列表，零副作用），
    没有任何可用音源就一分不扣，直接告诉用户/管理员。
  · 扣分后判定播放结果：音乐插件的失败也是 return 字符串（不抛异常），
    所以扫失败特征串 → 退分 + 人话原因；只有明确成功（或有音频产物）才记已播放。
  · 详见 music_health.py 头部（真实插件返回文本的取证）。

别名：/积分点歌、/金曲（均不与音乐插件的 /点歌、/song、/点歌台 重名）

@机器人 + 歌名：需在配置里打开 at_mention_enabled（默认关）。

积分底层直接复用签到插件 astrbot_plugin_checkin 的 checkin.db：
余额 = `checkin_users.total_points`，扣分与签到加分落在同一个数上，不另起货币。

数据：`data/plugin_data/astrbot_plugin_checkin/checkin.db`
（本插件只新增 song_order_log / song_free_usage 两张自己的表，不动签到插件的表结构）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import uuid
from typing import Any, Optional

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:  # 保证同目录模块可被导入
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot 4.x 统一导出
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot.plugin.song_points")

from song_points_core import (  # noqa: E402
    OUTCOME_DUPLICATE,
    OUTCOME_FREE,
    OUTCOME_INSUFFICIENT,
    OUTCOME_NO_LEDGER,
    OUTCOME_OK,
    InvalidArgument,
    OrderOutcome,
    Settings,
    SongPointsError,
    SongPointsStore,
    day_start_ts,
    format_history,
    format_insufficient,
    format_ranking,
    format_success,
    normalize_song,
    resolve_db_path,
    strip_command_prefix,
)

from music_health import (  # noqa: E402
    SourceProbe,
    classify_play_result,
    extract_sources_from_instance,
    looks_like_no_source,
    parse_sources_from_text,
    tool_instance,
)

PLUGIN_NAME = "astrbot_plugin_song_points"
PLUGIN_VERSION = "v1.0.3"

# v1.0.2：主指令从 /点歌 迁到 /赏歌。旧名 /点歌、/song、/点歌台 与音乐插件撞车，
# 一律不再注册；别名同样避开这三个名字。
SING_COMMAND = "赏歌"
SING_ALIASES = ("积分点歌", "金曲")
SING_TRIGGERS = (SING_COMMAND,) + SING_ALIASES

# 音乐插件真正会接手的触发词（astrbot_plugin_music_youtube.on_search_song：cmd == "点歌"，
# 或 cmd 命中平台关键词 youtube点歌/油管点歌/yt点歌）。@机器人 触发时必须整句让给它，
# 否则「@bot 点歌 晴天」会被两边各播一遍。
MUSIC_PLUGIN_TRIGGERS = ("点歌", "youtube点歌", "油管点歌", "yt点歌")
# @机器人 触发时要跳过的前缀：前四个让给音乐插件；song/点歌台 谁都不认，
# 跳过是为了不把「song 晴天」整句当成歌名去搜。
AT_MENTION_SKIP_PREFIXES = MUSIC_PLUGIN_TRIGGERS + ("点歌台", "song")


def _starts_with_any(text: str, prefixes: tuple[str, ...]) -> bool:
    """text 是否以任一触发词开头（大小写不敏感，忽略前导空白）。"""
    head = (text or "").strip().lower()
    return any(head.startswith(p.lower()) for p in prefixes if p)


DEFAULT_SUCCESS_TEMPLATE = "✅ 点歌成功｜{song}｜扣 {cost} 分｜剩余 {balance} 分"
DEFAULT_FREE_TEMPLATE = "🎁 今日免费点歌｜{song}｜免费次数剩 {free_left} 次｜余额 {balance} 分"
DEFAULT_INSUFFICIENT_TEMPLATE = (
    "⚠️ 积分不够点歌｜「{song}」要 {cost} 分，你现在 {balance} 分｜先发 /签到 攒分吧"
)
DEFAULT_NO_SOURCE_TEMPLATE = (
    "🎵 音乐插件现在没有可用音源，这次不扣分。\n"
    "请管理员在音乐插件配置里至少启用一个音源（例如 netease / qqmusic / kuwo）后再点歌。"
)
DEFAULT_FAILURE_TEMPLATE = (
    "⚠️ 「{song}」没放出来，已把 {cost} 分退回｜余额 {balance} 分\n原因：{reason}"
)

HELP_TEXT = (
    "🎵 赏歌（消耗签到积分）\n"
    "· /赏歌 <歌名>      花积分点一首，例如：/赏歌 晴天\n"
    "· /点歌账单          看自己的点歌记录 + 余额\n"
    "· /点歌榜 [今日]     看本群消费榜\n"
    "积分来自 /签到，余额不足就去签到。\n"
    "（/点歌 是音乐插件的指令，本插件不占用）"
)


def _fill(template: str, **kwargs: object) -> str:
    """模板占位符填充；模板写错也不让指令炸掉。"""
    try:
        return str(template).format(**kwargs)
    except Exception:
        return str(template)


@register(PLUGIN_NAME, "dian-dian233", "点歌消耗签到积分", PLUGIN_VERSION)
class SongPointsPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self._store: Optional[SongPointsStore] = None
        self._store_key = ""
        self._probe: Optional[SourceProbe] = None
        self._last_failure: str = ""
        self._build_store()
        logger.info(
            "[song_points] 已加载 v%s，积分库：%s",
            PLUGIN_VERSION,
            self.store.db_path,
        )

    # ------------------------------------------------------------ 配置/存储
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            data = self.config
            if hasattr(data, "get"):
                value = data.get(key, default)
                return default if value is None else value
        except Exception:
            pass
        return default

    def _settings(self) -> Settings:
        return Settings.from_config(self.config)

    def _build_store(self) -> None:
        settings = self._settings()
        db_path = resolve_db_path(settings)
        key = str(db_path)
        if self._store is not None and self._store_key == key:
            self._store.settings = settings
            return
        store = SongPointsStore(db_path, settings)
        try:
            store.init_db()
        except Exception as exc:  # 建表失败也不让插件加载失败
            logger.error("[song_points] 初始化积分库失败：%s", exc)
        self._store = store
        self._store_key = key

    @property
    def store(self) -> SongPointsStore:
        self._build_store()
        assert self._store is not None
        return self._store

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _at_mention_enabled(self) -> bool:
        return bool(self._cfg("at_mention_enabled", False))

    def _allow_private(self) -> bool:
        return bool(self._cfg("allow_private", False))

    # ------------------------------------------------------------ 事件工具
    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", None)
        if not text:
            msg_obj = getattr(event, "message_obj", None)
            text = getattr(msg_obj, "message_str", "")
        return (text or "").strip()

    @staticmethod
    def _scope(event: AstrMessageEvent) -> Optional[str]:
        """群聊用 group_id；私聊（需配置允许）用 private:<uid>，与签到插件同口径。"""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid:
            return gid
        try:
            uid = str(event.get_sender_id())
        except Exception:
            return None
        return f"private:{uid}"

    @staticmethod
    def _sender_meta(event: AstrMessageEvent) -> tuple[str, str]:
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = "unknown"
        try:
            name = str(event.get_sender_name() or "") or uid
        except Exception:
            name = uid
        return uid, name

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _message_id(self, event: AstrMessageEvent) -> str:
        for holder in (getattr(event, "message_obj", None), event):
            for attr in ("message_id", "msg_id", "id"):
                try:
                    value = getattr(holder, attr, None)
                except Exception:
                    value = None
                if value:
                    return str(value)
        return ""

    def _dedupe_key(self, event: AstrMessageEvent, scope: str, uid: str, song: str) -> str:
        """幂等键：优先用平台消息 id；没有就按 dedupe_seconds 时间窗；都没有就一次性。"""
        message_id = self._message_id(event)
        if message_id:
            return f"mid:{scope}:{uid}:{message_id}"
        window = int(self._settings().dedupe_seconds or 0)
        if window > 0:
            bucket = int(time.time() // max(1, window))
            return f"win:{scope}:{uid}:{song}:{bucket}"
        return f"once:{scope}:{uid}:{uuid.uuid4().hex}"

    def _group_allowed(self, event: AstrMessageEvent, scope: str) -> bool:
        blocked = self._cfg("blocked_groups", []) or []
        if isinstance(blocked, str):
            blocked = [x.strip() for x in blocked.replace("，", ",").split(",") if x.strip()]
        if blocked and any(str(scope) == str(g) for g in blocked):
            return False
        allowed = self._cfg("allowed_groups", []) or []
        if isinstance(allowed, str):
            allowed = [x.strip() for x in allowed.replace("，", ",").split(",") if x.strip()]
        if allowed:
            return any(str(scope) == str(g) for g in allowed)
        return True

    # ------------------------------------------------------------ 播放后端
    def _music_backend(self) -> str:
        return str(self._cfg("music_backend", "llm_tool") or "llm_tool").strip().lower()

    def _find_music_tool(self):
        """在已注册的 LLM 工具里找音乐播放工具（默认 play_song_by_name）。"""
        name = str(self._cfg("music_tool_name", "play_song_by_name") or "").strip()
        if not name:
            return None
        try:
            manager = self.context.get_llm_tool_manager()
            tool = manager.get_func(name)
        except Exception as exc:
            logger.warning("[song_points] 查找音乐工具 %s 失败：%s", name, exc)
            return None
        if tool is None:
            return None
        if getattr(tool, "handler", None) is None:
            return None
        return tool

    @staticmethod
    def _tool_param_names(tool) -> list[str]:
        try:
            params = getattr(tool, "parameters", None) or {}
            props = params.get("properties") or {}
            return [str(k) for k in props]
        except Exception:
            return []

    # ------------------------------------------------------------ 音源预检
    async def _probe_sources(self, tool=None, force: bool = False) -> SourceProbe:
        """探测音乐插件「到底有没有可用音源」。

        做法（零网络、零副作用）：从 LLM 工具反查它所属的插件实例，直接读音源列表。
        依据 astrbot/core/star/star_manager.py:1299：插件工具 handler 是
        functools.partial(raw_handler, star_cls)，args[0] 就是插件实例。
        """
        tool = tool if tool is not None else self._find_music_tool()
        if tool is None:
            return SourceProbe(
                available=None, method="tool-missing", detail="音乐播放工具未注册"
            )
        tool_name = str(getattr(tool, "name", "") or "")
        ttl = max(0, int(self._cfg("health_check_ttl", 300) or 0))
        cached = self._probe
        if (
            cached is not None
            and not force
            and cached.tool_name == tool_name
            and ttl > 0
            and (time.time() - cached.ts) < ttl
        ):
            return cached

        instance = tool_instance(tool)
        if instance is None:
            configured = str(self._cfg("music_plugin_name", "") or "").strip()
            if configured:
                try:
                    meta = self.context.get_registered_star(configured)
                    instance = getattr(meta, "star_cls", None) if meta else None
                except Exception:  # 框架版本差异 / 插件没装
                    instance = None
        probe = await asyncio.to_thread(extract_sources_from_instance, instance)
        probe.tool_name = tool_name
        probe.ts = time.time()
        self._probe = probe
        return probe

    def _no_source_text(self, probe: SourceProbe, event: AstrMessageEvent) -> str:
        template = str(self._cfg("no_source_template", DEFAULT_NO_SOURCE_TEMPLATE))
        text = _fill(template, sources="、".join(probe.sources) or "无", detail=probe.detail)
        if self._is_admin(event):
            text += f"\n\n[管理员诊断] {probe.summary()}｜探测方式：{probe.method or 'unknown'}"
        return text

    @staticmethod
    def _human_reason(why: str, note: str) -> str:
        """把判定结果翻成一句人话（写进回复和订单备注）。"""
        raw = note if note else why
        raw = " ".join(str(raw).split())
        if looks_like_no_source(raw):
            return f"音乐插件没有可用音源，歌没放出来（{raw[:80]}）"
        return raw[:120] or "音乐插件没给出原因"

    def _failure_text(
        self, song: str, outcome: OrderOutcome, reason: str, refunded: bool
    ) -> str:
        if not refunded:
            return (
                f"⚠️ 「{song}」没放出来（{reason}）｜积分不退（配置 refund_on_failure=false）"
            )
        template = str(self._cfg("failure_template", DEFAULT_FAILURE_TEMPLATE))
        return _fill(
            template,
            song=song,
            cost=outcome.cost,
            balance=outcome.balance + outcome.cost,
            reason=reason,
            name=outcome.user_name,
        )

    def _music_kwargs(self, tool, song: str, artist: str = "") -> dict[str, Any]:
        """按目标工具真实声明的参数名喂参，绝不硬塞它不认识的 key。"""
        names = self._tool_param_names(tool)
        kwargs: dict[str, Any] = {}
        for candidate in ("song_name", "song", "keyword", "query", "name"):
            if candidate in names:
                kwargs[candidate] = song
                break
        if artist:
            for candidate in ("artist", "singer"):
                if candidate in names:
                    kwargs[candidate] = artist
                    break
        platform = str(self._cfg("music_default_platform", "") or "").strip()
        if platform and "platform" in names:
            kwargs["platform"] = platform
        send_mode = str(self._cfg("music_send_mode", "") or "").strip()
        if send_mode and "send_mode" in names:
            kwargs["send_mode"] = send_mode
        return kwargs

    async def _play(self, event: AstrMessageEvent, song: str, artist: str = ""):
        """调用音乐工具真播放。返回 (备注文本, 需要转发的 yield 列表)。"""
        backend = self._music_backend()
        if backend in ("none", "off", "disabled", ""):
            return "backend=none", []
        tool = self._find_music_tool()
        if tool is None:
            raise SongPointsError("music tool not found")
        kwargs = self._music_kwargs(tool, song, artist)
        timeout = float(self._cfg("music_timeout", 45) or 45)
        extra: list = []

        async def _run():
            result = tool.handler(event, **kwargs)
            if inspect.isasyncgen(result):
                last = None
                async for item in result:
                    if isinstance(item, str):
                        last = item
                    else:
                        extra.append(item)
                return last
            if inspect.isawaitable(result):
                return await result
            return result

        result = await asyncio.wait_for(_run(), timeout=timeout)
        note = result if isinstance(result, str) else ""
        return note, extra

    # ------------------------------------------------------------ 主流程
    async def _process(self, event: AstrMessageEvent, song: str, artist: str = ""):
        """校验 → 扣分 → 播放 → 回报。返回 (回复文本, 额外 yield 列表)。"""
        if not self._enabled():
            return "点歌功能未开启（配置项 enabled）", []

        scope = self._scope(event)
        if scope is None:
            return "拿不到会话标识，先不点歌了", []
        if scope.startswith("private:") and not self._allow_private():
            return "私聊里暂不支持点歌（配置项 allow_private 可打开）", []
        if not self._group_allowed(event, scope):
            return "本群没有开通点歌（配置项 blocked_groups / allowed_groups）", []

        store = self.store
        if not await asyncio.to_thread(store.ledger_ready):
            return (
                "积分体系没就位：没找到签到插件的数据库。\n"
                "请先装好并启用 astrbot_plugin_checkin，再发 /签到 攒分。",
                [],
            )

        backend = self._music_backend()
        if backend not in ("none", "off", "disabled", "") and self._find_music_tool() is None:
            return (
                "找不到音乐播放工具（配置项 music_tool_name，默认 play_song_by_name），"
                "这次不扣分。请确认音乐插件已启用，或把 music_backend 设为 none。",
                [],
            )

        # 音源可用性预检：没有音源就别让普通用户当冤大头（一分不扣）
        if backend not in ("none", "off", "disabled", "") and bool(
            self._cfg("health_check_enabled", True)
        ):
            probe = await self._probe_sources()
            if probe.available is False:
                logger.warning(
                    "[song_points] 音源预检未通过（%s），本次不扣分：%s",
                    probe.method,
                    probe.summary(),
                )
                return self._no_source_text(probe, event), []

        uid, name = self._sender_meta(event)
        dedupe = self._dedupe_key(event, scope, uid, song)
        platform = str(self._cfg("music_default_platform", "") or "").strip()

        outcome: OrderOutcome = await asyncio.to_thread(
            store.request_order,
            scope,
            uid,
            name,
            song,
            dedupe,
            artist,
            platform,
            self._is_admin(event),
        )

        if outcome.status == OUTCOME_NO_LEDGER:
            return ("签到积分库没建好，先发一次 /签到 再来点歌。", [])
        if outcome.status == OUTCOME_DUPLICATE:
            replay = {
                "played": "已经点过啦",
                "pending": "上一笔还在处理",
                "refunded": "这单已退分",
            }.get(outcome.existing_status, "已经点过啦")
            return (
                f"🔁 「{song}」这条消息已经处理过（{replay}），不重复扣分｜余额 {outcome.balance} 分",
                [],
            )
        if outcome.status == OUTCOME_INSUFFICIENT:
            return (
                format_insufficient(
                    outcome,
                    str(self._cfg("not_enough_template", DEFAULT_INSUFFICIENT_TEMPLATE)),
                    name,
                ),
                [],
            )

        # 已扣分，开始真实播放
        note = ""
        extra: list = []
        try:
            note, extra = await self._play(event, song, artist)
        except asyncio.TimeoutError:
            self._last_failure = f"{song}：播放超时"
            await asyncio.to_thread(store.refund, outcome.order_id, "playback timeout")
            return (
                f"⏱ 播放超时，已把 {outcome.cost} 分退回｜余额 {outcome.balance + outcome.cost} 分",
                [],
            )
        except SongPointsError as exc:
            self._last_failure = f"{song}：{exc}"
            await asyncio.to_thread(store.refund, outcome.order_id, f"playback failed: {exc}")
            return (
                f"⚠️ 播放失败（{exc}），已把 {outcome.cost} 分退回｜余额 {outcome.balance + outcome.cost} 分",
                [],
            )
        except Exception as exc:  # noqa: BLE001 - 第三方音乐插件什么都能抛
            logger.error("[song_points] 播放异常：%s", exc)
            self._last_failure = f"{song}：{exc}"
            if bool(self._cfg("refund_on_failure", True)):
                await asyncio.to_thread(store.refund, outcome.order_id, f"playback error: {exc}")
                return (
                    f"⚠️ 播放出错了，已把 {outcome.cost} 分退回｜余额 "
                    f"{outcome.balance + outcome.cost} 分｜详情看后台日志",
                    [],
                )
            return (f"⚠️ 播放出错了（{exc}），积分不退（配置 refund_on_failure=false）", [])

        # 关键：音乐工具失败也是 return 字符串（不抛异常），所以这里必须自己判成败。
        # 依据 music_health.py 头部注释（astrbot_plugin_music v3.2.6 真实返回文本）。
        verdict, why = classify_play_result(
            note,
            len(extra),
            empty_action=str(self._cfg("empty_result_action", "ok")),
            unknown_action=str(self._cfg("unknown_result_action", "ok")),
        )
        if verdict != "ok":
            reason = self._human_reason(why, note)
            self._last_failure = f"{song}：{reason}"
            logger.warning("[song_points] 播放判失败（%s）：%s", song, reason)
            if bool(self._cfg("refund_on_failure", True)):
                await asyncio.to_thread(store.refund, outcome.order_id, f"playback failed: {reason}")
                return self._failure_text(song, outcome, reason, True), []
            return self._failure_text(song, outcome, reason, False), []

        self._last_failure = ""
        await asyncio.to_thread(store.mark_played, outcome.order_id, note or why)

        template = DEFAULT_SUCCESS_TEMPLATE
        if outcome.status == OUTCOME_FREE:
            template = str(self._cfg("free_template", DEFAULT_FREE_TEMPLATE))
        else:
            template = str(self._cfg("success_template", DEFAULT_SUCCESS_TEMPLATE))
        return format_success(outcome, template, name), extra

    async def _handle(self, event: AstrMessageEvent, song: str, artist: str = ""):
        """统一出口：把 _process 的文本包成 plain_result，并转发音乐插件 yield 的消息段。"""
        try:
            text, extra = await self._process(event, song, artist)
        except InvalidArgument as exc:
            yield event.plain_result(f"⚠️ {exc}\n用法：/赏歌 <歌名>，例如 /赏歌 晴天")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("[song_points] 未预期异常：%s", exc)
            yield event.plain_result("点歌出错了，已记录日志。稍后再试，或发 /点歌账单 看有没有扣分。")
            return
        yield event.plain_result(text)
        for item in extra:
            try:
                yield item
            except Exception:
                continue

    # ------------------------------------------------------------ 指令
    @filter.command(SING_COMMAND, alias=set(SING_ALIASES))
    async def cmd_song(self, event: AstrMessageEvent):
        """赏歌（消耗签到积分）：/赏歌 歌名"""
        if not self._enabled():
            yield event.plain_result("点歌功能未开启（配置项 enabled）")
            return
        text = self._text(event)
        raw = strip_command_prefix(text, SING_TRIGGERS)
        artist = ""
        raw, artist = self._split_artist(raw)
        try:
            song = normalize_song(raw)
        except InvalidArgument:
            yield event.plain_result(HELP_TEXT)
            return
        async for item in self._handle(event, song, artist):
            yield item

    @staticmethod
    def _split_artist(raw: str) -> tuple[str, str]:
        """支持 `/赏歌 晴天 | 周杰伦` 与 `/赏歌 晴天 - 周杰伦` 两种写法。"""
        text = (raw or "").strip()
        for sep in ("|", "｜", " - ", " -", "- "):
            if sep in text:
                left, right = text.split(sep, 1)
                left, right = left.strip(), right.strip()
                if left and right:
                    return left, right
        return text, ""

    @filter.command("点歌账单", alias={"点歌记录", "song账单", "点歌明细"})
    async def cmd_song_history(self, event: AstrMessageEvent):
        """查自己的点歌记录与余额"""
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result("拿不到会话标识，查不了账单")
            return
        if not await asyncio.to_thread(self.store.ledger_ready):
            yield event.plain_result("还没找到签到积分库，先发 /签到 吧。")
            return
        uid, _ = self._sender_meta(event)
        settings = self._settings()
        balance, rows = await asyncio.gather(
            asyncio.to_thread(self.store.balance, scope, uid),
            asyncio.to_thread(self.store.history, scope, uid, settings.history_size),
        )
        yield event.plain_result(format_history(rows, balance, settings.tz))

    @filter.command("点歌榜", alias={"点歌排行", "消费榜", "song榜"})
    async def cmd_song_rank(self, event: AstrMessageEvent):
        """点歌消费榜：/点歌榜 今日"""
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result("拿不到会话标识，看不了榜单")
            return
        if not await asyncio.to_thread(self.store.ledger_ready):
            yield event.plain_result("还没找到签到积分库，先发 /签到 吧。")
            return
        raw = strip_command_prefix(self._text(event), ("点歌榜", "点歌排行", "消费榜", "song榜"))
        settings = self._settings()
        since = None
        title = "点歌消费榜"
        if raw.strip().lower() in ("today", "今日", "今天", "日榜"):
            since = day_start_ts(settings.tz)
            title = "点歌消费榜（今日）"
        rows = await asyncio.to_thread(
            self.store.spend_ranking, scope, settings.leaderboard_size, since,
        )
        label = "本群" if not scope.startswith("private:") else "本会话"
        yield event.plain_result(format_ranking(rows, label, title))

    @filter.command("赏歌诊断", alias={"音源诊断", "赏歌状态"})
    async def cmd_song_diagnose(self, event: AstrMessageEvent):
        """音源诊断（管理员）：看音乐工具是否注册、有没有可用音源"""
        if not self._is_admin(event) and not bool(self._cfg("diagnose_for_all", False)):
            yield event.plain_result(
                "这个指令给管理员用｜普通点歌直接发 /赏歌 <歌名> 就行（没音源时不会扣分）"
            )
            return
        backend = self._music_backend()
        tool_name = str(self._cfg("music_tool_name", "play_song_by_name") or "")
        lines = [f"🔎 赏歌诊断 {PLUGIN_VERSION}", f"· 播放后端：{backend}｜工具名：{tool_name}"]
        tool = self._find_music_tool()
        if backend in ("none", "off", "disabled", ""):
            lines.append("· 音乐工具：后端为 none，不调用音乐插件（只记账）")
        elif tool is None:
            lines.append("· 音乐工具：未注册（点歌一律不扣分）→ 检查音乐插件是否启用")
        else:
            params = "、".join(self._tool_param_names(tool)) or "无参数"
            lines.append(f"· 音乐工具：已注册｜声明参数：{params}")

        probe = await self._probe_sources(tool, force=True)
        age = int(max(0, time.time() - probe.ts)) if probe.ts else 0
        lines.append(f"· 音源预检：{probe.summary()}")
        lines.append(f"· 探测方式：{probe.method or 'unknown'}｜{age} 秒前｜缓存 {self._cfg('health_check_ttl', 300)} 秒")
        extra_sources = probe.sources or parse_sources_from_text(self._last_failure)
        if extra_sources:
            lines.append(f"· 音源清单：{'、'.join(extra_sources)}")
        if probe.available is False:
            lines.append("· 现在普通用户点歌一律不扣分（直到音源恢复）→ 去音乐插件里启用音源")
        lines.append(f"· 最近一次失败：{self._last_failure or '无'}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ @机器人 触发
    def _at_me(self, event: AstrMessageEvent) -> bool:
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            return False
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if not self_id:
            return False
        for seg in chain:
            kind = str(getattr(seg, "type", "") or type(seg).__name__)
            if kind.endswith("At") or kind == "At":
                qq = str(getattr(seg, "qq", "") or "")
                if qq and qq == self_id:
                    return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_at_mention(self, event: AstrMessageEvent):
        """@机器人 + 歌名 也能赏歌（配置 at_mention_enabled 打开后生效）。"""
        if not self._enabled() or not self._at_mention_enabled():
            return
        text = self._text(event)
        if not text or text.startswith(("/", "／", "!")):
            return
        if not self._at_me(event):
            return
        # 音乐插件自己的触发词：整句让给它，否则「@bot 点歌 晴天」会被两边各播一次
        if _starts_with_any(text, AT_MENTION_SKIP_PREFIXES):
            return
        raw = strip_command_prefix(text, SING_TRIGGERS)
        if not raw.strip():
            yield event.plain_result(HELP_TEXT)
            return
        try:
            song = normalize_song(raw)
        except InvalidArgument:
            return
        async for item in self._handle(event, song):
            yield item
        event.stop_event()

    # ------------------------------------------------------------ LLM 工具
    @filter.llm_tool(name="song_with_points")
    async def llm_song_with_points(self, event: AstrMessageEvent, song_name: str, artist: str = "") -> str:
        """替用户点歌并扣签到积分（会真的播放音乐）。用户说想听某首歌时调用。

        Args:
            song_name(string): 歌名，可包含歌手
            artist(string): 可选，指定歌手
        """
        try:
            song = normalize_song(song_name)
        except InvalidArgument:
            yield "歌名不合法（空或超过 80 字），请让用户重新说清歌名。"
            return
        text, extra = await self._process(event, song, (artist or "").strip())
        for item in extra:
            try:
                yield item
            except Exception:
                continue
        yield text

    @filter.llm_tool(name="song_points_balance")
    async def llm_song_points_balance(self, event: AstrMessageEvent) -> str:
        """查用户当前点歌可用积分（签到积分余额）以及今日剩余免费点歌次数。"""
        scope = self._scope(event)
        if scope is None:
            return "拿不到会话标识，查不了余额。"
        if not await asyncio.to_thread(self.store.ledger_ready):
            return "还没找到签到积分库，需要先装并启用 astrbot_plugin_checkin。"
        uid, _ = self._sender_meta(event)
        balance, free_left = await asyncio.gather(
            asyncio.to_thread(self.store.balance, scope, uid),
            asyncio.to_thread(self.store.free_left, scope, uid),
        )
        return f"当前可用积分 {balance} 分，今日免费点歌剩余 {free_left} 次。"

    # ------------------------------------------------------------ 生命周期
    async def initialize(self):
        """启动时：清理悬空 pending 订单 → 探测音源是否可用。"""
        try:
            seconds = int(self._cfg("stale_pending_seconds", 600) or 0)
            if seconds > 0:
                fixed = await asyncio.to_thread(self.store.sweep_stale_pending, seconds)
                if fixed:
                    logger.info("[song_points] 启动清扫：退回 %d 笔悬空扣分", fixed)
        except Exception as exc:  # 清扫失败不影响插件加载
            logger.warning("[song_points] 启动清扫失败：%s", exc)

        if not bool(self._cfg("health_check_on_startup", True)):
            return
        try:
            probe = await self._probe_sources(force=True)
        except Exception as exc:  # 探测失败也不影响加载
            logger.warning("[song_points] 音源预检异常：%s", exc)
            return
        if probe.available is False:
            logger.warning(
                "[song_points] 音源预检未通过（%s）：现在点歌不会扣分，"
                "请到音乐插件配置里启用至少一个音源",
                probe.method,
            )
        elif probe.available is True:
            logger.info("[song_points] 音源预检通过：%s", probe.summary())
        else:
            logger.info("[song_points] 音源状态未知：%s（不阻断点歌，失败会自动退分）", probe.summary())

    async def terminate(self):
        self._store = None