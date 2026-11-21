# podwatch · 播客自动监控

> 每天自动问一遍「我追的播客有没有新的一期」，有就**把能拿到的全文/转写抓下来存档**，再汇总成一份 5 分钟能扫完的日报。
> 只依赖 Python 标准库；不配任何 key 也能跑（拿不到全文的会进「待转写」队列，等你配 STT）。

配套阅读：
- 选品建议（哪些节目值得追、为什么、成本多少）→ [播客推荐与监控方案.md](../../播客监控/播客推荐与监控方案.md)
- 当前产出 → [播客监控/](../../播客监控/)（数据目录，不进 git）

---

## 1｜它解决什么问题

播客的问题不是「没内容」，而是**60–180 分钟的音频无法检索**。本工具把「订阅 → 发现新一期 → 拿到文字 → 排队读」这条链路自动化，并**明确告诉你哪一期只拿到了 show notes**——不假装有全文。

转写可得性只有三档，工具按顺序降级：

| 档位 | 判断 | 成本 | 典型节目 |
|---|---|---|---|
| ① RSS 自带全文 | `content:encoded` 够长（默认 ≥6000 字） | 0 | qiaomu.ai 8 个精读站、Dwarkesh Substack |
| ② 官网单集页有转写 | 抓页面正文；太短就跟随页面里的 transcript 链接 | 0 | Lex、Acquired、Cognitive Revolution、Latent Space、TBPN |
| ③ 只有音频 | 下载音频 → STT（需配置；不配就入队） | 0（本地）或 ~$0.006/分钟（API） | 硅谷101、Odd Lots、20VC、No Priors、中文非 qiaomu 节目 |

**不配置 STT 时，③ 类只入队不下载**，不会偷偷烧钱；`auto_transcribe=false` 可以让配了 STT 也先不跑。

---

## 2｜目录与文件

```
gongzhonghao/podwatch/            ← 代码（进 git）
├── podwatch.py                   ← 主程序（单文件，约 1,900 行）
├── podcasts.json                 ← 订阅配置（44 个节目，33 个启用）
├── test_podwatch.py              ← 33 项单测（全离线，含 mock STT 服务）
└── README.md

播客监控/                          ← 数据（不进 git，默认 paths.out）
├── state.json                    ← 已见条目 / 源健康 / 最近 50 次运行
├── 文章/<节目id>/<日期>_<标题>.md  ← 每期一篇（front matter + 提要 + 全文/待转写）
│                    .meta.json    ← 同名字段的机器可读版（给后续 LLM 摘要/检索用）
├── 转写/<节目id>/*.transcript.json ← 流水线标准格式（段级时间戳；没有就按章节时间戳）
├── 队列/待转写.jsonl              ← 只有音频、缺全文的条目（上限 500 条）
└── 输出/日报_YYYY-MM-DD.md        ← 每日日报（默认只列 S/A）
```

---

## 3｜快速开始

```bash
cd ~/Desktop/公众号/gongzhonghao/podwatch

# 1. 看订阅源健康度（44 个源，约 20–25 秒）
python3 podwatch.py check

# 2. 抓一轮（首次接入每个节目回看最近 7 天、最多 3 期；约 20–35 秒）
python3 podwatch.py poll

# 3. 出今天的日报（同时写 输出/日报_日期.md）
python3 podwatch.py digest

# 4. 以后每天一条：抓 + 出日报（cron 就调这个）
python3 podwatch.py run

# 5. 只看稿：按节目统计已入档篇数 / 待转写队列
python3 podwatch.py index
```

首次跑会建 40 篇左右的建档（首次接入的回看），之后每次只处理**真正新增**的期数（实测重跑 `poll` 为 0 期）。

---

## 4｜命令表

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `poll` | 轮询 + 去重 + 抓内容 + 落文章 | `--dry-run` 只看会发生什么；`--podcast id` 只抓某个；`--since 2026-11-01` 指定起始；`--history` 首次接入回灌全部历史 |
| `digest` | 生成日报（默认今天；同时写文件并打印） | `--date`、`--days 7`、`--tiers S,A`、`--limit 10`、`--write 路径` |
| `run` | `poll` + 当天日报 + 最近 7 天汇总 | `--notify` 推飞书 |
| `check` | 源健康检查（不写 state）：状态/期数/最近更新/距今天/是否全文 | `--json` |
| `index` | 每个节目已入档篇数、全文篇数、最近入档 | |
| `transcribe` | 处理「待转写」队列 | `--limit 3` 只跑 3 期（先试水不烧钱） |
| `inbox` | 把某天已拿到全文的 S/A 期按规则投进出稿流水线 `orchestrator/inbox/` | `--date`、`--pick 2`、`--tiers S,A`、`--dry-run` |
| `cron` | 打印可直接粘贴的 crontab 行（绝对路径已解析） | `--no-notify` |
| `find` | 用 Apple 播客搜索找 RSS | `--keyword "odd lots"` |
| `add` | 搜索并写入配置（不用手改 JSON） | `--keyword`、`--tier A`、`--lang en`、`--transcript auto` |

公共参数放在子命令前后都行：`--config`、`--out`、`--quiet`。

---

## 5｜配置字段（podcasts.json）

```jsonc
{
  "id": "lex",                 // 落盘目录名，唯一
  "name": "Lex Fridman Podcast",
  "lang": "zh|en",             // 影响 STT 的 language 提示
  "tier": "S|A|B|C",           // S 必追 / A 值得听 / B 扫标题 / C 归档
  "feed": "https://...",       // RSS / Atom
  "site": "https://...",       // 节目官网（可选，仅备注）
  "fulltext": true,            // true=feed 自带全文 / false=只有笔记 / "auto"=按长度判断
  "transcript": "feed|site|audio|auto|none",
  "enabled": true,             // false = 不参与轮询（C 级默认关）
  "site_transcript_link": "-transcript",  // 单集页正文太短时，按此正则跟随转写链接
  "skip": ["\\[AINews\\]"],    // 标题命中正则的不入档（日更资讯栏目）
  "note": "一句话：为什么值得追",
  "tags": ["AI", "研究"]
}
```

`defaults` 里几个关键旋钮：

| 键 | 默认 | 说明 |
|---|---|---|
| `baseline_days` | 7 | 首次接入回看几天；也限制「任何一轮」只入档这个时间窗内的条目 |
| `baseline_max_per_feed` | 3 | 首次接入每个节目最多入档几期（防止 feed 一大堆历史一次灌进来；`--history` 可回灌被跳过的基线条目） |
| `min_fulltext_chars` | 6000 | 判定 feed 正文算不算全文 |
| `min_site_chars` | 3000 | 官网页面正文低于此值 → 视为没抓到 |
| `auto_transcribe` | true | 配了 STT 时是否在 poll 里直接转写；false = 只入队 |
| `audio_max_mb` | 300 | 单集音频下载上限 |

---

## 6｜配 STT（想给「只有音频」的节目拿全文）

按优先级自动探测：

**A. OpenAI 兼容接口**（最简单，按分钟计费）

```bash
export GZH_STT_BASE_URL=https://api.openai.com/v1   # 或任何兼容端点
export GZH_STT_API_KEY=sk-...
export GZH_STT_MODEL=whisper-1                      # 或 gpt-4o-transcribe 等
python3 podwatch.py transcribe --limit 3            # 先试 3 期
```

> 大文件自动用 ffmpeg 切 10 分钟/片（32k 单声道），逐片转写再拼时间戳；不需要手动分片。

**B. 本地 whisper（Apple Silicon 推荐，免费）**

```bash
pip3 install mlx-whisper            # 首次运行会下模型（large-v3-turbo 约 1.5GB）
# 或：brew install whisper-cpp && 下载 ggml 模型，设 GZH_STT_MODEL_PATH=模型路径
python3 podwatch.py transcribe
```

代码自动探测 `mlx_whisper` / `whisper-cli` / `whisper` 三个命令。

**成本参考**：S/A 里「只有音频」的节目约 10–12 小时/周音频；API 全量转写约 $4/周（≈30 元），本地跑是 0。

---

## 7｜定时运行

### cron（最省事）

```bash
crontab -e
# 每天 08:10 抓一轮 + 出日报 + 推飞书（不需要 notify 就删掉 --notify）
10 8 * * * mkdir -p ~/Desktop/公众号/播客监控/日志 && cd ~/Desktop/公众号/gongzhonghao/podwatch && /usr/bin/python3 podwatch.py run --notify >> ~/Desktop/公众号/播客监控/日志/cron.log 2>&1
```

macOS 上 cron 若报 `Operation not permitted`，是系统权限问题，改用 launchd：

```xml
<!-- ~/Library/LaunchAgents/ai.qiaomu.podwatch.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>ai.qiaomu.podwatch</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>~/Desktop/公众号/gongzhonghao/podwatch/podwatch.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>~/Desktop/公众号/gongzhonghao/podwatch</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>10</integer></dict>
  <key>StandardOutPath</key><string>~/Desktop/公众号/播客监控/日志/launchd.log</string>
  <key>StandardErrorPath</key><string>~/Desktop/公众号/播客监控/日志/launchd.err</string>
</dict></plist>
```
`launchctl load ~/Library/LaunchAgents/ai.qiaomu.podwatch.plist`

### 推送到飞书

`export FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx`，然后 `run --notify`。

---

## 8｜接出稿流水线（从日报到一篇稿）

```bash
# 今天有 9 期拿到了全文，先只投前 2 期进 orchestrator/inbox/
python3 podwatch.py inbox --pick 2

# 然后按你们现有 SOP 出稿（先 stub 看流程，真跑换 provider）
cd ~/Desktop/公众号/gongzhonghao
python3 -m orchestrator.loop tick --provider stub --dry-run
```

投递规则（对齐 `orchestrator/inbox/README.md`）：`播客_标题.md` 里含标题 + 原集链接 + 备注；
同名 `播客_标题.json` 是转写附件（流水线标准 `transcript.json` 格式，章节级时间戳）。
重复执行幂等，不会重复投。

---

## 9｜故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `SSL: CERTIFICATE_VERIFY_FAILED` | 脚本已自动用 `certifi` 兜底；若仍报，`pip3 install certifi` |
| 某节目 `FAIL` 但别的不受影响 | `check` 看错误；单源失败只记 `last_error`，不会中断整轮 |
| `state.json 解析失败` | 自动备份成 `state.broken-*.json` 并重建，日志里有记录 |
| 首次跑就把历史灌进来 | 检查 `baseline_days` / `baseline_max_per_feed`；已入档的旧条目不会被删除，需要的话手动清理 `文章/` 与 `state.json` |
| 英文节目目录里出现多篇同标题 | 已按「标题归一化 + 发布日 ±2 天」去重（beehiiv/Substack 双发很常见）；仍有漏网可加 `skip` 正则 |
| `YouTube` 相关 | 本工具不走 YouTube；本机 `yt-dlp` 抓字幕会被风控（需 cookies），故英文节目优先用官网转写 |

---

## 10｜纪律（重要）

- **转写/精读只作检索底稿**：qiaomu 的精读是第三方二次改写，引用到成稿前必须回听原音频核对（对齐 SOP v8 §03「引文需回听」）。
- **不要拿全文直接当稿件发**：本工具的产物用于选题判断与素材定位；成稿仍走 `流水线/` 的 SOP 与质检门禁。
- **频率克制**：一次 `poll` 对每个源只发 1–2 个请求；不要把这个脚本改成分钟级轮询。
