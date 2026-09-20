# astrbot_plugin_airi_core

Airi 的 AstrBot 辅助核心插件，提供群管理、入群欢迎与 Poke 统计排行榜等功能。

## 功能

- LLM 群禁言工具，可配置允许的禁言时长范围。
- Bot 加入新群时发送自定义欢迎文字与图片。
- 记录群友“戳一戳” Bot 的次数，每个 QQ 独立累计。
- 同时支持当前群排行榜与所有群总排行榜。
- Poke 排行榜使用粉白 / 紫粉渐变卡片生成 PNG 图片，前三名突出显示。
- Poke 数据持久化保存，AstrBot 重启或插件重载后不会丢失。

## Poke 排行榜

### 指令

| 指令 | 说明 |
| --- | --- |
| `/poke排行` | 查看当前群的 Poke 排行榜 |
| `/poke排行 123456789` | 查看指定 QQ 在当前群累计戳 Bot 的次数，并同时展示当前群榜单 |
| `/poke总排行` | 查看所有群合计的 Poke 排行榜 |
| `/poke总排行 123456789` | 查看指定 QQ 在所有群的累计次数，并同时展示总榜 |

### 统计规则

只有 OneBot / aiocqhttp 上报的群内 Poke，且被戳目标 `target_id` 等于 Bot 自己的 QQ 时才会计数。因此：

- 群友戳 Bot：计数。
- 群友互相戳：不计数。
- Bot 自己触发的 Poke：不计数。
- 当前群榜按群号分别统计。
- 总榜把同一个 QQ 在所有群中的次数相加。

数据保存在：

```text
data/plugin_data/astrbot_plugin_airi_core/poke_stats.json
```

生成的排行榜图片也会临时保存在同一目录，插件会自动清理较旧图片，只保留最近一部分。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `mute_tool_enabled` | `false` | 启用 LLM 禁言工具 |
| `mute_duration_min` | `1` | 最短禁言分钟数 |
| `mute_duration_max` | `10` | 最长禁言分钟数 |
| `welcome_enabled` | `false` | 启用 Bot 入群欢迎 |
| `welcome_message` | 内置欢迎语 | 欢迎文字 |
| `welcome_images` | `[]` | 欢迎图片 |
| `poke_stats_enabled` | `true` | 启用 Poke 统计与排行榜 |
| `poke_rank_limit` | `10` | 图片排行榜显示人数，范围 3~30 |

## 中文字体

排行榜渲染会自动尝试 Noto Sans CJK、文泉驿、微软雅黑等字体。Linux / 树莓派若没有中文字体，推荐安装：

```bash
sudo apt update
sudo apt install fonts-noto-cjk
```

## 依赖

```text
Pillow>=10.0.0
```
