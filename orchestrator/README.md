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
python3 orchestrator/selftest.py        # 端到端：new → archived（有 Chrome 时连渲染一起验）
python3 -m unittest discover -s orchestrator -p 'test_*.py' -t .
python3 orchestrator/verify_p0.py       # P0 独立验收：临时目录跑 36 项对抗场景
```

`verify_p0.py` 是验收专用脚本（不改产品代码、不写 `cases/` 与 `runs/`），覆盖门禁三态、
渲染判定码/退出码、critic 体检表、一 tick 一步、发布不可自动、版本化、相对路径、坏状态与
并发锁、闸门通知。任何一项 FAIL 都会打印原因并返回退出码 1。

---

## 一次 tick 做什么

1. 扫 `inbox/`（手动投递）与 `sources.json`（RSS，`--scan` 时）；
2. 用 `run_id = sha1(规范化链接)[:12]` 去重，新事件建 run 并分配案例目录；
3. 对每个 `active` 且不在闸门上的 run，**默认只推进一个阶段**（`--steps N` 才连推，排障用）；
4. 落盘 `state.json` 与 `events.log`（时间戳一律 UTC）；
5. 打印等待中/被卡住的 run、坏 `state.json` 告警，并推送一次闸门通知（未配 webhook 静默）。

一次只推一步，是为了让失败点可定位、让重跑幂等。

### tick 的并发与容错（T2）

| 机制 | 行为 |
|---|---|
| 单实例锁 | tick 先拿 `runs/.lock`（`fcntl.flock`，非阻塞）；拿不到就打印"另一个 tick 正在运行，本轮跳过"并退出码 0，不拖垮定时任务 |
| 坏 `state.json` | `RunStore.all()` 收集到 `store.broken`，tick 打印 `[warn] state.json 解析失败：…` 并在 summary 标 `broken=N`，退出码仍为 0；`status` 则返回退出码 1，便于 CI 感知 |
| UTC 时间 | `state.now()` 统一 `+00:00`，本地/云端日志可按字符串排序、算耗时；历史记录不重写 |
| 通知失败 | webhook 超时/HTTP 错误只 `[warn]`，绝不阻塞 tick |

---

## 九个智能体

| agent | 阶段 | 产物 | 打回去向 |
|---|---|---|---|
| `scout` | `new → triaged` | 选题卡 | — |
| `transcriber` | `triaged → transcribed` | 素材卡 | 缺转写 → `blocked` |
| `ledger` | `transcribed → ledgered` | 事实台账 | — |
| `researcher` | `ledgered → researched` | 研究笔记 | — |
| `writer` | `researched → drafted` | `*_人味版_vN.md`（只增不覆盖） | — |
| `critic` | `drafted → critiqued` | 体检报告（逐节机器判两关） | 不过 → 回 `researched` |
| `composer` | `critiqued → composed` | `{嘉宾}_vN_排版预览.html` / `_导入片段.html` / `_质检报告.md` + 渲染图 | 流水线 FAIL → 原地重试 |
| `gatekeeper` | `composed → qc_passed → awaiting_publish` | 按判定码分派（PASS/FAIL/INCOMPLETE） | FAIL → 回 `critiqued` 重排；INCOMPLETE → `blocked` |
| `archivist` | `published → archived` | 发布差异记录 | — |

### 渲染验收契约与退出码（T1）

`composer` 只调 `流水线/gzh_pipeline.py`，不自己拼 HTML、不自己写判定。流水线在质检报告里写
机器可读的 `- 判定码：PASS|FAIL|INCOMPLETE`，进程退出码与之一一对应：

| 判定码 | 退出码 | 含义 | gatekeeper 动作 |
|---|---|---|---|
| `PASS` | 0 | 质检门禁全过 **且**三档 Chrome 渲染真的跑过 | `qc_passed` → 等人工发布 |
| `FAIL` | 1 | 质检门禁 FAIL，或任一 viewport 渲染测量/截图报错、溢出 | `rework` 打回 composer |
| `INCOMPLETE` | 3 | 渲染没跑：`--skip-render` / 找不到 Chrome / 没有 viewport 产出结果 | `blocked`：本地补跑渲染后 `resume`，**不得放行** |

报告存在但解析不出判定码（历史/手工报告）→ `blocked`，不可信不放行。判定字符串只有一份实现：
`orchestrator/verdict.py`（流水线与 gatekeeper 共用，T1c）。**`--skip-render` 永远不会判通过。**

> 环境适配（2026-11-09）：受限环境（DSH 文件沙箱 / CI 容器）里 headless Chrome 需要
> `--no-sandbox` + 独立 `--user-data-dir` 才能产出结果，`流水线/gzh_pipeline.py` 已内置这组参数
> （`chrome_sandbox_args()`）。它只影响渲染进程的启动方式，**不改变判定逻辑**：渲染失败仍判 `FAIL`，
> 渲染没跑成仍判 `INCOMPLETE`（验收脚本用故障注入验证过，见 `verify_p0.py` 的 2.3）。

---

## 人工闸门（不可绕过）

| 闸门 | 停在 | 放行命令 |
|---|---|---|
| 选题 | `triaged` | `approve --gate topic` |
| 发布 | `awaiting_publish` | `approve --gate publish` |

**没有 `--force-publish`。** 发布动作永远在微信编辑器里由人完成；`approve publish` 只是把状态记为已发布。

**发布永不自动（T4a）**：`policy.json` 里已删除 `auto_approve.publish` 键；即使手工写
`{"auto_approve": {"publish": true}}`，代码也不读它——`tick` 后 run 仍停在 `awaiting_publish`、
`gates.publish` 仍为 `false`。只有人 `approve --gate publish` 才会推进。

### 闸门通知与唤醒（T4b）

tick 结束若存在「等人工闸门」或 `blocked` 的 run，会向飞书群机器人推一条摘要
（run_id / 阶段 / 闸门 / 标题 / 处理命令）：

```bash
export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
# 兼容旧名：GZH_FEISHU_WEBHOOK
python3 orchestrator/loop.py tick --scan          # 默认推送
python3 orchestrator/loop.py tick --no-notify     # 本轮关闭
```

| 情况 | 行为 |
|---|---|
| 未配 webhook | 静默返回，不报错、不影响 tick |
| 5s 超时 / HTTP 错误 / 飞书 `code≠0` | 只 `[warn] 闸门通知发送失败`，tick 仍成功 |
| 12h 内同一签名（`run_id|stage|gate|status`） | 不重复推，去重记录在 `runs/.notified.json`，防 2 小时 cron 刷屏 |

**审批入口（T4c，未实测）**：`.github/workflows/gzh-approve.yml` 支持
`workflow_dispatch`（手填 run_id/gate）与 Issue 评论 `/approve <run_id> <topic|publish>`
（仅 OWNER/MEMBER）。**该 workflow 未在真实仓库实测**；触发回写需要配置 `GZH_DISPATCH_TOKEN`
（PAT/App token），`GITHUB_TOKEN` 触发同仓库其他 workflow 有反递归限制，首次使用前必须先手动触发验证一次。

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
| 转写 | **自动**（配好凭证后） | 投飞书妙记链接即调 API 拉转写；YouTube 用 yt-dlp + cookies |
| 台账/研究/写作/体检/排版/质检 | 自动 | 打回超 3 次转 blocked |
| 发布 | **人工** | 停在 `awaiting_publish`；`auto_approve.publish` 键已删除，写 `true` 也不生效（发布永不自动） |

自动抓字幕需要 cookies——YouTube 对机房 IP 要求登录验证：

```bash
# 浏览器装 "Get cookies.txt LOCALLY" 插件，登录 YouTube 后导出 youtube.com 的 cookies
gh secret set YT_COOKIES --repo <owner>/<repo> < cookies.txt
```

没有 cookies 时不会静默失败：`transcriber` 落 `blocked`，`转写/yt-dlp.log` 里有 yt-dlp 的原始报错。

## 版本化与 artifacts 相对路径（T3）

所有产物**只增不删**，重跑不覆盖上一版：

| 产物 | 命名 | 行为 |
|---|---|---|
| 正文 | `{嘉宾}_人味版_v{N}.md` | writer 扫已有 `_v*` 取 max+1；历史 `{嘉宾}_人味版.md` 视为 v1，重跑从 v2 起 |
| 排版 | `{嘉宾}_v{N}_排版预览.html` / `_导入片段.html` / `_质检报告.md` | composer 扫已有 `_v*_排版预览.html` 取 max+1 |
| 体检报告 | `体检报告_YYYY-MM-DD.md` | critic 每次覆盖当天报告（判据产物，不参与版本化） |

**artifacts 存相对 `case_dir` 的 posix 路径**（如 `"draft": "苏姿丰回归_人味版_v2.md"`，
`"qc_report": "苏姿丰回归_v2_质检报告.md"`）。读取时统一用 `ctx.artifact_path(key)` 解析：
相对路径按 `case_dir` 拼；旧 run 的绝对路径存在就用，不存在（如云端 `/home/runner/...` 拉回本地）
则按文件名回退到当前案例目录。写状态时 `loop.step()` 再统一归一化一次，九个 agent 不用各写一套。

## 成本看板（T5）

| 层级 | 实现 |
|---|---|
| provider | 解析响应 `usage` 写 `provider.last_usage`，并累计 `usage_total`（input/output/total/calls）；供应商没回 usage 时如实标 None/0，不伪装 |
| run | `state.json` 增 `usage` 字段；每步用 provider 前后差值累加，`events.log` 附 `usage input=… output=… total=…` |
| 汇总 | `loop.py status` 按 run 最近活动日归集「今日/本周」tokens，缺失的 run 标「未上报」；`status --json` 返回 `usage` 汇总 |

```bash
python3 orchestrator/loop.py status          # 表格 + 今日/本周 tokens
python3 orchestrator/loop.py status --json   # {runs, usage, broken}
```

> 注：`status --json` 结构在 P0 从「list」变为 `{runs, usage, broken}`（T5b 需要汇总与坏文件信息）；
> 仓库内没有脚本消费旧 list 结构，外部自动化如果有请按新结构适配。

## 飞书妙记：投链接就够

有自建应用凭证后，往 inbox 投**妙记链接**即可，不用手工导出：

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx

echo "https://xxx.feishu.cn/minutes/obcnq3b9jl72l83w4f149w9c" > orchestrator/inbox/某期.md
python3 orchestrator/loop.py tick --scan
```

`transcriber` 会调 [`导出妙记文字记录`](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute-transcript/get.md)：

```
GET /open-apis/minutes/v1/minutes/{minute_token}/transcript
    ?need_speaker=true&need_timestamp=true&file_format=srt
```

拿到 SRT 后自动转 `transcript.json`，时间戳与说话人都在。

**开通步骤**：飞书开放平台 → 创建企业自建应用 → 开权限 `minutes:minutes.transcript:export`
→ 拿 App ID / App Secret → 妙记「权限设置」里给该应用导出权限。

失败不会静默：错误写进 `转写/feishu.log`，`blocked` 的原因里会直接说明是缺凭证、无权限还是转写未完成。

## 一次投递：链接 + 转写稿放一起

`inbox/` 支持同名配对，不需要"先建任务、再往案例目录塞文件"：

```
inbox/
├── David_Baker.md      ← 第一行标题，第二行链接
└── David_Baker.srt     ← 同名即附件，建任务时自动复制到 cases/…/转写/
```

只有转写稿也行（本地录音）：`inbox/本地录音.srt`，文件名即标题，`kind=local`。

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
├── loop.py            # CLI：tick / submit / status / approve / resume / rewind / ping
├── state.py           # 状态机、闸门、非法转移校验、UTC 时间、坏 state 收集
├── verdict.py         # 判定码契约（PASS/FAIL/INCOMPLETE），流水线与 gatekeeper 共用
├── versions.py        # 产物版本号扫描（next_version）
├── notify.py          # 闸门通知（飞书 webhook、5s 超时、12h 去重）
├── events.py          # inbox 解析、RSS 抓取、去重
├── selftest.py        # 端到端自检
├── verify_p0.py       # P0 独立验收（临时目录，36 项对抗场景）
├── test_state.py      # 状态机单测（UTC / 坏 state / usage 兼容）
├── test_loop.py       # tick 行为单测（--steps / 锁 / 发布不可自动 / artifacts 相对化）
├── test_gatekeeper.py # 判定三态 + 流水线退出码单测
├── test_critic.py     # 体检表逐节机器判单测
├── test_usage.py      # provider usage 解析单测
├── test_versions.py   # next_version 单测
├── agents/            # 九类智能体
├── providers/         # LLM 适配层：stub / openai_compat（DeepSeek 等）
├── sources.json       # 源列表（默认空，不联网）
├── inbox/             # 手动投递
└── runs/{run_id}/     # state.json（唯一真相）+ events.log + llm/
    ├── .lock          # tick 单实例锁（不进 git）
    └── .notified.json # 通知去重记录（不进 git）
```

案例产物写到 `GZH_BASE`（本地默认 `公众号/`，云端 `cases/`），不进 `orchestrator/`。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GZH_BASE` | 仓库父目录 | 案例根目录；Actions 里设为 `$GITHUB_WORKSPACE/cases` |
| `FEISHU_WEBHOOK` | 空 | 飞书群机器人 webhook，用于闸门通知；不配则静默 |
| `GZH_FEISHU_WEBHOOK` | 空 | 同上，兼容旧名 |
| `GZH_LLM_API_KEY` | 空 | 真实 provider 必填；stub 不需要 |
| `GZH_LLM_MODEL` / `GZH_LLM_BASE_URL` | 空 | 覆盖默认模型与端点 |

---

## 云端

[`.github/workflows/gzh-loop.yml`](../.github/workflows/gzh-loop.yml)：每 2 小时 + 手动 + `repository_dispatch`。
一次 job 跑一个 tick（默认 `--steps 1`），**带 `--render`**（渲染验收不在云端跳过）；产物与状态
`git commit` 回写，摘要写进 job summary（含 broken 数）。

审批入口 [`.github/workflows/gzh-approve.yml`](../.github/workflows/gzh-approve.yml)（T4c）：
`workflow_dispatch` 手填 run_id/gate，或 Issue 评论 `/approve <run_id> <topic|publish>`（仅 OWNER/MEMBER）。
**未在真实仓库实测**：触发回写需要 `GZH_DISPATCH_TOKEN`（PAT/App token）且首次使用前手动触发验证一次。

启用前需要：

1. 仓库 Settings → Actions → Workflow permissions 选 **Read and write**；
2. 填 `sources.json` 的 `feeds`（不填也能跑，只处理 inbox）；
3. 需要 Issue 提醒时，在 workflow 末尾加一步 `gh issue create`（未内置，避免噪音）；
4. 需要闸门通知时配置 `FEISHU_WEBHOOK` secret。

**云端跑不出渲染的后果是 `blocked`，不是「通过」**：runner 没有 Chrome 时流水线判 `INCOMPLETE`，
gatekeeper 落 `blocked`，等本地补跑渲染后 `resume` 再放行。

---

## 已知边界

- **不做发布**：`approve publish` 之后仍要人工在编辑器里点发布；发布永不自动（policy 写 true 也不生效）。
- **渲染未完成 ≠ 通过**：`--skip-render`、无 Chrome、渲染无产出都判 `INCOMPLETE` → `blocked`，必须本地补跑渲染。
- **不调用转写引擎**：`transcriber` 不跑 whisper。把飞书妙记导出的逐字稿（`.md`/`.txt`/`.srt`）丢进
  `案例/转写/` 即可，会自动转成带时间戳的 `transcript.json`（见下）。
- **不做事实核验**：`ledger` 只建表；核验状态列由人填，机器不猜。
- **模型输出不合规就 blocked**：critic 打回 3 次即停，不会为了让流程走下去而放宽门禁。
- **RSS 只做发现**：抓不到就只记 warning，不影响其他 run。
- **一次 tick 默认只推进一个阶段**：连推必须显式 `--steps N`（排障用，不用于定时任务）。
- **GitHub Actions 审批入口未实测**：`gzh-approve.yml` 需要 `GZH_DISPATCH_TOKEN` 与一次手动触发验证。
