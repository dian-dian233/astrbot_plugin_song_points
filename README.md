# astrbot_plugin_song_points · 赏歌消费积分

AstrBot 插件。**点歌要花签到积分**，积分不够就提示去签到。

它不另起一套货币：直接读写签到插件 [`astrbot_plugin_checkin`](https://github.com/dian-dian233/astrbot_plugin_checkin) 的 `checkin.db`，
余额就是签到积分本身（`checkin_users.total_points`）。签到加分、点歌扣分，落在同一个数上。

| 能力 | 说明 |
| --- | --- |
| 原子扣分 | 单条 `BEGIN IMMEDIATE` 事务里「记流水 + 扣分」要么都成、要么都不成 |
| 幂等防重 | 同一条消息（平台 message_id）只扣一次；重复触发直接回「已经处理过」 |
| 会话隔离 | 按群分区（私聊为 `private:<QQ>`），与签到插件同口径，绝不跨群串分 |
| 播放失败自动退分 | 先扣分再播放；**音乐插件返回失败文案（不抛异常）也会退分** —— 只有真放出歌才记账 |
| 音源预检 | 点歌前先看音乐插件有没有已启用音源；一个都没有 → 一分不扣并直接提示（管理员多一行诊断） |
| 真实播放 | 调用你已装的音乐插件注册的 LLM 工具（默认 `play_song_by_name`）真放歌 |
| 附加 | 音源诊断 `/赏歌诊断`、点歌流水 `/点歌账单`、消费榜 `/点歌榜`、每日免费次数、管理员免单 |

---

## 一、安装

### 前提（缺一不可）

1. AstrBot **4.20 及以上**（已在 4.29.3 源码上对齐 API：`Star` / `filter.command` / `filter.llm_tool` / `context.get_llm_tool_manager().get_func`）。
2. 已装并启用 **astrbot_plugin_checkin ≥ v1.0.0**，并且群里有人 `/签到` 过（
   否则 `checkin.db` 里还没有余额行，点歌会被判 0 分）。
3. 有一个提供音乐播放 LLM 工具的插件（默认工具名 `play_song_by_name`），
   **并且该插件里至少启用了一个音源**（netease / qqmusic / kuwo / kugou… 任意一个）。
   没音源时 v1.0.3 起本插件不会扣分，只提示「没有可用音源」；
   想彻底不依赖音乐插件就把 `music_backend` 设成 `none`（只扣分记账、不播放）。

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

装完在 WebUI 插件列表里应看到 **赏歌消费积分 v1.0.3**。

---

## 二、配置项（WebUI → 插件 → 赏歌消费积分 → 配置）

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关；关掉后 `/赏歌` 只回一句"未开启" |
| `cost_per_song` | int | `10` | 每次点歌扣多少积分；填 0 = 白嫖（只记流水） |
| `free_daily_quota` | int | `0` | 每人每天免费次数，0 = 关闭；按「群 + 人 + 自然日」计 |
| `admin_free` | bool | `false` | 管理员点歌免单（仍记流水） |
| `music_backend` | string | `llm_tool` | `llm_tool` = 调音乐插件真播；`none` = 只记账不播 |
| `music_tool_name` | string | `play_song_by_name` | 要调的音乐工具名 |
| `music_default_platform` | string | 空 | 默认音源（只有工具声明 `platform` 参数时才传） |
| `music_send_mode` | string | 空 | 默认发送方式（只有工具声明 `send_mode` 时才传） |
| `music_timeout` | int | `45` | 播放超时秒数，超时自动退分 |
| `health_check_enabled` | bool | `true` | **音源预检**：点歌前先探音乐插件有没有已启用音源，没有就一分不扣 |
| `health_check_on_startup` | bool | `true` | 插件启动时探一次，没音源写告警日志 |
| `health_check_ttl` | int | `300` | 预检结果缓存秒数；改完音源最多等这么久生效，`/赏歌诊断` 立即重探，0 = 每次都探 |
| `music_plugin_name` | string | 空 | 高级：留空即可（自动从工具 handler 反查插件实例）；反查不到才填，如 `astrbot_plugin_music` |
| `empty_result_action` | string | `ok` | 音乐工具成功但返回空时的处理：`ok` = 算成功（默认）/ `refund` = 算失败退分 |
| `unknown_result_action` | string | `ok` | 工具返回无法判定的文本时：`ok` = 算成功（默认）/ `refund` = 算失败退分 |
| `no_source_template` | text | `🎵 音乐插件现在没有可用音源…` | 无音源提示文案，占位符 `{sources}` `{detail}` |
| `failure_template` | text | `⚠️ 「{song}」没放出来，已把 {cost} 分退回…` | 播放失败退分文案，占位符 `{song} {cost} {balance} {reason} {name}` |
| `diagnose_for_all` | bool | `false` | 允许所有人发 `/赏歌诊断`（默认只给管理员） |
| `refund_on_failure` | bool | `true` | 播放失败自动退分（含音乐工具返回失败文案） |
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
| `/赏歌 <歌名>` | `/积分点歌`、`/金曲` | 花积分点一首，够分则扣分并播放 |
| `/点歌账单` | `/点歌记录`、`/点歌明细` | 查自己最近点歌记录 + 当前余额 |
| `/点歌榜 [今日]` | `/点歌排行`、`/消费榜` | 本群消费榜（默认总榜） |
| `/赏歌诊断` | `/音源诊断`、`/赏歌状态` | 管理员诊断：音乐工具是否注册、有没有可用音源、最近一次失败原因 |
| `/赏歌`（不带参） | — | 帮助 |

带歌手写法：`/赏歌 晴天 | 周杰伦` 或 `/赏歌 晴天 - 周杰伦`（仅当音乐工具声明了 `artist` 参数时才会传）。

LLM 工具（给模型用）：

- `song_with_points(song_name, artist)` — 替用户点歌并扣分（真播放）。
- `song_points_balance()` — 查当前积分余额和今日免费次数。

> 工具名从 v1.0.0 起就叫这两个，v1.0.2 改指令时**没动**，现有提示词/调用不受影响。

### 为什么主指令叫 `/赏歌`（v1.0.2）

`/点歌`、`/song`、`/点歌台` 是音乐插件（`astrbot_plugin_music_youtube` 一类）的触发词：

- `astrbot_plugin_music_youtube` 的 `on_search_song` 是 `event_message_type(ALL)` + `is_at_or_wake_command`，
  唤醒前缀 `/` 剥掉后 `message_str` 变成 `点歌 晴天`，它比对 `cmd == "点歌"` 就开播；
  本插件 v1.0.1 也注册了 `/点歌`，于是**同一句话两个 handler 都跑 → 播两次、扣一次分**。

v1.0.2 起本插件不再注册这三个名字，主指令换成 `/赏歌`，别名 `/积分点歌`、`/金曲`。
`/点歌账单`、`/点歌榜` 经核对**与音乐插件不冲突**（它只精确匹配 `cmd == "点歌"`，
平台关键词是 `youtube点歌`/`油管点歌`/`yt点歌`，都不匹配 `点歌账单`、`点歌榜`），因此保留原名。

### 验证没冲突（装完发一次就够）

1. 群里发 `/赏歌 晴天`：只回一条「✅ 点歌成功…扣 10 分」，**只播一遍**。
2. 群里发 `/点歌账单`：只回账单卡片，不触发播放。
3. 群里发 `/点歌 晴天`：只有音乐插件响应，本插件不扣分（`/点歌账单` 里看不到新流水）。
4. WebUI → 指令管理里搜 `赏歌`：只有一个 handler；搜 `点歌`：只有音乐插件的，没有 `astrbot_plugin_song_points` 的。

---

## 三·五、音源怎么配（v1.0.3 重点：这是唯一需要你动手的地方）

本插件**自带音源为空**，它只会调用音乐插件。所以「有积分但放不出歌」99% 是音源没配。

### 用 `astrbot_plugin_music`（Zhalslar，推荐）

1. WebUI → 插件 → 音乐插件 → 配置 → **`players_template`（音乐音源）**。
2. 「添加」一个音源模板，选 `netease` / `qqmusic` / `kuwo` / `kugou` / `txqq` / `neko` / `apple_music` 之类，
   填 `trigger_words`（如 `网易点歌`），把 **`enable`（启用此音源）** 打开。
3. 保存 → 重载插件 → 群里发 `/赏歌 晴天`，再发 `/赏歌诊断` 看结论。

几个要点（都来自该插件自己的配置语义）：

- **只有「已添加且 enable=true」的音源才会被注册**；一个都没启用时它的
  `play_song_by_name` 会返回「已尝试音源：无可用音源」——v1.0.3 就是靠这句识别出来并退分的。
- `priority`（数值越小越优先）决定不给 `platform` 时的回退顺序。
- 指定音源写 `music_default_platform`（如 `kuwo`）；**指定了但那个音源没启用时，音乐插件不会回退**，
  会直接返回「音源「x」不可用」——本插件会把分退掉并告诉你原因。
- 想换音源试试：`/赏歌 晴天` 前后各发一次 `/赏歌诊断`，音源清单会跟着变。

### 没有音源时（v1.0.3 的行为，明确写死）

| 场合 | 行为 |
| --- | --- |
| 普通用户 `/赏歌` | 回「🎵 音乐插件现在没有可用音源，这次不扣分」，**不扣分、不落流水** |
| 管理员 `/赏歌` | 同上，末尾多一行「[管理员诊断] 音源不可用…｜探测方式：get_players_by_priority」 |
| `/赏歌诊断` | 列出后端、工具名、工具是否注册、音源清单/不可用、缓存时间、最近一次失败原因 |
| 播放时才失败的（音源临时挂了） | 扣分 → 失败文案识别 → **原路退分** + 一句人话原因 |
| 自动恢复 | 管理员把音源启用后，最多 `health_check_ttl`（默认 300 秒）后自动恢复；急用就发 `/赏歌诊断` 立刻重探 |

### 为什么插件不自带一个「兜底音源」

不接。原因很实在：兜底音源要么塞第三方 API 的 key（凭证进仓库违反本仓库规矩），
要么依赖某个随时会挂的公开接口（挂了照样放不出歌，还多一层不明责任的依赖）。
所以本插件把它做成**硬依赖 + 预检 + 退分三件套**：没有音源时不骗钱，有音源时调得动。

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

### 4. 播放失败退分（v1.0.3 重写：两道防线）

**旧版漏洞**：只把「抛异常/超时」当失败。但音乐插件的 `play_song_by_name`
**从不抛异常**——它失败也是 `return "音源「x」不可用…"` 这种字符串
（`astrbot_plugin_music` v3.2.6 源码逐条核过，列在 `music_health.py` 头部）。
于是旧版把这些串当成功：扣了分、还回「✅ 点歌成功」，这就是「空扣分」的真因。

v1.0.3 的两道防线：

1. **扣分前预检**（`health_check_enabled`）：从播放工具反查它所属的音乐插件实例
   （`astrbot/core/star/star_manager.py:1299` 把插件工具包成 `partial(fn, star_cls)`），
   直接问 `get_players_by_priority()` / `get_player(default=True)` / `players` /
   `cfg.players.enabled_platforms()`。没有任何音源 → **一分不扣**，直接回提示。
   全程不联网、不放歌、不走 LLM。
2. **扣分后判定**（`music_health.classify_play_result`）：
   - 有音频产物（工具 `yield` 出来的消息段）→ 成功
   - 命中成功特征（`已通过音源` / `正在播放` / `已发送`）→ 成功
   - 命中失败特征（`无可用音源` `无可用播放器` `不可用` `未找到` `失败` `超时` `未知发送方式`…含繁体）→ **退分**
   - 返回 `None`/空 → 默认算成功（该音乐插件关掉 `return_play_song_result` 时，
     发送成功就是返回 `None`，不能误退）；要严格就设 `empty_result_action=refund`
   - 无法判定 → 默认算成功（`unknown_result_action` 可改）

退分依然是幂等的：`UPDATE ... SET status='refunded' WHERE status='pending'`，行数 1 才加余额。
所以「预检说没音源」和「播放失败」两条路径都不会重复退、也不会退成负分。

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
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_music_health.py    # 28 条
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_song_points_core.py # 25 条
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_plugin_smoke.py     # 51 条
```

预期：三条各 `OK`，合计 **104 条**（v1.0.3 新增 47 条）。覆盖：

- 扣分/余额、积分不足不扣、群隔离、余额 0 用户
- 幂等（同 key 重复 + 6 线程并发）、退分幂等、免费次数跨天重置与失败归还
- 账单/榜单/统计只算 `played`
- 指令层：无参→帮助、空歌名→帮助、不足→友好提示、黑名单群拦截、白名单群放行、
  音乐工具缺失不扣分、播放异常退分、@机器人开关、LLM 工具
- 指令冲突（v1.0.2）：旧 `/点歌`、`/song`、`/点歌台` 不在注册表；`/赏歌` 只有一个 handler；
  按框架匹配规则派发 `/赏歌 晴天` 只命中一个 handler 且只扣一次分、只播一次；
  音乐插件的判定规则对我们所有指令名都返回「不归我」；@机器人 遇 `点歌` 前缀让路
- **无音源不扣分（v1.0.3）**：0 音源 → 不扣分、不落流水、不调音乐工具；管理员多一行诊断；
  音源启用后自动恢复；预检结果 TTL 内缓存、TTL=0 每次重探；探测不出来时不阻断
- **播放失败退分（v1.0.3）**：`音源「x」不可用` / `未找到…已尝试音源：A、B` / `发送《》失败` /
  `无可用播放器` 全部退分且余额一分不少；退两次不会多退；免费次数也归还
- **成功不被误退（v1.0.3）**：`已通过音源「酷我音乐（kuwo）」发送《晴天》` 保留扣分；
  `None` 返回默认算成功，配 `empty_result_action=refund` 时退分
- `music_health` 纯函数单测（28 条）：真实成功/失败串判定、繁体串、失败词出现在歌名里不误判、
  音源清单解析、四种音源探测来源、异步/异常实例降级为「未知」、`partial` 反查实例
- `/赏歌诊断` 只有 1 个 handler、不会被 `/赏歌` 抢走
- `_conf_schema.json` 与代码读取的键一一对应（含 v1.0.3 新增 9 项）、metadata 版本号

---

## 六、排错表（按实际踩坑持续补）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 回「积分体系没就位」 | 没找到 `checkin.db`，或库里还没有 `checkin_users` 表 | 先装/启用 `astrbot_plugin_checkin`，让至少一个人 `/签到`；实在定位不到就设 `db_path_override` |
| 回「找不到音乐播放工具」 | `music_tool_name` 与音乐插件注册的工具名不一致，或音乐插件没启用 | WebUI 里看该插件的工具名；或把 `music_backend` 设 `none` 先只记账 |
| 点歌扣了分但没声音 / 回「点歌成功」却没歌 | v1.0.2 及更早：只把抛异常当失败，而音乐插件失败是 `return` 字符串 | **v1.0.3 已修**：失败文案识别 + 退分。升级后照旧现象就发 `/赏歌诊断` 看最后一条原因 |
| 回「音乐插件现在没有可用音源」 | 音乐插件里一个音源都没启用（`players_template` 为空或全 `enable=false`） | 照「三·五、音源怎么配」启用一个音源；启用后最多 `health_check_ttl` 秒自动恢复 |
| 回「音源「x」不可用」 | `music_default_platform` 指定的音源在音乐插件里没启用（指定音源时不回退） | 要么把该音源启用，要么把 `music_default_platform` 清空走优先级回退；已退分，不亏 |
| `/赏歌诊断` 说「音源状态未知」 | 那个音乐插件不暴露音源列表（不是 astrbot_plugin_music 这套接口） | 不影响使用：预检放行，靠播放失败退分兜底；日志里能看到每次失败原因 |
| 一条消息扣两次分 / 播两遍 | 本插件与其他插件注册了同一个指令名，框架只标 conflict 但两个 handler 都执行（`command_management._group_conflicts`） | v1.0.2 已把主指令改成 `/赏歌`，不再占 `/点歌`。仍撞的话照「三、命令 → 验证没冲突」查一遍指令表 |
| `/点歌` 没扣分也没反应 | v1.0.2 起本插件故意不注册 `/点歌`，那条指令归音乐插件 | 花积分点歌请用 `/赏歌 歌名`；只想记账不播放可用 `/赏歌` + `music_backend=none` |
| 换个群积分不对 | 积分本来就是按群隔离的（与签到一致） | 想让全群共享得等签到插件支持全局余额，本插件不擅自合并 |
| 私聊点歌没分 | 私聊口径是 `private:<QQ>`，签到插件默认不允许私聊 | 两个插件都开 `allow_private` |
| 重启后积分少了但没听到歌 | 上次进程崩在「扣完分、还没 mark_played」之间 | v1.0.1 起启动自动退分（`stale_pending_seconds`，默认 600 秒）；手动可用 `/点歌账单` 看到 `已退分` 状态 |
| `_empty() takes no arguments` 一类的报错 | 指令 handler 里写了 `*args`/`**kwargs` | 本插件不写：歌名自己从 `event.message_str` 剥前缀解析，无需框架转换 |

---

## 七、版本记录

- **v1.0.3**（修「扣了分放不出歌」）
  - **只认「真放出歌」才扣分**：判定音乐工具返回值，命中失败特征
    （`无可用音源`/`无可用播放器`/`不可用`/`未找到`/`失败`/`超时`/`未知发送方式`，含繁体）
    一律退分 + 一句人话原因；`None`/空返回按 `empty_result_action` 处理（默认算成功，因为
    `astrbot_plugin_music` 关掉 `return_play_song_result` 时发送成功就返回 `None`）
  - **音源可用性预检**：从播放工具反查音乐插件实例，直接读音源列表（零网络零副作用）；
    0 音源 → 一分不扣，普通用户只看到「没有可用音源」，管理员多一行诊断；
    启动时也探一次并写日志；结果按 `health_check_ttl`（默认 300s）缓存
  - 新增管理员指令 `/赏歌诊断`（别名 `/音源诊断`、`/赏歌状态`）：
    后端 / 工具名 / 工具是否注册 / 音源清单 / 探测方式 / 缓存时长 / 最近一次失败原因
  - 新增配置项 9 个：`health_check_enabled` `health_check_on_startup` `health_check_ttl`
    `music_plugin_name` `empty_result_action` `unknown_result_action`
    `no_source_template` `failure_template` `diagnose_for_all`
  - 新模块 `music_health.py`（纯标准库，可脱离 AstrBot 单跑）：播放结果判定 + 音源探测
  - 明确不接内置兜底音源：硬依赖音乐插件 + 预检 + 退分，README 写清怎么配音源
  - 合并 v1.0.2 的指令迁移（`/点歌` → `/赏歌`），一个版本一起发
  - 自测 57 → 104 条（+47：无音源不扣分、失败串退分、成功不误退、预检缓存、诊断指令、纯函数单测）
- **v1.0.2**
  - 主指令 `/点歌` → **`/赏歌`**，别名改为 `/积分点歌`、`/金曲`；不再注册 `/点歌`、`/song`、`/点歌台`，
    彻底避开音乐插件，同一句话不会两边都播
  - `@机器人` 触发时主动让出音乐插件的触发词（`点歌`/`点歌台`/`song`/`youtube点歌`/`油管点歌`/`yt点歌`），
    修掉「@bot 点歌 X」双播
  - `/点歌账单`、`/点歌榜` 经核对与音乐插件不冲突，保留原名；LLM 工具名不动
  - 自测 48 → 57 条（新增 9 条：旧指令未注册、新指令唯一 handler、按框架规则派发只跑一个 handler、
    别名命中、账单/榜单保名、音乐插件判定不认我们的所有指令名、@机器人让路、LLM 工具名不变）
- **v1.0.1**
  - 新增 `stale_pending_seconds`：插件启动时清扫「扣完分但没来得及落 played」的悬空订单并退分
    （进程崩溃/重启兜底）；退分沿用同一套幂等逻辑，只动 `pending`
  - 自测 45 → 48 条（+2 清扫用例、+1 启动生命周期用例）
- **v1.0.0**（首发）
  - `/点歌`、`/点歌账单`、`/点歌榜`、`@机器人` 触发、LLM 工具 `song_with_points` / `song_points_balance`
    （v1.0.2 起 `/点歌` 迁到 `/赏歌`）
  - 复用签到积分库，原子扣分 + 幂等防重 + 播放失败退分
  - 每日免费次数、管理员免单、群黑白名单、三套文案模板

## License

MIT