"""点歌消费积分的纯逻辑核心（不依赖 AstrBot，可脱离框架单独跑测试）。

设计要点（每条都对应需求里的一条）：
1. 不另起货币：**直接读写签到插件 astrbot_plugin_checkin 的 checkin.db**，
   余额字段就是 `checkin_users.total_points`，扣分与签到加分落在同一个数上。
2. 原子 + 幂等：
   - 所有写操作放在**单条 `BEGIN IMMEDIATE` 事务**里（同库同连接，天然原子）；
   - `song_order_log.dedupe_key` 有 UNIQUE 约束，同一条消息重复点歌只会落一条流水，
     第二次进来直接命中 duplicate，不扣分；
   - 扣分用 `UPDATE ... WHERE total_points >= ?` 带条件更新，永远扣不成负数。
3. 会话隔离：所有查询/扣分都以 `scope`（群号，私聊为 `private:<uid>`）为分区键，
   与签到插件的 `(group_id, user_id)` 口径完全一致，不跨群串分。
4. 流水与榜单：`song_order_log` 记每笔点歌（pending/played/refunded），
   `song_free_usage` 记每日免费次数，消费榜 = played 流水的 cost 求和。

为什么把我们的表建在 checkin.db 里而不是自己的库：
SQLite 的跨库（ATTACH）事务在 WAL 模式下**不保证原子**，而签到插件把 checkin.db
设成了 WAL。只有同库同事务才能保证「扣分」与「记流水」要么都成、要么都不成。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

CHECKIN_PLUGIN_NAME = "astrbot_plugin_checkin"
CHECKIN_DB_NAME = "checkin.db"
DEFAULT_TIMEZONE = "Asia/Shanghai"

STATUS_PENDING = "pending"
STATUS_PLAYED = "played"
STATUS_REFUNDED = "refunded"

OUTCOME_OK = "ok"
OUTCOME_FREE = "free"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_INSUFFICIENT = "insufficient"
OUTCOME_NO_LEDGER = "no_ledger"

_SONG_SCHEMA = """
CREATE TABLE IF NOT EXISTS song_order_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    user_name   TEXT    NOT NULL DEFAULT '',
    song        TEXT    NOT NULL DEFAULT '',
    artist      TEXT    NOT NULL DEFAULT '',
    platform    TEXT    NOT NULL DEFAULT '',
    cost        INTEGER NOT NULL DEFAULT 0,
    is_free     INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    dedupe_key  TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    played_ts   INTEGER,
    UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_song_order_scope_ts
    ON song_order_log (scope, ts DESC);
CREATE INDEX IF NOT EXISTS idx_song_order_scope_user_ts
    ON song_order_log (scope, user_id, ts DESC);

CREATE TABLE IF NOT EXISTS song_free_usage (
    scope   TEXT    NOT NULL,
    user_id TEXT    NOT NULL,
    day     TEXT    NOT NULL,
    used    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, user_id, day)
);
"""


class SongPointsError(Exception):
    """业务异常基类。"""


class InvalidArgument(SongPointsError):
    """入参非法（指令/LLM 工具入参校验用）。"""


# ------------------------------------------------------------------ 工具函数


def _clamp_int(value: Any, default: int, lo: int = 0, hi: int = 10**7) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "n", "否", "关", ""):
        return False
    return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def get_tz(name: Optional[str]):
    """解析时区；非法时回退 UTC+8，绝不让插件启动失败。"""
    raw = (name or DEFAULT_TIMEZONE).strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass
    if raw in ("Asia/Shanghai", "PRC", "UTC+8", "GMT+8", ""):
        return timezone(timedelta(hours=8))
    return timezone.utc


def day_str(ts: Optional[int] = None, tz=None) -> str:
    """把时间戳换成配置时区下的 YYYY-MM-DD。"""
    zone = tz or get_tz(DEFAULT_TIMEZONE)
    moment = datetime.fromtimestamp(int(ts if ts is not None else time.time()), zone)
    return moment.strftime("%Y-%m-%d")


def day_start_ts(tz=None) -> int:
    """配置时区下「今天 00:00」的时间戳，用于今日消费统计。"""
    zone = tz or get_tz(DEFAULT_TIMEZONE)
    now = datetime.now(zone)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def normalize_song(text: str) -> str:
    """规范化歌名：去首尾空白、去掉包住整句的引号/书名号、压掉多余空白。"""
    song = (text or "").strip()
    song = song.strip("　 \t\r\n")
    for left, right in (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"),
                        ("《", "》"), ("<", ">"), ("【", "】"), ("[", "]")):
        if len(song) > 1 and song.startswith(left) and song.endswith(right):
            inner = song[1:-1].strip()
            if inner:
                song = inner
            break
    song = " ".join(song.split())
    if not song:
        raise InvalidArgument("歌名不能为空")
    if len(song) > 80:
        raise InvalidArgument("歌名太长了（上限 80 字）")
    return song


def strip_command_prefix(text: str, commands: Iterable[str]) -> str:
    """剥掉开头的 `/`、`!`、`／` 和指令名，返回剩余文本（歌名原文）。"""
    raw = (text or "").strip()
    raw = raw.lstrip("/／!！").strip()
    for name in sorted(commands, key=len, reverse=True):
        if not name:
            continue
        if raw.lower().startswith(name.lower()):
            return raw[len(name):].strip()
    return raw


class _SafeDict(dict):
    def __missing__(self, key):  # 未知占位符原样保留，不让回复文本崩
        return "{" + str(key) + "}"


def render(template: str, **fields: Any) -> str:
    """按模板渲染回复文案，未知占位符原样保留。"""
    try:
        return (template or "").format_map(_SafeDict(fields))
    except Exception:
        return template or ""


# ------------------------------------------------------------------ 配置


@dataclass
class Settings:
    cost_per_song: int = 10
    free_daily_quota: int = 0
    admin_free: bool = False
    refund_on_failure: bool = True
    dedupe_seconds: int = 0
    leaderboard_size: int = 10
    history_size: int = 5
    timezone: str = DEFAULT_TIMEZONE
    checkin_plugin_name: str = CHECKIN_PLUGIN_NAME
    db_path_override: str = ""

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]]) -> "Settings":
        data: Mapping[str, Any] = cfg or {}
        return cls(
            cost_per_song=_clamp_int(data.get("cost_per_song", 10), 10, 0, 10**6),
            free_daily_quota=_clamp_int(data.get("free_daily_quota", 0), 0, 0, 100),
            admin_free=_as_bool(data.get("admin_free"), False),
            refund_on_failure=_as_bool(data.get("refund_on_failure"), True),
            dedupe_seconds=_clamp_int(data.get("dedupe_seconds", 0), 0, 0, 3600),
            leaderboard_size=_clamp_int(data.get("leaderboard_size", 10), 10, 1, 50),
            history_size=_clamp_int(data.get("history_size", 5), 5, 1, 50),
            timezone=_as_str(data.get("timezone"), DEFAULT_TIMEZONE),
            checkin_plugin_name=_as_str(data.get("checkin_plugin_name"), CHECKIN_PLUGIN_NAME),
            db_path_override=_as_str(data.get("db_path_override"), ""),
        )

    @property
    def tz(self):
        return get_tz(self.timezone)


# ------------------------------------------------------------------ 路径定位


def resolve_data_dir(plugin_name: str = CHECKIN_PLUGIN_NAME) -> Path:
    """定位 AstrBot 的 data/plugin_data/<签到插件> 目录。

    与签到插件 main.py 的 `_resolve_data_dir` 完全同源，保证指向同一个库。
    """
    env_override = os.environ.get("SONG_POINTS_DATA_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    candidates: list[Path] = []
    try:  # AstrBot 官方工具函数
        from astrbot.core.utils import astrbot_path  # type: ignore

        for fn_name in ("get_astrbot_data_path", "get_astrbot_root", "get_astrbot_path"):
            fn = getattr(astrbot_path, fn_name, None)
            if callable(fn):
                try:
                    candidates.append(Path(fn()))
                except Exception:
                    continue
    except Exception:
        pass

    for env_key in ("ASTRBOT_ROOT", "ASTRBOT_DATA_DIR", "ASTRBOT_HOME"):
        raw = os.environ.get(env_key)
        if raw:
            candidates.append(Path(raw))
    candidates.append(Path.cwd() / "data")

    for base in candidates:
        try:
            base = base.expanduser()
            if not base.is_absolute():
                base = (Path.cwd() / base).resolve()
            target = base / "plugin_data" / plugin_name
            target.mkdir(parents=True, exist_ok=True)
            return target
        except Exception:
            continue
    fallback = Path.cwd() / "data" / "plugin_data" / plugin_name
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_db_path(settings: Settings) -> Path:
    if settings.db_path_override:
        return Path(settings.db_path_override).expanduser()
    return resolve_data_dir(settings.checkin_plugin_name) / CHECKIN_DB_NAME


# ------------------------------------------------------------------ 结果对象


@dataclass
class OrderOutcome:
    status: str
    order_id: int = 0
    cost: int = 0
    balance: int = 0
    is_free: bool = False
    free_left: int = 0
    song: str = ""
    user_name: str = ""
    existing_status: str = ""
    needed: int = 0


@dataclass
class OrderRow:
    id: int
    song: str
    cost: int
    is_free: bool
    status: str
    ts: int
    note: str = ""


@dataclass
class RankRow:
    user_id: str
    user_name: str
    spent: int
    orders: int
    rank: int = 0


@dataclass
class SpendStats:
    total_orders: int = 0
    total_spent: int = 0
    today_orders: int = 0
    today_spent: int = 0
    members: list = field(default_factory=list)


# ------------------------------------------------------------------ 存储层


class SongPointsStore:
    """SQLite 存储（同步实现，调用方用 asyncio.to_thread 包一层）。"""

    def __init__(self, db_path, settings: Settings) -> None:
        self.db_path = Path(db_path)
        self.settings = settings

    # -------------------------------------------------------- 连接/建表
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.isolation_level = None  # 事务自己用 BEGIN/COMMIT 显式控制
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """只建自己的两张表；签到插件的表由签到插件负责，这里绝不去动它。"""
        conn = self._connect()
        try:
            conn.executescript(_SONG_SCHEMA)
        finally:
            conn.close()

    def ledger_ready(self) -> bool:
        """签到插件的 checkin_users 是否存在（= 积分体系是否已就位）。"""
        if not self.db_path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkin_users'",
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # -------------------------------------------------------- 余额
    def balance(self, scope: str, user_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT total_points FROM checkin_users WHERE group_id = ? AND user_id = ?",
                (str(scope), str(user_id)),
            ).fetchone()
            return int(row["total_points"]) if row else 0
        finally:
            conn.close()

    def free_left(self, scope: str, user_id: str, day: Optional[str] = None) -> int:
        quota = self.settings.free_daily_quota
        if quota <= 0:
            return 0
        today = day or day_str(tz=self.settings.tz)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT used FROM song_free_usage WHERE scope=? AND user_id=? AND day=?",
                (str(scope), str(user_id), today),
            ).fetchone()
            used = int(row["used"]) if row else 0
        finally:
            conn.close()
        return max(0, quota - used)

    # -------------------------------------------------------- 下单（核心）
    def request_order(
        self,
        scope: str,
        user_id: str,
        user_name: str,
        song: str,
        dedupe_key: str,
        artist: str = "",
        platform: str = "",
        admin: bool = False,
        now_ts: Optional[int] = None,
    ) -> OrderOutcome:
        """一次性完成：幂等判重 → 算免费次数 → 扣分 → 落流水（同一个事务）。"""
        scope = str(scope)
        user_id = str(user_id)
        song = (song or "").strip()
        ts = int(now_ts if now_ts is not None else time.time())
        cost = 0 if admin and self.settings.admin_free else int(self.settings.cost_per_song)
        today = day_str(ts, self.settings.tz)
        quota = int(self.settings.free_daily_quota)

        with self._tx() as conn:
            ready = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkin_users'",
            ).fetchone()
            if ready is None:
                return OrderOutcome(
                    status=OUTCOME_NO_LEDGER,
                    balance=0,
                    song=song,
                    user_name=user_name,
                    needed=cost,
                )

            # 1) 幂等判重：同一条消息只认第一笔
            dup = conn.execute(
                "SELECT id, status, cost FROM song_order_log WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            row = conn.execute(
                "SELECT total_points FROM checkin_users WHERE group_id=? AND user_id=?",
                (scope, user_id),
            ).fetchone()
            balance = int(row["total_points"]) if row else 0
            if dup is not None:
                return OrderOutcome(
                    status=OUTCOME_DUPLICATE,
                    order_id=int(dup["id"]),
                    cost=int(dup["cost"]),
                    balance=balance,
                    song=song,
                    user_name=user_name,
                    existing_status=str(dup["status"]),
                    needed=cost,
                )

            # 2) 免费次数
            used = 0
            if quota > 0:
                free_row = conn.execute(
                    "SELECT used FROM song_free_usage WHERE scope=? AND user_id=? AND day=?",
                    (scope, user_id, today),
                ).fetchone()
                used = int(free_row["used"]) if free_row else 0
            is_free = quota > 0 and used < quota
            if is_free:
                cost = 0

            # 3) 余额校验
            if not is_free and balance < cost:
                return OrderOutcome(
                    status=OUTCOME_INSUFFICIENT,
                    balance=balance,
                    song=song,
                    user_name=user_name,
                    needed=cost,
                )

            # 4) 先落流水（UNIQUE 兜底防并发重复），再扣分
            cur = conn.execute(
                "INSERT OR IGNORE INTO song_order_log"
                " (scope, user_id, user_name, song, artist, platform, cost, is_free,"
                "  status, dedupe_key, note, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope, user_id, user_name, song, artist, platform, cost,
                 int(is_free), STATUS_PENDING, dedupe_key, "", ts),
            )
            if cur.rowcount != 1:  # 并发下被别的请求抢先插入同一条 dedupe_key
                return OrderOutcome(
                    status=OUTCOME_DUPLICATE,
                    balance=balance,
                    song=song,
                    user_name=user_name,
                    needed=cost,
                )
            order_id = int(cur.lastrowid or 0)

            if is_free:
                conn.execute(
                    "INSERT INTO song_free_usage (scope, user_id, day, used) VALUES (?,?,?,1)"
                    " ON CONFLICT(scope, user_id, day) DO UPDATE SET used = used + 1",
                    (scope, user_id, today),
                )
            elif cost > 0:
                cur2 = conn.execute(
                    "UPDATE checkin_users SET total_points = total_points - ?, updated_at = ?"
                    " WHERE group_id = ? AND user_id = ? AND total_points >= ?",
                    (cost, ts, scope, user_id, cost),
                )
                if cur2.rowcount != 1:  # 理论上到不了，防御并发/脏数据
                    raise SongPointsError("balance changed during transaction")
                balance -= cost

            free_left = 0
            if quota > 0:
                free_left = max(0, quota - (used + (1 if is_free else 0)))

            return OrderOutcome(
                status=OUTCOME_FREE if is_free else OUTCOME_OK,
                order_id=order_id,
                cost=cost,
                balance=balance,
                is_free=is_free,
                free_left=free_left,
                song=song,
                user_name=user_name,
                needed=cost,
            )

    # -------------------------------------------------------- 收尾
    def mark_played(self, order_id: int, note: str = "", now_ts: Optional[int] = None) -> bool:
        ts = int(now_ts if now_ts is not None else time.time())
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE song_order_log SET status=?, played_ts=?, note=?"
                " WHERE id=? AND status=?",
                (STATUS_PLAYED, ts, (note or "")[:200], int(order_id), STATUS_PENDING),
            )
            return cur.rowcount == 1

    def refund(self, order_id: int, note: str = "", now_ts: Optional[int] = None) -> bool:
        """把一笔 pending 的订单退分（幂等：只有 pending 才能退，退过就退不动）。"""
        ts = int(now_ts if now_ts is not None else time.time())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT scope, user_id, cost, status, is_free, ts FROM song_order_log WHERE id=?",
                (int(order_id),),
            ).fetchone()
            if row is None or str(row["status"]) != STATUS_PENDING:
                return False
            cur = conn.execute(
                "UPDATE song_order_log SET status=?, note=? WHERE id=? AND status=?",
                (STATUS_REFUNDED, (note or "playback failed")[:200], int(order_id), STATUS_PENDING),
            )
            if cur.rowcount != 1:
                return False
            cost = int(row["cost"])
            if cost > 0:
                conn.execute(
                    "UPDATE checkin_users SET total_points = total_points + ?, updated_at = ?"
                    " WHERE group_id=? AND user_id=?",
                    (cost, ts, str(row["scope"]), str(row["user_id"])),
                )
            if int(row["is_free"] or 0) == 1:
                # 免费次数也要还回去，否则一次播放失败会白吃一格
                conn.execute(
                    "UPDATE song_free_usage SET used = used - 1"
                    " WHERE scope=? AND user_id=? AND day=? AND used > 0",
                    (
                        str(row["scope"]),
                        str(row["user_id"]),
                        day_str(int(row["ts"] or ts), self.settings.tz),
                    ),
                )
            return True

    # -------------------------------------------------------- 查询
    def sweep_stale_pending(self, older_than: int = 600, now_ts: Optional[int] = None) -> int:
        """把卡在 pending 太久的订单退分（进程崩在「扣完分、还没 mark_played」之间）。

        只动 ts 早于 now-older_than 的 pending，正常播放中的订单不会被打到。
        """
        ts = int(now_ts if now_ts is not None else time.time())
        cutoff = ts - max(0, int(older_than))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM song_order_log WHERE status=? AND ts <= ?",
                (STATUS_PENDING, cutoff),
            ).fetchall()
        finally:
            conn.close()
        fixed = 0
        for row in rows:
            if self.refund(int(row["id"]), "auto refund: stale pending", ts):
                fixed += 1
        return fixed

    def history(self, scope: str, user_id: str, limit: int = 5) -> list[OrderRow]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, song, cost, is_free, status, ts FROM song_order_log"
                " WHERE scope=? AND user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (str(scope), str(user_id), int(limit)),
            ).fetchall()
            return [
                OrderRow(
                    id=int(r["id"]),
                    song=str(r["song"]),
                    cost=int(r["cost"]),
                    is_free=bool(r["is_free"]),
                    status=str(r["status"]),
                    ts=int(r["ts"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def spend_ranking(self, scope: str, limit: int = 10, since_ts: Optional[int] = None) -> list[RankRow]:
        sql = (
            "SELECT user_id, MAX(user_name) AS user_name, SUM(cost) AS spent, COUNT(*) AS orders,"
            " MIN(ts) AS first_ts FROM song_order_log"
            " WHERE scope=? AND status=?"
        )
        params: list[Any] = [str(scope), STATUS_PLAYED]
        if since_ts is not None:
            sql += " AND ts >= ?"
            params.append(int(since_ts))
        sql += " GROUP BY user_id ORDER BY spent DESC, first_ts ASC LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out: list[RankRow] = []
        for idx, r in enumerate(rows, start=1):
            out.append(
                RankRow(
                    user_id=str(r["user_id"]),
                    user_name=str(r["user_name"] or r["user_id"]),
                    spent=int(r["spent"] or 0),
                    orders=int(r["orders"] or 0),
                    rank=idx,
                ),
            )
        return out

    def stats(self, scope: str) -> SpendStats:
        start = day_start_ts(self.settings.tz)
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS s FROM song_order_log"
                " WHERE scope=? AND status=?",
                (str(scope), STATUS_PLAYED),
            ).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS s FROM song_order_log"
                " WHERE scope=? AND status=? AND ts >= ?",
                (str(scope), STATUS_PLAYED, start),
            ).fetchone()
        finally:
            conn.close()
        return SpendStats(
            total_orders=int(total["n"] or 0),
            total_spent=int(total["s"] or 0),
            today_orders=int(today["n"] or 0),
            today_spent=int(today["s"] or 0),
        )


# ------------------------------------------------------------------ 文案


def format_success(outcome: OrderOutcome, template: str, name: str = "") -> str:
    return render(
        template,
        name=name or outcome.user_name,
        song=outcome.song,
        cost=outcome.cost,
        balance=outcome.balance,
        free_left=outcome.free_left,
    )


def format_insufficient(outcome: OrderOutcome, template: str, name: str = "") -> str:
    return render(
        template,
        name=name or outcome.user_name,
        song=outcome.song,
        cost=outcome.needed,
        balance=outcome.balance,
        lack=max(0, outcome.needed - outcome.balance),
        free_left=outcome.free_left,
    )


def format_history(rows: Iterable[OrderRow], balance: int, tz=None) -> str:
    items = list(rows)
    head = f"🧾 我的点歌账单｜余额 {balance} 分"
    if not items:
        return head + "\n还没有点歌记录，试试发「点歌 歌名」～"
    status_map = {STATUS_PLAYED: "已播放", STATUS_PENDING: "进行中", STATUS_REFUNDED: "已退分"}
    lines = [head]
    zone = tz or get_tz(DEFAULT_TIMEZONE)
    for row in items:
        stamp = datetime.fromtimestamp(row.ts, zone).strftime("%m-%d %H:%M")
        money = "免费" if row.is_free else f"-{row.cost} 分"
        lines.append(f"· {stamp} {row.song} {money}（{status_map.get(row.status, row.status)}）")
    return "\n".join(lines)


def format_ranking(rows: Iterable[RankRow], scope_label: str, title: str = "点歌消费榜") -> str:
    items = list(rows)
    head = f"🏆 {title}｜{scope_label}"
    if not items:
        return head + "\n📭 还没有人点歌，第一个上榜首的就是你～"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [head]
    for row in items:
        medal = medals.get(row.rank, f"{row.rank}.")
        lines.append(f"{medal} {row.user_name} — {row.spent} 分 / {row.orders} 次")
    return "\n".join(lines)