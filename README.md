# danmaku-it（原 sc_collector）

独立的 B 站直播间「醒目留言（SC）」旁路采集器。自己匿名连接弹幕服务器，不依赖浏览器、不修改 `sc_catch` 扩展。

## 特性

- 匿名连接：不登录，只需要匿名 `buvid` + WBI 签名
- 多房间并发采集，每房间独立重连
- 正确解包 `pv0` / `zlib` / `brotli` 三种协议
- 30 秒心跳，断线指数退避重连（1s → 60s，带抖动）
- 只落 `SUPER_CHAT_MESSAGE`，同时记录 `SUPER_CHAT_MESSAGE_DELETE` 便于事后校对
- SQLite 落库，`sc_id` 主键去重，重连不会重复写入
- 导出 NDJSON / CSV，便于翻译校对与 RAG 使用
- 全量原始 JSON 保留（`raw_json`）
- 界面把表情弹幕直接画成图片：表情图地址在 `info[0][13].url`，只在内存里流转，不落库

## 依赖安装

项目自带虚拟环境，位于 `.venv`，依赖已装好，日常无需再执行安装。

如需重建（Python 3.10+，本机 `D:\Dev\Python\x64\3.13.7\python.exe`）：

```powershell
cd D:\HibaneHinome\danmaku-it
& "D:\Dev\Python\x64\3.13.7\python.exe" -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

依赖：`websockets`、`brotli`、`PySide6-Essentials`（Qt 界面）。

## 配置

`config.json` **不入库**（含 cookie 路径与本机窗口状态），首次使用从模板复制：

```bash
cp config.example.json config.json
```

模板里 `database` 用相对路径指向同一工作区的归档库，`rooms` 换成要采集的直播间：

```json
{
  "rooms": [22748536],
  "database": "../live-archive/.data/translation_history.db",
  "log_file": "logs/collector.log",
  "cookie_file": "",
  "log_level": "INFO",
  "heartbeat_seconds": 30
}
```

- `rooms` 可填短号或真实房间号，程序会用 `getInfoByRoom` 解析成真实 `room_id`；填 `0` 进入离线回放，从库里读真实 payload 走同一套解析，不联网也不写库。
- `database` 与 `log_file` 的相对路径基于 `config.json` 所在目录解析，也可写绝对路径。
- `cookie_file` 支持浏览器导出的 Netscape 格式 cookie（**用绝对路径**，代码不会帮你解析相对路径）。留空即匿名采集——公开直播间的弹幕不需要登录。
- `sessdata` / `dede_user_id` 可选，同样只留在本机。
- **模板文件本身不会被写回**：`config.example.json` 只读，界面的窗口位置、栏高这些只会存进 `config.json`，所以可以直接拿模板试跑而不会污染它。

表结构由 `collector/storage.py` 在首次写入时自动创建，指向一个空文件也能直接用。

## 启动

### 图形界面（悬浮弹幕机）

双击 `gui.bat`，或：

```powershell
cd D:\HibaneHinome\danmaku-it
& ".\.venv\Scripts\python.exe" -m collector gui -c config.json
```

界面说明：

- 无边框悬浮窗，默认置顶，拖动顶部区域即可移动，右下角可拉伸
- 一次只采集一个房间：输入房间号 →「连接」；换房间会先断开再连新的
- 上方为实时弹幕流（自动跟随底部，向上翻阅时不打断滚动）
- 表情弹幕直接画成图片（20px）；图没下到就先按原来的青色 `[表情名]` 显示，下好后自动补画这一行，不会卡住后面的弹幕
- **不显示发送者头像**：头像是一人一图、种类数等于人数，缓存几乎不命中，量大了得不偿失；表情种类有限、重复率高，才值得缓存
- 表情图缓存在 `cache/images/`（原始字节，按 URL 哈希命名），同一个 URL 只下一次，下次开播直接用；删掉这个目录没有任何副作用
- 三栏（弹幕 / 事件 / 醒目留言）可按住标题左侧的 ⠿ 手柄上下拖动换顺序；栏高、顺序与显隐都会记住，顶部「菜单」可隐藏整栏
- 醒目留言与弹幕同样是日志流，每条以粗分隔线隔开，≥100 元加粗橙色高亮
- 顶部「置顶」切换悬浮层级，「设置」可改数据库/日志路径、心跳、重连间隔、日志级别、弹幕保留行数、主题（暗色 / 浅色 / 跟随系统，默认暗色）
- 顶部「导出」可选 NDJSON 或 CSV

性能设计：采集在独立线程，界面每 30ms 批量取一次事件，单帧最多渲染 150 条弹幕，超出部分丢弃并计数；弹幕文档限制 600 行自动截断。高密度房间下界面不会积压卡死。

GUI 与命令行共用同一份 `config.json` 和同一个 SQLite 库，可以随时切换。

### 0 号直播间（真实数据回放）

房间号输入 `0` 就是离线回放：不联网、不写库，素材直接取自 `config.json` 里 `database` 指向的采集库。

- **SC 与礼物用原始 payload 回放**：从 `sc_message.raw_json` / `room_event.raw_json` 读出真实记录，再交给生产同一条 `extract_sc()` 解析，等于把「原始 JSON → 字段 → 界面」整条链路跑一遍，改了解析或渲染随时能复现。
- **译文三种到达方式各跑一遍**：本体自带 `message_trans`、`_JPN` 后到、`_JPN` 先到（缓冲合并）。带译文和不带译文各占一半，方便对照界面上「译：」这一行到底出没出。
- **弹幕是最近一场的连续片段**：按 30 分钟空档切场，跳过末尾那种几十秒的零星短场，所以时间戳单调递增，不会突然跳几个小时。
- 回放节奏压慢了（弹幕约 33 条/秒、SC 约 1.5 秒一条），方便肉眼核对每条排版；列表循环播放，每条 SC 的 `sc_id` 加 `-rN` 后缀区分轮次。
- 库里没有可用数据（路径为空、文件不存在、不是本项目的库）时，自动退回原来的合成数据流。

只用来看渲染和文案，不需要登录，也不会污染采集库。

### 命令行（常驻采集）

```powershell
cd D:\HibaneHinome\danmaku-it
& ".\.venv\Scripts\python.exe" -m collector run -c config.json
```

后台常驻（无窗口）：

```powershell
D:\HibaneHinome\danmaku-it\run.bat
```

或手动：

```powershell
Start-Process -FilePath "D:\HibaneHinome\danmaku-it\.venv\Scripts\pythonw.exe" `
  -ArgumentList "-m","collector","run","-c","D:\HibaneHinome\danmaku-it\config.json" `
  -WorkingDirectory "D:\HibaneHinome\danmaku-it" -WindowStyle Hidden
```

临时覆盖房间、不修改配置文件：

```powershell
& ".\.venv\Scripts\python.exe" -m collector run -c config.json --room 6 --room 21452505
```

## 命令

| 命令 | 说明 |
|---|---|
| `run` | 启动采集（常驻） |
| `resolve --room 6` | 解析房间号、验证 token 与节点可用性 |
| `export --format ndjson --out exports/sc.ndjson` | 导出 NDJSON |
| `export --format csv --out exports/sc.csv` | 导出 CSV（UTF-8 BOM，Excel 可直接打开） |
| `stats` | 查看各房间条数与时间范围 |
| `selftest` | 离线自检：三种解包、去重、导出 |

导出支持过滤：`--room 545068`、`--since <unix秒>`、`--until <unix秒>`、`--with-raw`（仅 NDJSON）。

## 表结构

`sc_message`（SC 主表）：

| 字段 | 说明 |
|---|---|
| `sc_id` | B 站 SC id，**主键**，冲突即忽略（去重） |
| `room_id` | 真实房间号 |
| `uid` / `uname` | 发送者 |
| `price` | 价格（元） |
| `message` | 留言原文 |
| `message_trans` | SC 翻译（来自 `SUPER_CHAT_MESSAGE_JPN`，无翻译时为 NULL） |
| `start_time` | B 站 Unix 秒（权威时间） |
| `end_time` | B 站 Unix 秒，SC 停留结束时间 |
| `received_at` | 本地收到时刻，Unix 毫秒 |
| `guard_level` | 舰队等级（0 普通 / 1 总督 / 2 提督 / 3 舰长） |
| `medal_name` / `medal_level` / `medal_anchor_roomid` / `medal_color` | 粉丝勋章 |
| `raw_json` | 整条原始 payload |

`sc_deletion`：SC 删除事件，一条记录对应一个被删 `sc_id`，原始 JSON 同样保留。
`collect_room`：采集过的房间信息（标题、主播名）。

`room_event`（进场 / 礼物 / 上舰 / SC 删除，**所见即所得**：事件栏里勾选该类型且事件栏可见时才写入，取消勾选或隐藏事件栏则既不显示也不落库；弹幕与 SC 不受影响，始终入库）：

| 字段 | 说明 |
|---|---|
| `seq` | 自增主键 |
| `room_id` | 真实房间号 |
| `event_type` | `enter` 进入直播间 / `gift` 礼物 / `guard` 上舰 / `delete` SC 删除 |
| `event_key` | 去重键：礼物用 `tid`，进场用 `uid:时间:msg_type` |
| `uid` / `uname` | 用户 |
| `gift_name` / `gift_num` / `price` / `coin_type` | 礼物信息（仅 `gift`） |
| `event_time` | B 站 Unix 秒 |
| `received_at` | 本地收到时刻，Unix 毫秒 |
| `raw_json` | 整条原始 payload |

唯一索引 `(room_id, event_type, event_key)`，重复事件自动忽略。

> 进场事件实际走 `INTERACT_WORD_V2`，用户信息在 `data.pb` 的 protobuf 里（字段 1=uid、2=uname、5=msg_type、6=room_id、7=时间戳），由 `protocol.parse_interact_word_v2` 解析。

索引：`(room_id, start_time)`、`start_time`、`received_at`。

## 时间对齐录播

- 以 `start_time`（B 站 Unix 秒）为准，与录播时间轴一致；
- `received_at` 是本地墙钟毫秒，实测与 `start_time` 相差约 0.7 秒（网络 + 服务端推送延迟）；
- 导出里的 `start_offset_seconds = received_at/1000 - start_time`，可用于估算链路延迟；
- 同秒多条 SC 时，`start_time` 无法区分顺序，需要时用 `received_at` 或 `sc_id` 兜底排序。

## 注意事项

- **SC 是低频事件**。热门房间也可能几分钟才一条，日志里 `sc=0` 属正常。
- **匿名连接的普通弹幕昵称会被 B 站打码**（如 `青***`），这是服务端行为，不是本地截断；`SC` 和 `进入直播间` 事件的 `uname` 是完整的。
- **表情图来自 `i0/i1.hdslb.com`**，断网时只影响这一张小图，文字照常显示。`room=0` 回放的历史弹幕没有表情图——库里从来没存这一列，只有新采集的才带。
- SC 翻译来自紧随其后的 `SUPER_CHAT_MESSAGE_JPN`，按 `id` 回填到 `message_trans`；没有翻译的 SC 该字段为空。
- 进场事件在热门房间量很大（30 秒 60 条量级），不需要时取消事件栏的「进场」勾选即可，显示与入库会同时关闭。
- 协议上无法只订阅 SC，必须收全量弹幕再本地过滤；房间越热，带宽和解析开销越大。
- 断线期间 B 站不补发 SC，重连后无法自动补齐历史；需要精确补录时用 `getInfoByRoom` 对账（匿名可见性有限）。
- 匿名连接受风控约束：同 IP 挂过多房间或高频重连可能被临时拒绝，表现为鉴权失败并触发退避重连。
- token 与 `roomid` 强绑定：必须用同一房间号取 token 并鉴权，否则服务端会直接断开连接。
