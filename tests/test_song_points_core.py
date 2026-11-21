"""song_points_core 单测（纯标准库，不需要 AstrBot、不需要 pytest）。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from song_points_core import (  # noqa: E402
    OUTCOME_DUPLICATE,
    OUTCOME_FREE,
    OUTCOME_INSUFFICIENT,
    OUTCOME_NO_LEDGER,
    OUTCOME_OK,
    STATUS_PLAYED,
    STATUS_REFUNDED,
    InvalidArgument,
    Settings,
    SongPointsStore,
    day_start_ts,
    day_str,
    format_ranking,
    normalize_song,
    render,
    strip_command_prefix,
)

# 与签到插件 checkin_core._SCHEMA 一致的余额表（只测本插件要用的部分）
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


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "checkin.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(CHECKIN_SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_store(self, **cfg) -> SongPointsStore:
        settings = Settings.from_config(
            {
                "cost_per_song": 10,
                "free_daily_quota": 0,
                "timezone": "Asia/Shanghai",
                **cfg,
            },
        )
        store = SongPointsStore(self.db, settings)
        store.init_db()
        return store

    def grant(self, group_id: str, user_id: str, points: int) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO checkin_users (group_id, user_id, user_name, total_points, updated_at)"
            " VALUES (?,?,?,?,0)"
            " ON CONFLICT(group_id, user_id) DO UPDATE SET total_points = ?",
            (group_id, user_id, user_id, points, points),
        )
        conn.commit()
        conn.close()

    def points(self, group_id: str, user_id: str) -> int:
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT total_points FROM checkin_users WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0

    def order(self, store, key="k1", song="晴天", user="10001", group="88888", **kw):
        return store.request_order(group, user, f"用户{user}", song, key, **kw)


class TestOrder(StoreTestBase):
    def test_deduct_and_balance(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        out = self.order(store)
        self.assertEqual(out.status, OUTCOME_OK)
        self.assertEqual(out.cost, 10)
        self.assertEqual(self.points("88888", "10001"), 20)
        self.assertEqual(out.balance, 20)

    def test_insufficient_does_not_deduct(self) -> None:
        self.grant("88888", "10001", 5)
        store = self.make_store()
        out = self.order(store)
        self.assertEqual(out.status, OUTCOME_INSUFFICIENT)
        self.assertEqual(out.needed, 10)
        self.assertEqual(self.points("88888", "10001"), 5)
        self.assertEqual(store.history("88888", "10001"), [])

    def test_unknown_user_is_zero(self) -> None:
        store = self.make_store()
        out = self.order(store, user="nobody")
        self.assertEqual(out.status, OUTCOME_INSUFFICIENT)
        self.assertEqual(out.balance, 0)

    def test_idempotent_same_key(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        first = self.order(store)
        second = self.order(store, song="改了歌名也不管用")
        self.assertEqual(first.status, OUTCOME_OK)
        self.assertEqual(second.status, OUTCOME_DUPLICATE)
        self.assertEqual(second.order_id, first.order_id)
        self.assertEqual(self.points("88888", "10001"), 20)

    def test_idempotent_under_concurrency(self) -> None:
        self.grant("88888", "10001", 100)
        store = self.make_store()
        results = []
        lock = threading.Lock()

        def worker():
            outcome = self.order(store, key="same-key")
            with lock:
                results.append(outcome.status)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(OUTCOME_OK), 1, results)
        self.assertEqual(results.count(OUTCOME_DUPLICATE), 5, results)
        self.assertEqual(self.points("88888", "10001"), 90)

    def test_group_isolation(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        self.order(store, key="a", group="88888")
        out = self.order(store, key="b", group="99999")
        self.assertEqual(out.status, OUTCOME_INSUFFICIENT)
        self.assertEqual(self.points("88888", "10001"), 20)
        self.assertEqual(self.points("99999", "10001"), 0)

    def test_cost_zero(self) -> None:
        store = self.make_store(cost_per_song=0)
        out = self.order(store)
        self.assertEqual(out.status, OUTCOME_OK)
        self.assertEqual(out.cost, 0)
        self.assertEqual(out.balance, 0)

    def test_admin_free(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store(admin_free=True)
        out = self.order(store, admin=True)
        self.assertEqual(out.status, OUTCOME_OK)
        self.assertEqual(out.cost, 0)
        self.assertEqual(self.points("88888", "10001"), 30)

    def test_no_ledger(self) -> None:
        empty_db = os.path.join(self.tmp.name, "empty.db")
        store = SongPointsStore(empty_db, Settings.from_config({}))
        store.init_db()
        out = store.request_order("1", "2", "u", "歌", "k")
        self.assertEqual(out.status, OUTCOME_NO_LEDGER)


class TestFreeQuota(StoreTestBase):
    def test_free_then_paid(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store(free_daily_quota=2)
        self.assertEqual(store.free_left("88888", "10001"), 2)
        a = self.order(store, key="a", song="a")
        b = self.order(store, key="b", song="b")
        c = self.order(store, key="c", song="c")
        self.assertEqual(a.status, OUTCOME_FREE)
        self.assertEqual(a.cost, 0)
        self.assertEqual(b.status, OUTCOME_FREE)
        self.assertEqual(c.status, OUTCOME_OK)
        self.assertEqual(c.cost, 10)
        self.assertEqual(self.points("88888", "10001"), 20)
        self.assertEqual(store.free_left("88888", "10001"), 0)

    def test_free_resets_next_day(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store(free_daily_quota=1)
        today = day_start_ts(store.settings.tz)
        self.order(store, key="a", song="a", now_ts=today + 60)
        tomorrow = today + 86400
        nxt = self.order(store, key="b", song="b", now_ts=tomorrow + 60)
        self.assertEqual(nxt.status, OUTCOME_FREE)
        self.assertEqual(nxt.free_left, 0)

    def test_free_slot_restored_on_refund(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store(free_daily_quota=1)
        out = self.order(store, key="a")
        self.assertEqual(out.status, OUTCOME_FREE)
        self.assertEqual(store.free_left("88888", "10001"), 0)
        self.assertTrue(store.refund(out.order_id))
        self.assertEqual(store.free_left("88888", "10001"), 1)


class TestRefundAndRecords(StoreTestBase):
    def test_refund_is_idempotent(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        out = self.order(store)
        self.assertTrue(store.refund(out.order_id))
        self.assertFalse(store.refund(out.order_id))
        self.assertEqual(self.points("88888", "10001"), 30)

    def test_mark_played_only_from_pending(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        out = self.order(store)
        self.assertTrue(store.mark_played(out.order_id, "ok"))
        self.assertFalse(store.mark_played(out.order_id, "again"))

    def test_history_and_status(self) -> None:
        self.grant("88888", "10001", 100)
        store = self.make_store()
        a = self.order(store, key="a", song="晴天")
        b = self.order(store, key="b", song="七里香")
        store.mark_played(a.order_id)
        store.refund(b.order_id)
        rows = store.history("88888", "10001", 10)
        self.assertEqual(len(rows), 2)
        statuses = {row.song: row.status for row in rows}
        self.assertEqual(statuses["晴天"], STATUS_PLAYED)
        self.assertEqual(statuses["七里香"], STATUS_REFUNDED)

    def test_ranking_counts_played_only(self) -> None:
        self.grant("88888", "10001", 100)
        self.grant("88888", "10002", 100)
        store = self.make_store()
        a = self.order(store, key="a", user="10001", song="a")
        store.mark_played(a.order_id)
        b = self.order(store, key="b", user="10002", song="b")
        store.refund(b.order_id)
        rows = store.spend_ranking("88888", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, "10001")
        self.assertEqual(rows[0].spent, 10)
        self.assertEqual(rows[0].rank, 1)

    def test_stats(self) -> None:
        self.grant("88888", "10001", 100)
        store = self.make_store()
        a = self.order(store, key="a")
        store.mark_played(a.order_id)
        stats = store.stats("88888")
        self.assertEqual(stats.total_orders, 1)
        self.assertEqual(stats.total_spent, 10)
        self.assertEqual(stats.today_orders, 1)

    def test_sweep_stale_pending(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        old = self.order(store, key="old", now_ts=int(time.time()) - 7200)
        # 正常播放中的订单不该被清掉
        self.assertEqual(store.sweep_stale_pending(600), 1)
        self.assertEqual(self.points("88888", "10001"), 30)
        self.assertFalse(store.refund(old.order_id))

    def test_sweep_keeps_fresh_pending(self) -> None:
        self.grant("88888", "10001", 30)
        store = self.make_store()
        self.order(store, key="fresh")
        self.assertEqual(store.sweep_stale_pending(600), 0)
        self.assertEqual(self.points("88888", "10001"), 20)


class TestParsing(unittest.TestCase):
    def test_normalize_song(self) -> None:
        self.assertEqual(normalize_song("  晴天 "), "晴天")
        self.assertEqual(normalize_song("《七里香》"), "七里香")
        self.assertEqual(normalize_song("“稻香”"), "稻香")
        self.assertEqual(normalize_song("周杰伦   晴天"), "周杰伦 晴天")

    def test_normalize_song_rejects_empty_and_long(self) -> None:
        with self.assertRaises(InvalidArgument):
            normalize_song("   ")
        with self.assertRaises(InvalidArgument):
            normalize_song("歌" * 81)

    def test_strip_command_prefix(self) -> None:
        cmds = ("赏歌", "积分点歌", "金曲")
        self.assertEqual(strip_command_prefix("/赏歌 晴天", cmds), "晴天")
        self.assertEqual(strip_command_prefix("金曲晴天", cmds), "晴天")
        # 长名优先：不能先剥「积分点歌」里的「金曲」
        self.assertEqual(strip_command_prefix("积分点歌 晴天", cmds), "晴天")
        self.assertEqual(strip_command_prefix("！/赏歌 晴天", cmds), "晴天")
        self.assertEqual(strip_command_prefix("随便说点啥", cmds), "随便说点啥")

    def test_render_keeps_unknown_placeholder(self) -> None:
        self.assertEqual(render("扣 {cost} 分 剩 {balance} 分", cost=10, balance=5), "扣 10 分 剩 5 分")
        self.assertEqual(render("x {nope}", cost=1), "x {nope}")

    def test_format_ranking_empty(self) -> None:
        self.assertIn("还没有人点歌", format_ranking([], "本群"))

    def test_day_str_stable(self) -> None:
        ts = 1700000000
        self.assertEqual(day_str(ts, None), day_str(ts, None))


if __name__ == "__main__":
    unittest.main()