# astrbot_plugin_airi_core

Airi 的 AstrBot 辅助核心插件，提供群管理、好友/群邀请审批、入群欢迎与 Poke 统计排行榜等功能。

## 功能

- 可配置 `@Bot` 唤醒 LLM：即使设置了 provider wake prefix，也可以通过直接 @Bot 进入人格对话，并复用当前会话上下文。
- 可配置 LLM 主动 @ 群友工具：只有模型判断确实需要明确点名某位群友时才会 @，普通回复不会自动 @。
- LLM 群禁言工具，可配置允许的禁言时长范围。
- 可选自动同意 OneBot / aiocqhttp 的 QQ 好友申请。
- 可选好友申请 / 邀请 Bot 入群人工审批：请求会私聊发送给指定 QQ，由指定 QQ 决定同意或拒绝。
- 兼容 Snowluma 将好友申请上报成普通私聊“请求添加你为好友”的情况，无需修改 Snowluma / OneBot / AstrBot 内部代码。
- Bot 加入新群时发送自定义欢迎文字与图片。
- 记录群友“戳一戳” Bot 的次数，每个 QQ 独立累计。
- 提供每日当前群榜、每日所有群总榜，以及两套历史累计榜。
- 每日榜以服务器本地时间 **04:00** 为统计日切换点，不依赖定时任务清空数据。
- 当前群日榜与历史榜将“本群总次数 / 参与用户 / 群排名”放在底部统计卡中；顶部仅保留榜单类型与 04:00 刷新提示。
- Poke 排行榜使用粉白 / 紫粉渐变卡片生成 PNG 图片，前三名突出显示，并为每个上榜 QQ 显示圆形头像。
- QQ 头像通过公开 qlogo 地址获取并在本地缓存 24 小时；网络失败时优先使用旧缓存，没有缓存则绘制默认占位头像。
- 排行榜图片不展示完整 QQ 号：当前群优先显示群名片，其次 QQ 昵称；总榜显示 QQ 昵称；获取失败时使用匿名代号。
- Poke 数据持久化保存，AstrBot 重启或插件重载后不会丢失。

## @Bot 唤醒 LLM

v1.3.7 起增加可配置的 `@Bot` 唤醒功能。

当 `self_mention_wakeup_enabled=true` 时，在 QQ / OneBot 群聊中发送：

```text
@Bot 你好
```

插件会检测消息链中是否真的 @ 了 Bot 自己，并显式触发一次 LLM 请求。因此，即使 AstrBot 的 provider wake prefix 配置为 `ch`，`@Bot 你好` 仍可以进入人格对话。

该功能会复用当前 AstrBot conversation，不额外注入 `system_prompt` 或重复拼接人格内容；如果当前还没有 conversation，则按 AstrBot 的会话管理方式创建一个。这样可以尽量保持原有上下文与模型缓存行为，避免因为 @ 唤醒单独构造一套大 Prompt。

为了避免重复请求，@ 唤醒触发显式 LLM 请求后，会阻止同一条消息继续进入 AstrBot 默认 LLM 链路。

规则：

- `@Bot + 文本`：触发 LLM。
- 仅 @ 其他群友：不触发。
- 没有 @Bot：不受该功能影响。
- 仅发送空 @、没有实际文本：不会额外调用 LLM。
- `self_mention_wakeup_enabled=false`：完全关闭此功能，恢复为 AstrBot 原有触发方式。

## LLM 主动 @ 群友

v1.3.8 起增加可配置的 `mention_user` LLM 工具。

当 `mention_tool_enabled=true` 时，LLM 可以在确实需要明确点名、提醒某位群友，或多人同时聊天需要明确指向时调用该工具。普通聊天不需要调用，因此不会变成每条回复都自动 @ 发送者。

工具流程：

1. LLM 调用 `mention_user(user_id=...)`。
2. 插件确认当前为 OneBot 群聊、QQ 号合法、目标不是 Bot 自己，并通过 `get_group_member_info` 确认目标在当前群。
3. 工具只把目标 QQ 记录到当前事件中。
4. 最终回复进入 `on_decorating_result` 时，插件在消息链最前面插入真实 `At` 消息段。
5. 如果最终消息链已经 @ 了同一个人，则不会重复插入。

为了尽量保持 LLM 缓存稳定，这个功能不会在每轮请求里动态修改 `system_prompt`，也不会额外拼接动态上下文。启用时只会增加一个固定的工具 schema；关闭 `mention_tool_enabled` 后该工具不会注册，也不会产生这部分工具 schema 开销。

## 好友申请与群邀请

v1.3.5 起，请求事件按照 AstrBot aiocqhttp 的请求处理方式独立监听，不再混在 notice/Poke 事件处理器中；审批动作直接使用 CQHttp 提供的 `set_friend_add_request` 与 `set_group_add_request`。

v1.3.6 起增加 Snowluma 好友申请兼容：部分 Snowluma 版本不会向 AstrBot 上报 OneBot `post_type=request`，而是把好友申请转换成申请人的普通私聊文本 `请求添加你为好友`。插件会仅在 aiocqhttp 私聊、且文本完全匹配该系统提示时启用兜底，并使用消息发送者 QQ/UIN 调用 Snowluma 已提供的 `set_friend_add_request`。识别后会停止该系统消息继续进入其他插件或 LLM。

此兼容逻辑只位于 Airi Core 插件内部，不需要修改 Snowluma、OneBot 或 AstrBot。

### 自动同意好友申请

在插件配置中开启 `auto_accept_friend_request` 后：

- 标准 OneBot：收到 `post_type=request`、`request_type=friend` 的好友申请时，直接调用 `event.bot.set_friend_add_request(..., approve=True)`。
- Snowluma 兼容：收到申请人的私聊系统文本 `请求添加你为好友` 时，以发送者 QQ/UIN 作为 `flag` 调用同一个 `set_friend_add_request`。
- 该选项默认关闭。
- 只自动处理好友申请，不自动同意群邀请。
- 若同时开启人工审批，**好友申请优先自动同意，不再发送审批通知**；群邀请仍然走人工审批。

### 人工审批

配置：

- `request_approval_enabled=true`
- `request_approval_qq=你的审批 QQ 号`

开启后，需要人工处理的好友申请以及邀请 Bot 入群请求会私聊发送到指定 QQ。Snowluma 好友申请兜底模式下，如果上游没有提供验证信息，通知中会显示 `Snowluma 未提供验证信息`。

每个请求都有独立编号，例如：

```text
【好友申请】A001
昵称：示例用户
QQ：123456789
验证信息：你好

回复 /同意申请 A001 或 /拒绝申请 A001
```

群邀请会同时显示邀请人、群名称与群号。

只有配置的 `request_approval_qq` 有权限执行：

| 指令 | 说明 |
| --- | --- |
| `/同意申请 A001` | 同意对应好友申请或群邀请 |
| `/拒绝申请 A001` | 拒绝对应好友申请或群邀请 |

处理规则：

- 好友申请：调用 `set_friend_add_request(flag=..., approve=...)`。
- 群邀请：调用 `set_group_add_request(flag=..., sub_type="invite", approve=...)`。
- 审批成功后，对应待审批记录立即删除。
- OneBot 审批失败时保留记录，可以稍后再次执行审批命令。
- 非指定 QQ 执行审批命令会提示无权限。
- 待审批信息会持久化，因此 AstrBot 重启后仍可继续处理。

待审批数据保存在：

```text
data/plugin_data/astrbot_plugin_airi_core/pending_requests.json
```

## Poke 排行榜

### 指令

| 指令 | 说明 |
| --- | --- |
| `/poke排行` | 当前统计日当前群榜，默认显示前 10 人 |
| `/poke排行 50` | 当前统计日当前群榜，显示前 50 人 |
| `/poke排行 123456789` | 兼容旧写法：默认显示前 10 人，并查询指定 QQ |
| `/poke排行 50 123456789` | 显示前 50 人，并查询指定 QQ |
| `/poke总排行 [人数] [QQ]` | 当前统计日所有群合计总榜；人数和 QQ 均可选 |
| `/poke历史排行 [人数] [QQ]` | 当前群历史累计榜；人数和 QQ 均可选 |
| `/poke历史总排行 [人数] [QQ]` | 所有群历史累计总榜；人数和 QQ 均可选 |

四个排行命令都遵循相同规则：不写人数时固定显示前 **10** 人；填写人数时最多显示到配置项 `poke_rank_max_limit` 指定的上限。请求人数超过上限时会自动按上限生成。

### 每日刷新规则

每日榜不是通过一个必须在 04:00 准时运行的定时任务去清空，而是使用“统计日”计算：

```text
统计日 = 当前服务器本地时间 - 4 小时 后对应的日期
```

因此：

- 09 月 20 日 03:59 仍属于 09 月 19 日榜。
- 09 月 20 日 04:00 起进入 09 月 20 日榜。
- 即使 AstrBot 在 04:00 时离线、重启或插件没有运行，下一次记录/查询时仍会自动进入正确的新统计日。
- 切日只影响 `/poke排行` 和 `/poke总排行`；历史累计榜不会清零。

从 v1.2.x 升级时，旧 `poke_stats.json` 中的累计次数会完整保留到历史榜。由于旧版本没有记录每次 Poke 的日期，升级前的历史累计数据不会被错误塞进升级当天的日榜；日榜从 v1.3.1 开始按新规则记录。

### 统计规则

只有 OneBot / aiocqhttp 上报的群内 Poke，且被戳目标 `target_id` 等于 Bot 自己的 QQ 时才会计数。因此：

- 群友戳 Bot：计数。
- 群友互相戳：不计数。
- Bot 自己触发的 Poke：不计数。
- 当前群榜按群分别统计。
- 总榜把同一个 QQ 在所有群中的次数相加。

### 头像与隐私展示

统计内部仍以 QQ 号作为稳定用户标识，以保证跨重启、跨群汇总时不会因为昵称变化而把同一用户拆成多人；**完整 QQ 号不会绘制到排行榜图片中**。

- 当前群榜：`群名片 > QQ 昵称 > 匿名用户代号`
- 所有群总榜：`QQ 昵称 > 匿名用户代号`
- 每个实际显示在榜单中的用户会尝试加载其 QQ 头像，并裁剪为圆形显示在昵称左侧。
- 头像缓存默认有效 24 小时；过期后会尝试刷新，刷新失败时仍可继续使用已有缓存。
- 无可用头像时显示默认占位头像，排行榜仍可正常生成。
- 当前群排行榜图片不会显示群号。
- 昵称过长会自动截断，避免破坏卡片排版。

Poke 数据保存在：

```text
data/plugin_data/astrbot_plugin_airi_core/poke_stats.json
```

QQ 头像缓存保存在：

```text
data/plugin_data/astrbot_plugin_airi_core/avatar_cache/
```

生成的排行榜图片也会临时保存在插件数据目录，插件会自动清理较旧图片，只保留最近一部分。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `self_mention_wakeup_enabled` | `true` | 启用 @Bot 唤醒 LLM；复用当前 conversation，不额外注入 system prompt |
| `mention_tool_enabled` | `true` | 启用 LLM 按需主动 @ 群友工具；普通回复不会自动 @ |
| `mute_tool_enabled` | `false` | 启用 LLM 禁言工具 |
| `mute_duration_min` | `1` | 最短禁言分钟数 |
| `mute_duration_max` | `10` | 最长禁言分钟数 |
| `welcome_enabled` | `false` | 启用 Bot 入群欢迎 |
| `welcome_message` | 内置欢迎语 | 欢迎文字 |
| `welcome_images` | `[]` | 欢迎图片 |
| `auto_accept_friend_request` | `false` | 自动同意 OneBot / Snowluma 好友申请 |
| `request_approval_enabled` | `false` | 启用好友申请 / 群邀请人工审批 |
| `request_approval_qq` | 空 | 接收审批通知并有权执行审批命令的 QQ |
| `poke_stats_enabled` | `true` | 启用 Poke 统计与排行榜 |
| `poke_rank_max_limit` | `100` | 单次排行最大允许显示人数，范围 10~100；命令未填写人数时仍默认显示 10 人 |

## 中文字体

排行榜图片优先使用 Noto Sans CJK。Linux / ARM64 环境如果中文显示异常，可以安装：

```bash
sudo apt install fonts-noto-cjk
```
