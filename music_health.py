"""音源健康探测 + 播放结果判定（纯标准库，可脱离 AstrBot 单独跑）。

依据（2026-11 实读 astrbot_plugin_music（Zhalslar）v3.2.6 源码，别凭记忆改）：

    @filter.llm_tool()
    async def play_song_by_name(self, event, song_name, platform="", artist="", send_mode=""):
        ...
        if send_modes is None:           return "未知发送方式，请使用 voice、record_link、..."
        if not selected_player:          return f"音源「{p}」不可用；指定音源时不会自动回退"
        if not selected_song:            return f"未找到与《{song}》…可靠匹配的歌曲；已尝试音源：{attempted}"
        if not sent:                     return f"音源「{platform_name}」发送《{song.name}》失败"
        if return_play_song_result and not comment_sent:
            return f"已通过音源「{platform_name}」发送《{song.name} - {author}》"
        # 否则隐式 return None（发送成功但没回话）

结论：
1. 这个工具**永远不抛异常**，失败也是 return 字符串 → 「没抛异常」绝不能当作播放成功，
   必须扫失败特征串。这就是「扣了分没歌」的真因。
2. 成功有两种形态：带「已通过音源/正在播放」的字符串，或 None/空串
   （send_config.return_play_song_result=false 时，发送成功就返回 None）。
3. 音源是否启用可以从插件实例直接问，零网络、零副作用：
   get_players_by_priority() / get_player(default=True) / players / cfg.players.enabled_platforms()
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 明确的成功特征（出现即判成功；比失败特征优先，避免歌名里带「失败」两个字被误判）
STRONG_SUCCESS_MARKERS = (
    "正在播放",
    "已通过音源",
    "已发送",
    "播放成功",
    "开始播放",
    "已开始播放",
    "playing",
)

# 失败特征：全部来自真实插件 / 常见音乐插件的 return 文本
FAILURE_MARKERS = (
    "无可用音源",
    "无可用播放器",
    "没有可用音源",
    "不可用",
    "无法播放",
    "未找到",
    "没找到",
    "未搜到",
    "没有找到",
    "失败",
    "出错",
    "错误",
    "超时",
    "不支持",
    "未知发送方式",
    "无结果",
    "not found",
    "failed",
    "error",
    "no available",
)

# 「没有音源」的强特征：命中就不能让用户扣分
NO_SOURCE_MARKERS = (
    "无可用音源",
    "无可用播放器",
    "没有可用音源",
    "音源不可用",
    "未配置音源",
    "no available source",
)


def normalize_text(text: Any) -> str:
    """工具返回值可能是 None / str / 其它对象，统一成去空白的字符串。"""
    if text is None:
        return ""
    if isinstance(text, str):
        return text.strip()
    return str(text).strip()


def looks_like_no_source(text: Any) -> bool:
    """文本是否明确表达「没有可用音源」。"""
    body = normalize_text(text).lower()
    if not body:
        return False
    return any(marker.lower() in body for marker in NO_SOURCE_MARKERS)


def parse_sources_from_text(text: Any) -> list[str]:
    """从「已尝试音源：A、B」里抠出音源名；抠不到返回空列表。"""
    body = normalize_text(text)
    if not body:
        return []
    for marker in ("已尝试音源：", "已尝试音源:", "已尝试：", "音源列表："):
        if marker in body:
            tail = body.split(marker, 1)[1]
            for stop in ("\n", "；", ";", "。"):
                if stop in tail:
                    tail = tail.split(stop, 1)[0]
            items = [x.strip() for x in tail.replace("，", "、").split("、")]
            return [x for x in items if x]
    return []


def classify_play_result(
    text: Any,
    extra_count: int = 0,
    empty_action: str = "ok",
    unknown_action: str = "ok",
) -> tuple[str, str]:
    """判定这一次播放到底成不成。

    返回 (verdict, note)：
      verdict = "ok"      → 可以记成已播放、保留扣分
      verdict = "failed"  → 必须退分
      note    = 可读原因，写进订单备注 / 诊断指令

    extra_count：工具 yield 出来的消息段数量（音频卡片、语音等实体产物）。
    有实体产物就按成功算（播放内容已经发到会话里了）。
    """
    if extra_count and extra_count > 0:
        return "ok", "media delivered"

    body = normalize_text(text)
    lowered = body.lower()

    if body:
        for marker in STRONG_SUCCESS_MARKERS:
            if marker.lower() in lowered:
                return "ok", body[:200]

    if body:
        for marker in FAILURE_MARKERS:
            if marker.lower() in lowered:
                return "failed", f"{marker}|{body[:160]}"

    empty_action = (empty_action or "ok").strip().lower()
    unknown_action = (unknown_action or "ok").strip().lower()

    if not body:
        if empty_action in ("refund", "failed", "fail"):
            return "failed", "empty result (empty_result_action=refund)"
        return "ok", "empty result (treated as ok)"

    if unknown_action in ("refund", "failed", "fail"):
        return "failed", f"unknown result (unknown_result_action=refund)|{body[:160]}"
    return "ok", f"unknown result (treated as ok)|{body[:160]}"


# ------------------------------------------------------------------ 音源探测


@dataclass
class SourceProbe:
    """一次音源探测的结论。available: True=有音源 / False=明确没有 / None=探测不出来。"""

    available: Optional[bool] = None
    sources: list[str] = field(default_factory=list)
    method: str = ""
    detail: str = ""
    ts: float = 0.0
    tool_name: str = ""

    def summary(self) -> str:
        """给人看的一句话结论。"""
        if self.available is True:
            listing = "、".join(self.sources) if self.sources else "（音源名未暴露）"
            return f"音源可用：{listing}"
        if self.available is False:
            return "音源不可用：音乐插件里没有任何已启用音源"
        return f"音源状态未知（{self.detail or self.method or '探测不到'}）"


def _player_labels(players: Iterable[Any]) -> list[str]:
    labels: list[str] = []
    for player in players or []:
        if isinstance(player, str):
            labels.append(player)
            continue
        platform = getattr(player, "platform", None)
        display = getattr(platform, "display_name", None) or getattr(platform, "name", None)
        name = getattr(platform, "name", None)
        if display and name and str(display) != str(name):
            labels.append(f"{display}({name})")
        elif display or name:
            labels.append(str(display or name))
    return labels


def _call_quietly(func, *args, **kwargs):
    """调插件实例的方法；抛异常、返回协程都算「拿不到」，返回 (ok, value)。"""
    try:
        value = func(*args, **kwargs)
    except Exception as exc:  # 第三方插件什么都能抛
        return False, exc
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):  # 避免 coroutine never awaited 警告
            try:
                close()
            except Exception:
                pass
        return False, "async"
    return True, value


def extract_sources_from_instance(instance: Any) -> SourceProbe:
    """从音乐插件实例上直接读「有没有已启用音源」，零网络、零副作用。

    顺序照真实插件实现来（astrbot_plugin_music v3.2.6 三个都有）：
      get_players_by_priority() -> list[BaseMusicPlayer]    ← 最准，按优先级排好
      get_player(default=True)  -> BaseMusicPlayer | None
      players                   -> list[BaseMusicPlayer]
      cfg.players.enabled_platforms() -> list[str]
    """
    if instance is None:
        return SourceProbe(available=None, method="no-instance", detail="拿不到音乐插件实例")

    getter = getattr(instance, "get_players_by_priority", None)
    if callable(getter):
        ok, value = _call_quietly(getter)
        if ok and isinstance(value, (list, tuple)):
            labels = _player_labels(value)
            return SourceProbe(
                available=bool(value),
                sources=labels,
                method="get_players_by_priority",
                detail=f"{len(value)} 个音源",
            )

    getter = getattr(instance, "get_player", None)
    if callable(getter):
        ok, value = _call_quietly(getter, default=True)
        if not ok:
            ok, value = _call_quietly(getter, None, None, True)
        if ok and value is not None:
            labels = _player_labels([value])
            return SourceProbe(
                available=True, sources=labels, method="get_player", detail="1 个默认音源"
            )

    players = getattr(instance, "players", None)
    if isinstance(players, (list, tuple)):
        return SourceProbe(
            available=bool(players),
            sources=_player_labels(players),
            method="players",
            detail=f"{len(players)} 个音源",
        )

    cfg = getattr(instance, "cfg", None) or getattr(instance, "config", None)
    players_cfg = getattr(cfg, "players", None)
    enabled = getattr(players_cfg, "enabled_platforms", None)
    if callable(enabled):
        ok, value = _call_quietly(enabled)
        if ok and isinstance(value, (list, tuple)):
            return SourceProbe(
                available=bool(value),
                sources=[str(x) for x in value],
                method="cfg.players.enabled_platforms",
                detail=f"{len(value)} 个已启用音源",
            )

    return SourceProbe(available=None, method="unknown-instance", detail="该音乐插件不暴露音源列表")


def tool_instance(tool: Any) -> Any:
    """从已注册的 LLM 工具反查它所属的插件实例。

    依据 astrbot/core/star/star_manager.py:1299 —— 插件工具的 handler 会被包成
    functools.partial(raw_handler, metadata.star_cls)，所以 args[0] 就是插件实例。
    """
    handler = getattr(tool, "handler", None)
    if handler is None:
        return None
    args = getattr(handler, "args", None)
    if args:
        return args[0]
    func = getattr(handler, "func", None)
    return getattr(func, "__self__", None)