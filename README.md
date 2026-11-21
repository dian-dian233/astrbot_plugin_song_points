# astrbot_plugin_song_points · 点歌消费积分

AstrBot 插件。**点歌要花签到积分**，积分不够就提示去签到。

它不另起一套货币：直接读写签到插件 [`astrbot_plugin_checkin`](https://github.com/dian-dian233/astrbot_plugin_checkin) 的 `checkin.db`，
余额就是签到积分本身（`checkin_users.total_points`）。签到加分、点歌扣分，落在同一个数上。

| 能力 | 说明 |
| --- | --- |
| 原子扣分 | 单条 `BEGIN IMMEDIATE` 事务里「记流水 + 扣分」要么都成、要么都不成 |
| 幂等防重 | 同一条消息（平台 message_id）只扣一次；重复触发直接回「已经处理过」 |
| 会话隔离 | 按群分区（私聊为 `private:<QQ>`），与签到插件同口径，绝不跨群串分 |
| 播放失败自动退分 | 先扣分再播放，播放抛异常/超时原路退回（可关） |
| 真实播放 | 调用你已装的音乐插件注册的 LLM 工具（默认 `play_song_by_name`）真放歌 |
| 附加 | 点歌流水 `/点歌账单`、消费榜 `/点歌榜`、每日免费次数、管理员免单 |

---

## 一、安装

### 前提（缺一不可）

1. AstrBot **4.20 及以上**（已在 4.29.3 源码上对齐 API：`Star` / `filter.command` / `filter.llm_tool` / `context.get_llm_tool_manager().get_func`）。
2. 已装并启用 **astrbot_plugin_checkin ≥ v1.0.0**，并且群里有人 `/签到` 过（
   否则 `checkin.db` 里还没有余额行，点歌会被判 0 分）。
3. 有一个提供音乐播放 LLM 工具的插件（默认工具名 `play_song_by_name`）。没有的话把
   `music_backend` 设成 `none`，插件只扣分记账、不播放。

### 方式 A：WebUI 插件市场

AstrBot 管理面板 → 插件 → 安装插件 → 填仓库地址：

```
https://github.com/dian-dian233/astrbot_plugin_song_points
```

### 方式 B：手动 clone

```bash
cd <AstrBot 根目录>/data/plugins
git clone https://github.com/dian-dian233/astrbot_plugin_song_points.git
# 然后 WebUI 里点「重载插件」，或重启 AstrBot
```

装完在 WebUI 插件列表里应看到 **点歌消费积分 v1.0.0**。

---

## 二、配置项（WebUI → 插件 → 点歌消费积分 → 配置）

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关；关掉后 `/点歌` 只回一句"未开启" |
| `cost_per_song` | int | `10` | 每次点歌扣多少积分；填 0 = 白嫖（只记流水） |
| `free_daily_quota` | int | `0` | 每人每天免费次数，0 = 关闭；按「群 + 人 + 自然日」计 |
| `admin_free` | bool | `false` | 管理员点歌免单（仍记流水） |
| `music_backend` | string | `llm_tool` | `llm_tool` = 调音乐插件真播；`none` = 只记账不播 |
| `music_tool_name` | string | `play_song_by_name` | 要调的音乐工具名 |
| `music_default_platform` | string | 空 | 默认音源（只有工具声明 `platform` 参数时才传） |
| `music_send_mode` | string | 空 | 默认发送方式（只有工具声明 `send_mode` 时才传） |
| `music_timeout` | int | `45` | 播放超时秒数，超时自动退分 |
| `refund_on_failure` | bool | `true` | 播放异常自动退分 |
| `stale_pending_seconds` | int | `600` | 启动时把卡在 pending 超过这么久（进程崩了）的订单自动退分，0 = 关闭 |
| `at_mention_enabled` | bool | `false` | 开「@机器人 + 歌名」触发（群里比较易误触，默认关） |
| `allow_private` | bool | `false` | 允许私聊点歌（签到插件也要开私聊才有分） |
| `allowed_groups` | list | `[]` | 只在这些群生效，空 = 全部群 |
| `blocked_groups` | list | `[]` | 这些群禁用，优先级高于 allowed |
| `dedupe_seconds` | int | `0` | 适配器不给 message_id 时的防重时间窗（秒） |
| `leaderboard_size` | int | `10` | 消费榜条数 |
| `history_size` | int | `5` | 账单条数 |
| `success_template` | text | `✅ 点歌成功｜{song}｜扣 {cost} 分｜剩余 {balance} 分` | 成功文案 |
| `free_template` | text | `🎁 今日免费点歌｜…` | 免费次数文案 |
| `not_enough_template` | text | `⚠️ 积分不够点歌｜…` | 积分不足文案 |
| `timezone` | string | `Asia/Shanghai` | 每日免费次数按此时区跨天，建议与签到插件一致 |
| `checkin_plugin_name` | string | `astrbot_plugin_checkin` | 签到插件目录名（用于定位 checkin.db） |
| `db_path_override` | string | 空 | 高级：直接指定 checkin.db 绝对路径 |

文案占位符：`{song}` `{cost}` `{balance}` `{free_left}` `{name}` `{lack}`。未知占位符会原样保留，不会报错。

**最小配置**：什么都不改就能用（每次 10 分，调 `play_song_by_name` 播放）。
只想让某个群能用就把群号填进 `allowed_groups`。

---

## 三、命令

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `/点歌 <歌名>` | `/song`、`/点歌台` | 花积分点一首，够分则扣分并播放 |
| `/点歌账单` | `/点歌记录`、`/点歌明细` | 查自己最近点歌记录 + 当前余额 |
| `/点歌榜 [今日]` | `/点歌排行`、`/消费榜` | 本群消费榜（默认总榜） |
| `/点歌`（不带参） | — | 帮助 |

带歌手写法：`/点歌 晴天 | 周杰伦` 或 `/点歌 晴天 - 周杰伦`（仅当音乐工具声明了 `artist` 参数时才会传）。

LLM 工具（给模型用）：

- `song_with_points(song_name, artist)` — 替用户点歌并扣分（真播放）。
- `song_points_balance()` — 查当前积分余额和今日免费次数。

---

## 四、实现要点（为什么这么写）

### 1. 为什么不另建一张积分表

需求是「把签到插件当货币底层」。所以余额字段只有一个：签到插件的
`checkin_users.total_points( group_id, user_id )`。本插件不复制、不缓存余额。

### 2. 为什么把本插件的两张表建在 checkin.db 里

SQLite 的**跨库（ATTACH）事务在 WAL 模式下不保证原子**，而签到插件把
`checkin.db` 设成了 WAL。要让「记流水」和「扣分」在同一事务里要么都成、要么都不成，
只能同库同连接。因此本插件在 `checkin.db` 里新增两张**自己的**表：

- `song_order_log`：每笔点歌一行（`dedupe_key` 上有 UNIQUE），状态 `pending / played / refunded`
- `song_free_usage`：每人每天已用免费次数

签到插件的 `checkin_records` / `checkin_users` 表结构本插件**从不修改**（只 UPDATE 余额一列）。

### 3. 扣分流程（一条事务）

```
BEGIN IMMEDIATE
  ① checkin_users 表在不在？不在 → no_ledger（不扣分）
  ② song_order_log 里 dedupe_key 存在？存在 → duplicate（不扣分，也不重播）
  ③ 读余额；有免费次数 → cost=0，免费计数 +1
  ④ 余额 < cost → insufficient（什么都不写）
  ⑤ INSERT 流水（pending，UNIQUE 兜底并发）
  ⑥ UPDATE checkin_users SET total_points = total_points - cost WHERE total_points >= cost
COMMIT
```

- **原子**：⑤⑥ 在同一事务；⑥ 带 `total_points >= cost` 条件，永远扣不成负数。
- **幂等**：同一 `dedupe_key` 第二次进来在 ② 就被挡掉；并发下 ⑤ 的 UNIQUE 也会挡住
  （单测 `test_idempotent_under_concurrency`：6 线程同 key，只扣一次）。

### 4. 播放失败退分

先扣分（防止并发白嫖），播放抛异常/超时 → `UPDATE ... SET status='refunded' WHERE status='pending'`，
行数为 1 才把积分加回去。所以**退分本身也是幂等的**，失败重试不会重复加钱；
免费次数被用掉的话也会还回去。

### 5. 音乐播放怎么接

不改音乐插件、不引依赖，走框架自己的工具注册表：

```python
tool = self.context.get_llm_tool_manager().get_func(self.music_tool_name)
result = tool.handler(event, **kwargs)   # kwargs 只喂它 parameters 里声明的参数名
```

参数名同义词自动识别：`song_name / song / keyword / query / name`。
工具如果 `yield` 了消息段（语音卡片等），本插件会原样转发给会话。

---

## 五、自测

不需要装 AstrBot，纯标准库：

```bash
cd astrbot_plugin_song_points
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

预期：`Ran 48 tests ... OK`。覆盖：

- 扣分/余额、积分不足不扣、群隔离、余额 0 用户
- 幂等（同 key 重复 + 6 线程并发）、退分幂等、免费次数跨天重置与失败归还
- 账单/榜单/统计只算 `played`
- 指令层：无参→帮助、空歌名→帮助、不足→友好提示、黑名单群拦截、白名单群放行、
  音乐工具缺失不扣分、播放异常退分、@机器人开关、LLM 工具
- `_conf_schema.json` 与代码读取的键一一对应、metadata 版本号格式

---

## 六、排错表（按实际踩坑持续补）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 回「积分体系没就位」 | 没找到 `checkin.db`，或库里还没有 `checkin_users` 表 | 先装/启用 `astrbot_plugin_checkin`，让至少一个人 `/签到`；实在定位不到就设 `db_path_override` |
| 回「找不到音乐播放工具」 | `music_tool_name` 与音乐插件注册的工具名不一致，或音乐插件没启用 | WebUI 里看该插件的工具名；或把 `music_backend` 设 `none` 先只记账 |
| 点歌扣了分但没声音 | 音乐插件自己要的音源/凭证没配好 | 这是音乐插件的问题；本插件只在它**抛异常**时退分，返回"播放失败"字符串视为成功 |
| 一条消息扣两次分 | 音乐插件与本插件都注册了 `/点歌`，两个 handler 都跑 | WebUI → 指令管理里禁用其中一个（会显示冲突）；或用 `/点歌` 别名区分 |
| 换个群积分不对 | 积分本来就是按群隔离的（与签到一致） | 想让全群共享得等签到插件支持全局余额，本插件不擅自合并 |
| 私聊点歌没分 | 私聊口径是 `private:<QQ>`，签到插件默认不允许私聊 | 两个插件都开 `allow_private` |
| 重启后积分少了但没听到歌 | 上次进程崩在「扣完分、还没 mark_played」之间 | v1.0.1 起启动自动退分（`stale_pending_seconds`，默认 600 秒）；手动可用 `/点歌账单` 看到 `已退分` 状态 |
| `_empty() takes no arguments` 一类的报错 | 指令 handler 里写了 `*args`/`**kwargs` | 本插件不写：歌名自己从 `event.message_str` 剥前缀解析，无需框架转换 |

---

## 七、版本记录

- **v1.0.1**
  - 新增 `stale_pending_seconds`：插件启动时清扫「扣完分但没来得及落 played」的悬空订单并退分
    （进程崩溃/重启兜底）；退分沿用同一套幂等逻辑，只动 `pending`
  - 自测 45 → 48 条（+2 清扫用例、+1 启动生命周期用例）
- **v1.0.0**（首发）
  - `/点歌`、`/点歌账单`、`/点歌榜`、`@机器人` 触发、LLM 工具 `song_with_points` / `song_points_balance`
  - 复用签到积分库，原子扣分 + 幂等防重 + 播放失败退分
  - 每日免费次数、管理员免单、群黑白名单、三套文案模板

## License

MIT