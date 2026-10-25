# 更好的QQ体验（better_qq）

一个给 **AstrBot + SnowLuma（QQ NT hook → OneBot v11）** 用的聊天体验增强插件。不调用大模型，零额外费用。

## 解决什么问题

| 问题 | 插件怎么处理 |
|---|---|
| SnowLuma 上报的图片链接缺 `rkey`，AstrBot 下载失败、看不到图 | 下载前自动调 SnowLuma `/get_image` 补全链接 |
| 图片和 @ 分两条消息发，模型只看到"@你"看不到上图 | 自动把最近群图片补进本次请求 |
| 引用某张图回复，模型看不到被引用的图 | 读取引用消息里的图片并补入 |
| 只 @ 不打字，模型回"你想说什么" | 自动带上最近聊天记录 |
| 主人顶号（用机器人账号手动发消息），模型不知道"自己"说过什么 | 群聊/私聊都自动补入自己账号发的消息 |
| 每条回复都引用，太机械 | 按概率引用；被 @ 或每条新消息的首回复必引用 |

## 环境要求

- AstrBot >= 4.16（在 4.29.x 上测试）
- SnowLuma（或兼容 `/get_image`、`get_group_msg_history`、`get_friend_msg_history` 的 OneBot v11 实现）
- 不需要额外 pip 依赖（`aiohttp` 随 AstrBot 提供）

## 安装

1. 把本目录放到 `AstrBot/data/plugins/astrbot_plugin_better_qq`；
2. 重启 AstrBot 或在插件管理里重载；
3. 到插件配置里按需调整（默认全部开启）。

## 配置（分 4 个板块）

### 图片修复与补图（image）
| 配置 | 默认 | 说明 |
|---|---|---|
| `snowluma_api` | http://127.0.0.1:3000 | SnowLuma HTTP API 地址 |
| `snowluma_token` | 空 | 手填 SnowLuma HTTP token；留空自动读本机 SnowLuma 配置 |
| `fix_rkey` | 开 | 补全图片 rkey |
| `inject_recent_images` | 开 | 补最近群图片 |
| `reply_image` | 开 | 引用图补入 |
| `lookback_seconds` | 180 | 补图时间窗 |
| `max_recent_images` | 2 | 最多补几张 |

### 回复与引用（dialog）
| 配置 | 默认 | 说明 |
|---|---|---|
| `inject_self_messages` | 开 | 群聊：补顶号消息 |
| `self_msg_window` | 900 | 群聊时间窗（秒） |
| `self_msg_max` | 3 | 群聊最多几条 |
| `inject_self_messages_private` | 开 | 私聊：补顶号消息 |
| `self_msg_window_private` | 900 | 私聊时间窗 |
| `self_msg_max_private` | 3 | 私聊最多几条 |
| `quote_private_min_length` | 15 | 私聊：消息至少多少字才引用（短句不引用） |
| `quote_private_quote_media` | 开 | 私聊：带图片的消息也引用 |
| `quote_probability_group` | 0.25 | 群聊后续回复引用概率 |
| `quote_probability_private` | 0.25 | 私聊后续回复引用概率 |

### 上下文补全（context）
| 配置 | 默认 | 说明 |
|---|---|---|
| `empty_at_hint` | 开 | 只 @ 时补聊天上下文 |
| `empty_at_context_count` | 5 | 附带几条 |

### 生效范围（scope）
| 配置 | 默认 | 说明 |
|---|---|---|
| `scope_groups` | 空=全部 | 生效群号列表 |

## 常见问题

- **图片还是看不到**：确认 SnowLuma 的 HTTP API（默认 3000）可访问、token 正确；插件日志会打印补全成功/失败。
- **装了没效果**：rkey 修复针对 SnowLuma；其他 OneBot 实现若图片链接本来就带 rkey，则只会用到补图/引用/顶号消息等通用功能。
- **AstrBot 升级后失效**：插件通过给 `Image.convert_to_file_path` 打补丁实现，若核心方法改名需要更新。

## 日志关键字

- `[better_qq] 已把最近 N 张图补进本次请求`
- `[better_qq] 已补入自己账号的 N 条消息`
- `[better_qq] 本次回复引用（必引用 / 概率xx%）`

## English

`better_qq` enhances QQ chat experience for AstrBot + SnowLuma: fixes image URLs missing `rkey`, injects recent/quoted images into the LLM request, injects recent chat context for empty mentions, injects messages sent by the bot account itself (manual login), and replies with quotes by configurable probability. No extra LLM calls.

## License

MIT
