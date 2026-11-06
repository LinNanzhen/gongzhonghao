# orchestrator · 多智能体事件循环

对应 [SOP v9](../01_SOP工作流/SOP_v9_多智能体编排.md)。把 v8 的人工串行流程拆成九个智能体，
用状态机推进，云端定时跑；**只在两个地方停下来等人**：选题确认、发布拍板。

```
inbox/ + sources.json → loop.py tick → 九个 agent → 案例目录 + runs/{id}/state.json
```

---

## 快速开始

```bash
cd gongzhonghao

# 1) 投一条素材（二选一）
python3 orchestrator/loop.py submit --url "https://www.youtube.com/watch?v=xxxx" --title "某人访谈"
echo "https://www.youtube.com/watch?v=yyyy" > orchestrator/inbox/下一条.md

# 2) 跑一轮
python3 orchestrator/loop.py tick --scan

# 3) 看状态
python3 orchestrator/loop.py status

# 4) 人工闸门放行
python3 orchestrator/loop.py approve --run-id 1a2b3c4d5e6f --gate topic
python3 orchestrator/loop.py approve --run-id 1a2b3c4d5e6f --gate publish

# 5) 卡住时解封
python3 orchestrator/loop.py resume --run-id 1a2b3c4d5e6f
```

自检（不联网，跑完自动清理）：

```bash
python3 orchestrator/selftest.py        # 端到端：new → archived
python3 -m unittest orchestrator.test_state -v
```

---

## 一次 tick 做什么

1. 扫 `inbox/`（手动投递）与 `sources.json`（RSS，`--scan` 时）；
2. 用 `run_id = sha1(规范化链接)[:12]` 去重，新事件建 run 并分配案例目录；
3. 对每个 `active` 且不在闸门上的 run，**只推进一个阶段**；
4. 落盘 `state.json` 与 `events.log`；
5. 打印等待中/被卡住的 run。

一次只推一步，是为了让失败点可定位、让重跑幂等。

---

## 九个智能体

| agent | 阶段 | 产物 | 打回去向 |
|---|---|---|---|
| `scout` | `new → triaged` | 选题卡 | — |
| `transcriber` | `triaged → transcribed` | 素材卡 | 缺转写 → `blocked` |
| `ledger` | `transcribed → ledgered` | 事实台账 | — |
| `researcher` | `ledgered → researched` | 研究笔记 | — |
| `writer` | `researched → drafted` | `*_人味版.md` | — |
| `critic` | `drafted → critiqued` | 体检报告 | 不过 → 回 `researched` |
| `composer` | `critiqued → composed` | 双 HTML + 渲染图 | 流水线 FAIL → 原地重试 |
| `gatekeeper` | `composed → qc_passed → awaiting_publish` | 质检判定 | 不过 → 回 `critiqued` |
| `archivist` | `published → archived` | 发布差异记录 | — |

---

## 人工闸门（不可绕过）

| 闸门 | 停在 | 放行命令 |
|---|---|---|
| 选题 | `triaged` | `approve --gate topic` |
| 发布 | `awaiting_publish` | `approve --gate publish` |

**没有 `--force-publish`。** 发布动作永远在微信编辑器里由人完成；`approve publish` 只是把状态记为已发布。

---

## 接真实模型

内置 OpenAI 兼容适配器（`providers/openai_compat.py`），覆盖 DeepSeek / Kimi / 通义 / OpenAI，
**只用标准库，无 SDK 依赖**。

```bash
export GZH_LLM_API_KEY=sk-...                          # 必填
export GZH_LLM_MODEL=deepseek-flash                    # 可选，默认 deepseek-flash
export GZH_LLM_BASE_URL=https://api.deepseek.com/v1     # 可选

python3 orchestrator/loop.py tick --provider deepseek
```

别名都指向同一个适配器：`deepseek` · `kimi` · `moonshot` · `qwen` · `dashscope` · `openai` · `openai-compat`。
也可以用 `--model` / `--base-url` 临时覆盖。

| 情况 | 行为 |
|---|---|
| 缺 `GZH_LLM_API_KEY` | tick 直接退出码 2，不会每个 run 各报一次错 |
| 网络错误 / 429 / 5xx | 退避重试 2 次（1s、2s） |
| 400 / 401 / 403 | 立即失败，不重试（重试只会撞更多次） |
| 响应无 choices / 空内容 / 非 JSON | `ProviderError`，该 run 记 `error`，停在原阶段 |

**推理模型注意**：`deepseek-flash` 之类会先输出 `reasoning_content`，它算在 `max_tokens` 里。
预算太小会得到 `finish_reason=length` + 空 content——writer 因此用 8000，ping 用 64。

**换厂商**只改环境变量；**换协议**才需要新增 `Provider` 子类并注册到 `get_provider` 的 `ALIASES`，
agent 代码一行不用改。

### 桩模式的约定

`stub` 输出以 `[STUB]` 开头，agent 看到前缀就改用内置安全骨架，因此端到端能过门禁。
接真实模型后这条分支自然失效。

**接上模型后先跑一篇回归**：真实输出的章节结构未必合规，`critic` 会打回，
打回 3 次转 `blocked`——这时要改的是提示词，不是放宽判据。

## 全自动到什么程度

`policy.json` 控制自动化等级，改文件即可，不用改代码：

| 环节 | 默认 | 说明 |
|---|---|---|
| 发现素材 | 自动 | RSS 扫描 + inbox 投递 |
| 补标题 | 自动 | oEmbed 取标题与频道 |
| 选题 | **自动**（`auto_approve.topic: true`） | scout 用模型填满五项后直接进入转写 |
| 转写 | 半自动 | 见下：飞书导出，或带 cookies 的 yt-dlp |
| 台账/研究/写作/体检/排版/质检 | 自动 | 打回超 3 次转 blocked |
| 发布 | **人工** | 停在 `awaiting_publish`，`auto_approve.publish` 默认关闭 |

自动抓字幕需要 cookies——YouTube 对机房 IP 要求登录验证：

```bash
# 浏览器装 "Get cookies.txt LOCALLY" 插件，登录 YouTube 后导出 youtube.com 的 cookies
gh secret set YT_COOKIES --repo <owner>/<repo> < cookies.txt
```

没有 cookies 时不会静默失败：`transcriber` 落 `blocked`，`转写/yt-dlp.log` 里有 yt-dlp 的原始报错。

## 转写稿怎么进来

云端不跑转写引擎，但**能直接吃飞书妙记的导出**：

```bash
# 飞书妙记 → 右上角「···」→ 导出「文字记录」→ 得到一个 .md
# 放进案例的转写目录（文件名随意，别带「纪要」二字）
cases/01_某某_案例/转写/飞书逐字稿.md

# 手动转换（tick 时也会自动做）
python3 -m orchestrator.transcript "cases/01_某某_案例/转写/飞书逐字稿.md"
```

支持的格式：

| 来源 | 样例 | 识别方式 |
|---|---|---|
| 飞书妙记 | `@说话人 1  00:01` | `@` + 说话人 + 时间戳 |
| SRT | `00:00:01,000 --> 00:00:03,500` | 时间轴行 |
| 纯时间戳 | `00:01` 独占一行 | 时间行 + 正文 |
| 无时间戳 | 任意文本 | 整段作为一条，start=0 |

产物：`{"segments":[{"start":1.0,"end":28.0,"speaker":"说话人 1","text":"…"}]}`。
`transcriber` 发现目录里只有 `.md` 时会自动转换，不需要手动跑命令。

## 目录

```
orchestrator/
├── loop.py            # CLI：tick / submit / status / approve / resume
├── state.py           # 状态机、闸门、非法转移校验
├── events.py          # inbox 解析、RSS 抓取、去重
├── selftest.py        # 端到端自检
├── test_state.py      # 状态机单测（15 个）
├── test_provider.py   # provider 单测（15 个，mock 网络）
├── agents/            # 九类智能体
├── providers/         # LLM 适配层：stub / openai_compat（DeepSeek 等）
├── sources.json       # 源列表（默认空，不联网）
├── inbox/             # 手动投递
└── runs/{run_id}/     # state.json（唯一真相）+ events.log + llm/
```

案例产物写到 `GZH_BASE`（本地默认 `公众号/`，云端 `cases/`），不进 `orchestrator/`。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GZH_BASE` | 仓库父目录 | 案例根目录；Actions 里设为 `$GITHUB_WORKSPACE/cases` |

---

## 云端

[`.github/workflows/gzh-loop.yml`](../.github/workflows/gzh-loop.yml)：每 2 小时 + 手动 + `repository_dispatch`。
一次 job 跑一个 tick，产物与状态 `git commit` 回写，摘要写进 job summary。

启用前需要：

1. 仓库 Settings → Actions → Workflow permissions 选 **Read and write**；
2. 填 `sources.json` 的 `feeds`（不填也能跑，只处理 inbox）；
3. 需要 Issue 提醒时，在 workflow 末尾加一步 `gh issue create`（未内置，避免噪音）。

---

## 已知边界

- **不做发布**：`approve publish` 之后仍要人工在编辑器里点发布。
- **不调用转写引擎**：`transcriber` 不跑 whisper。把飞书妙记导出的逐字稿（`.md`/`.txt`/`.srt`）丢进
  `案例/转写/` 即可，会自动转成带时间戳的 `transcript.json`（见下）。
- **不做事实核验**：`ledger` 只建表；核验状态列由人填，机器不猜。
- **模型输出不合规就 blocked**：critic 打回 3 次即停，不会为了让流程走下去而放宽门禁。
- **RSS 只做发现**：抓不到就只记 warning，不影响其他 run。
