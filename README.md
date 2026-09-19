# DS Status (状态页订阅)

![logo](logo.png)

订阅 DeepSeek 状态页的 RSS，定时拉取，有新条目就推送到你指定的会话。

- 作者：Fangnai-byte
- 版本：0.1.0
- 仓库：https://github.com/NekoHome-Studio/astrbot_plugin_ds_status
- 支持平台：aiocqhttp 等 AstrBot 官方平台适配器

## 它做什么

- 默认订阅 `https://status.deepseek.com/feed.rss`，配置里可以换成任意 RSS 2.0 或 Atom 地址。
- 按 `poll_interval_sec`（默认 600 秒）轮询，发现新条目推送给所有已订阅的会话。
- 首次加载只静默记录现有条目，不会刚装好就刷一屏旧消息（想要推送旧条目就把 `initial_sync` 打开）。
- 支持关键词白名单、忽略词、单次推送条数上限、摘要字数上限、是否附带链接。
- 源站不可达时不会静默失败，会把原因写进日志，并在 `/ds帮助` 里显示最近一次错误。

## 安装

把整个 `astrbot_plugin_ds_status` 文件夹放进 `data/plugins/`，在 AstrBot 管理面板里重载插件即可。首次加载会在 `data/plugin_data/astrbot_plugin_ds_status/` 下创建配置文件与状态文件。

## 命令

| 命令 | 作用 | 权限 |
| --- | --- | --- |
| `/ds订阅` | 让当前会话开始接收推送 | 管理类 |
| `/ds退订` | 取消当前会话的订阅 | 管理类 |
| `/ds订阅列表` | 查看已订阅的会话 | 管理类 |
| `/ds检查` | 立即轮询一次，有新条目就推送 | 管理类 |
| `/ds测试` | 往当前会话发一条测试推送 | 管理类 |
| `/ds状态` | 立即拉取并查看最新 3 条（只读，不影响推送记录） | 所有人 |
| `/ds帮助` | 显示订阅源、间隔、上次检查时间等状态 | 所有人 |

## 权限

- 「管理类」命令默认仅管理员可用，可在配置里把 `admin_only` 关掉。
- `/ds状态` 和 `/ds帮助` 任何人都能用。
- `allow_user_ids` 是额外白名单：留空表示所有人都能敲命令；填了 QQ 号则只有列表里的人能用。
- 两层限制的关系：先过 `allow_user_ids`，管理类命令再过 `admin_only`，两者都通过才会执行。

## 配置要点

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | true | 关掉就不轮询，只能手动敲命令 |
| `rss_url` | DeepSeek 状态页 | 订阅源地址，支持 RSS 2.0 与 Atom |
| `poll_interval_sec` | 600 | 轮询间隔，建议不低于 300 秒 |
| `timeout_sec` | 20 | 单次请求超时 |
| `proxy` | 空 | 需要走代理时填 `http://127.0.0.1:7890` 这类地址 |
| `user_agent` | Mozilla/5.0 (AstrBot DsStatus) | 请求头 UA，被源站拒绝时可换一个 |
| `allow_user_ids` | 空 | 命令白名单，留空=所有人 |
| `admin_only` | true | 管理类命令是否仅管理员可用 |
| `initial_sync` | false | 首次运行是否推送已有条目 |
| `push_max_entries` | 3 | 单次最多推送几条 |
| `filter_keywords` | 空 | 只推送包含这些词的条目，留空=全部 |
| `ignore_keywords` | 空 | 跳过包含这些词的条目 |
| `max_content_chars` | 300 | 摘要截断长度 |
| `include_link` | true | 推送里是否带原始链接 |

## 目录结构

```
astrbot_plugin_ds_status/
├── main.py            插件主逻辑：轮询、解析、过滤、推送、命令
├── _conf_schema.json  配置面板定义
├── metadata.yaml      插件元信息
└── README.md          本文件
```

## 数据存放

- 订阅关系、已读条目、最近检查时间与最近错误都记在 `data/plugin_data/astrbot_plugin_ds_status/state.json`。
- 写入使用「临时文件 + 替换」的原子方式，进程被强杀也不会写坏文件。
- 删掉这个文件等于重置订阅；已读记录最多保留 300 条，超出会自动裁剪旧条目。

## 已知问题

- **源站 443 在本机网络下不可达**：`https://status.deepseek.com/feed.rss` 直连会返回 TLS 握手失败（curl 退出码 000），改用 http 明文则返回 403，页面是阿里云「Non-compliance ICP Filing」备案拦截页。这是网络侧的区域限制，不是插件问题。解决办法：配置 `proxy` 走代理，或把 `rss_url` 换成可访问的镜像源。
- **403**：多数是源站按区域或 IP 拦截，先配 `proxy` 再试；也可能是 UA 被拒，换一个 `user_agent`。
- **连接被重置 / 超时**：网络或代理问题，调大 `timeout_sec` 或检查代理是否可用。
- **解析到 0 条条目**：说明返回内容不是 RSS，常见于被拦截页替换掉了正文，插件会把这句话作为错误提示抛出。
- **推送不到**：有些平台接口不支持机器人主动发消息，这种会话即使订阅了也推不过去，日志里会记一条「找不到平台会话」。

## 排错顺序

1. `/ds帮助` 看最近一次错误和上次检查时间。
2. `/ds状态` 手动拉一次，确认源本身能不能取到。
3. 还不行就看日志里的 `[astrbot_plugin_ds_status]` 前缀行。

## 变更记录

- 0.1.0：首版。支持订阅/退订/列表/手动检查/测试，RSS 2.0 与 Atom 解析，关键词过滤，代理配置，错误友好提示。
