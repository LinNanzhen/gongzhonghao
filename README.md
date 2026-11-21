# gongzhonghao · 公众号人物专访内容生产

**内容规则：SOP v10**（2026-11-10，伊利亚篇风格复盘：对话解说式）→ [`01_SOP工作流/人物专访深度报道_标准工作流SOP_v10.md`](01_SOP工作流/人物专访深度报道_标准工作流SOP_v10.md)

**编排规则：SOP v9**（2026-11-09，P0 加固）→ [`01_SOP工作流/SOP_v9_多智能体编排.md`](01_SOP工作流/SOP_v9_多智能体编排.md)

> 本文是仓库导航与速查：**内容判据以 SOP v10 为准，编排判据以 SOP v9 为准**；两者冲突时按 SOP。
> v9 编号用于编排规则，内容规则自 v8 直接升至 v10；v5—v8 保留作历史版本，不覆盖、不删除。
>
> 工作区根目录的 `HANDOFF.md`、`工作区说明.md` 与 `研究/`（审计、P0 验收、实跑记录）不在本仓库，接手先读 `HANDOFF.md`。
> 本文更新：2026-11-10（内容规则升 v10：对话缩写＋中英对照＋人物简介卡＋文末归属声明）。

---

## 📁 仓库结构

```
gongzhonghao/
├── 01_SOP工作流/
│   ├── 人物专访深度报道_标准工作流SOP_v10.md  # ★ 内容规则入口（唯一判据）
│   ├── SOP_v9_多智能体编排.md                 # ★ 编排规则入口（2026-11-09 P0）
│   ├── 人物专访深度报道_标准工作流SOP_v8.md   # 历史
│   ├── 人物专访深度报道_标准工作流SOP_v7.md   # 历史
│   ├── 人物专访深度报道_标准工作流SOP_v6.md   # 历史
│   ├── 历史版本/                              # v7 发布复盘前快照
│   ├── _备份/                                 # v5 原始版
│   └── 模板/                                  # 12 个可复用资产，见下
├── 流水线/                                    # Python 自动化（纯标准库）
│   ├── gzh_pipeline.py                        # 正文 → 双 HTML → 质检判定 → 三档渲染
│   ├── gzh_subtitle.py                        # 字幕长图（3:4）
│   ├── subtitle_skill/native_subtitle_stitch.py
│   └── README_使用说明.md
├── orchestrator/                              # 多智能体事件循环（SOP v9）
│   ├── loop.py                                # tick / submit / status / approve / resume / rewind / ping
│   ├── verdict.py                             # 判定码唯一来源（PASS/FAIL/INCOMPLETE）
│   ├── state.py / versions.py / notify.py     # 状态机 / 产物版本号 / 闸门通知
│   ├── agents/                                # 九类智能体
│   ├── providers/                             # LLM 适配层（stub 默认；openai_compat 接真实模型）
│   ├── selftest.py / verify_p0.py             # 端到端自检 / P0 独立验收（36 项）
│   ├── test_*.py                              # 172 个单测
│   ├── deploy/                                # systemd / launchd / cron 部署样例
│   └── README.md
├── cases/                                     # 编排产物（15、16；随 tick 回写入库，仅文本）
└── .github/workflows/
    ├── gzh-loop.yml                           # 每 2 小时 tick（带 --render）
    └── gzh-approve.yml                        # 审批入口（未实测）
```

**案例默认不在本仓库内**：

- 本地跑稿落在工作区根目录 `NN_XX_案例/`（02—14，不进 git，含音频片段与截图）；
- 云端/编排 tick 写入仓库内 `cases/`（已入库 15、16，文本产物随 `chore(loop): tick 回写` 提交，媒体仍被 `.gitignore` 排除）。

---

## 🎯 这个仓库解决什么问题

把一段人物采访，变成一篇经得起核验的深度报道。流程十步：

```
① 选题卡（读者问题/人物选择/初步判断/阅读收益/素材条件）
② 转写 → 素材卡（时间戳｜说话者｜原文｜转述｜限定条件）
③ 事实台账（闸门：填不出的人物字段，正文不能写）
④ 研究补证据（行业阶段/关键证据/对照/不确定性/暂定判断）
⑤ 骨架：主标题 + 导语 + 章节标题 + 每节一句新增认识 → 观点体检
⑥ 写正文 → 禁用词自检 → 加粗口径复核
⑦ 抽帧配图（先定用途 → 再找图 → 保 PNG 原帧 → 记图源与时间戳）
⑧ gzh_pipeline.py 一条命令 → 双 HTML + 质检报告 + 三档渲染
⑨ 微信编辑器实测：保存草稿 → 重开确认 → 手机预览
⑩ 发布后差异记录 → 回写成 SOP 新版本
```

v10 是人来做这十步；v9 不改任何一步的规则，只把其中可判定的部分变成机器闸门，交给九个 agent 执行（见下）。

---

## 🚦 六条铁律

> 全部由真实事故换来，不是参考建议。

**① 容器用 `<section>`，永远不用 `<div>`**
公众号编辑器底层 ProseMirror 白名单里没有 `div`，用 div 包的卡片/引用块/色块会被**整段吞掉**，只剩文字。详见 [`模板/公众号HTML硬规则.md`](01_SOP工作流/模板/公众号HTML硬规则.md)。

**② 事实台账是闸门，不是清单**
台账里查不到的人物字段，文章里不能出现；引号内容必须有时间戳（视频）或页码（文本）；每个具体数字必须有信源。

**③ 区分三件事：核实事实 / 受访者自述 / 作者判断**
对话行用姓名缩写归属，叙述默认作者声音，文末一次性声明；台账里编辑归纳单独占一行，不能写成"他说"。正文不逐段贴"本文归纳/作者判断"标签（v10）。

**④ 既有人物卡原样保护**
用户最新确认的卡片，文字、头像、布局、尺寸一律不动，除非明确授权改哪一部分。

**⑤ 预览版与导入片段同源生成**
两份 HTML 必须出自同一份正文；正式稿不得留 `【】` 占位符、本地路径、空 `src`。

**⑥ 本地检查 ≠ 发布验收**
没跑过编辑器保存与手机预览，只能标"本地完成，编辑器待验证"。

---

## 🤖 编排层（SOP v9）

v8 管「写得对不对」，v9 管「能不能无人值守地跑、跑歪了谁拦住」。九个 agent 串成状态机，
只在两处停下等人：选题（`triaged`）与发布（`awaiting_publish`）。

```bash
cd gongzhonghao

# 投素材（二选一：CLI / 往 inbox 放链接或转写稿）
python3 orchestrator/loop.py submit --url "https://www.youtube.com/watch?v=xxxx" --title "某人访谈"

# 推进一轮：默认只推一步；--steps N 连推，排障用
python3 orchestrator/loop.py tick --scan --provider deepseek

# 看状态与成本（--json 输出 {runs, usage, broken}）
python3 orchestrator/loop.py status

# 人工闸门放行；卡住时解封
python3 orchestrator/loop.py approve --run-id <id> --gate topic    # 选题闸门
python3 orchestrator/loop.py approve --run-id <id> --gate publish  # 发布闸门（不实际发布）
python3 orchestrator/loop.py resume --run-id <id>
```

**P0 加固（2026-11-09）**

| 机制 | 行为 |
|---|---|
| 判定码三值 | 质检报告写机器可读 `- 判定码：PASS\|FAIL\|INCOMPLETE`，退出码 0/1/3；`--skip-render`、无 Chrome、渲染无产出 = `INCOMPLETE` → `blocked`，**不再假通过** |
| 发布锁死 | `auto_approve.publish` 已删除，写 `true` 也不生效；`approve --gate publish` 是唯一出口，且只是把状态记为已发布，实际发布仍由人在编辑器完成 |
| 一 tick 一步 | `tick` 默认 `--steps 1`，失败点可定位、重跑幂等；同一工作区内并发 tick 用 `fcntl` 单实例锁互斥 |
| 状态可运维 | 时间戳统一 UTC；坏 `state.json` 在 `tick` 告警、`status` 退出码 1；`status --json` 返回 `{runs, usage, broken}` |
| 产物版本化 | writer / composer 只增不删（`{嘉宾}_人味版_vN.md`、`{嘉宾}_vN_排版预览.html`）；artifacts 存相对 `case_dir` 路径，云端绝对路径拉回本地可回退 |
| 闸门通知 | 配 `FEISHU_WEBHOOK` 后，等闸门/`blocked` 推一条摘要（5s 超时、12h 去重、失败只 warn） |
| 成本可见 | provider 的 `usage` 落到 run；`status` 汇总今日/本周 tokens，未上报如实标「未上报」 |

自检与验收：

```bash
python3 orchestrator/selftest.py                                   # 端到端 new → archived
python3 -m unittest discover -s orchestrator -p 'test_*.py' -t .   # 172 个单测
python3 orchestrator/verify_p0.py                                  # P0 独立验收：临时目录跑 36 项对抗场景
```

**云端**：[`gzh-loop.yml`](.github/workflows/gzh-loop.yml) 每 2 小时 tick（带 `--render`，产物与状态 commit 回写）；
[`gzh-approve.yml`](.github/workflows/gzh-approve.yml) 提供 Actions / Issue `/approve` 两种放行入口，
但需要 `GZH_DISPATCH_TOKEN`，且**未在真实仓库实测**。

**真实跑稿**：2026-11-09 用 Ilya Sutskever × Dwarkesh 访谈（96 分钟）跑通全链路，停在 `awaiting_publish`：
正文 2,918 字、30 处带时间戳引语、三档渲染 0 溢出、36,942 tokens；案例在 `14_伊利亚苏茨克维尔_案例/`（工作区根，非本仓库）。

细节见 [`orchestrator/README.md`](orchestrator/README.md) 与 [`SOP_v9_多智能体编排.md`](01_SOP工作流/SOP_v9_多智能体编排.md)。

---

## 🌙 夜间自动出稿（2026-11-21）

一条不需要盯屏幕的链路：**02:01 抓取 → 打分选 3 个候选 → 转写 1 期 → LLM 出 v0 底稿 → 08:30 飞书卡片推给你 → 你点一个按钮 → 会话里改成 v1 发布**。
发布这一步永远是人工的（`awaiting_publish` 闸门，代码里硬拒自动发布）。

| 环节 | 组件 | 说明 |
|---|---|---|
| 抓取 | `podwatch/podwatch.py poll` | 44 个播客 RSS；只入「待转写」队列，**不再顺手转写**（`defaults.auto_transcribe=false`） |
| 选片 | `nightly.py` | 新鲜度 + 关键词 + 来源分级 + 重播惩罚的启发式打分；同一期不会重复跑 |
| 转写 | `podwatch` 的 `gzh` 后端 → `GZH_STT_CMD` | 任意「音频文件 → `*.transcript.json`」的命令都能接（默认 `tools/stt/stt`，faster-whisper，CPU int8 约 5× 实时） |
| 出稿 | `orchestrator.oneshot` | 十阶段到 `awaiting_publish`；`--title` 可省，自动读转写旁的 `.meta.json` |
| 推送 | `feishu_bot.py push` | 群自定义机器人 webhook 推交互卡片（三候选 + 理由） |
| 回传 | `choice_server.py` | 卡片按钮 → `GET /c?t=<token>&i=N` → 写 `choice.json`，下一晚优先跑你选的 |
| 复盘 | `nightly.py review` | 每周扫当周 `*_导入片段.html` 生成复盘 md，`--clean --keep-days 14` 删旧件 |

```bash
# 干跑（只打分，不转写不出稿）
python3 -m nightly run --dry-run

# 真跑一期（跳过抓取，用现有队列；指定某一期）
python3 -m nightly run --skip-poll --pick <key> --provider openai-compat

# 看上一晚结果 / 周复盘
python3 -m nightly status
python3 -m nightly review --days 7
```

环境变量全部走 **env**，不写死在代码里：复制 [`.env.example`](.env.example) 成 `.env` 再填。
服务器上同一批键名放在 `/etc/gzh/env`（600，systemd `EnvironmentFile`），由
[`orchestrator/deploy/setup_ecs.sh`](orchestrator/deploy/setup_ecs.sh) 生成与补写。
**仓库里没有任何真实 key / webhook / cookie**。

部署（Mac 推到 ECS + 装 systemd 单元，一次性）：

```bash
sh orchestrator/deploy/deploy_ecs.sh    # 需要本机 workbench CLI 已登录，GZH_INSTANCE 指定实例
```

单元：`gzh-nightly.timer`(02:00) · `gzh-push.timer`(08:30) · `gzh-review.timer`(周日 09:00) · `gzh-choice.service`(8788) · `gzh-bot.service`(可选，需飞书应用)。

实测（2 vCPU / 3.6 GiB）：59 分钟单集 → 转写 14 min + 出稿 ~1 min，全程 30.7 min；18 次夜间跑批稳定。

---

## 🧰 模板

| 文件 | 行数 | 用途 |
|---|---|---|
| `模板/公众号HTML硬规则.md` | 118 | 白名单/黑名单，**动手前必读** |
| `模板/禁用词表.md` | 119 | 去 AI 味：高危词 + 句式黑名单 + **何时可保留** |
| `模板/人味检查表.md` | 207 | 命中之后改不改：五条优先级 + 17 条模式（基于 humanizer-zh） |
| `模板/事实台账.md` | 109 | 九列台账，填完才能写正文 |
| `模板/字幕定位抽帧.md` | 223 | 抽帧细则 + 锐度检测（拉普拉斯方差 < 50 判糊） |
| `模板/人物简介卡_文字版.html` | 40 | **现行**简介卡（v10）：标签式 3—5 行，几人几张 |
| `模板/人物卡片_壹伴.html` | 94 | 历史履历卡（含 `div`，新稿不用） |
| `模板/公众号排版_v4_壹伴.html` | 45 | **现行**预览版骨架 |
| `模板/公众号排版_v4_导入片段.html` | 45 | **现行**导入片段骨架 |

> 模板目录共 12 个文件；`v2` / `v3` 模板仅为历史对照保留，新稿一律用 v4 排版 + 文字版简介卡。

---

## ⚙️ 流水线

```bash
cd gongzhonghao/流水线

python3 gzh_pipeline.py \
  --md 某篇_人味版.md \
  --card 人物卡_XX.html \
  --name 某篇_v9 \
  --series 人物与科技 --episode 14 \
  --outdir /Users/lnz/Desktop/公众号/14_XX_案例/
```

产出四件套：`_排版预览.html`、`_导入片段.html`、`_质检报告.md`、`_渲染_{375,390,430}.png`。

**判定契约：三值 + 退出码**

| 判定码 | 退出码 | 含义 |
|---|---|---|
| `PASS` | 0 | 质检门禁全过，**且**三档渲染验收真的跑过 |
| `FAIL` | 1 | 硬规则/禁用词/同源/图片违规，或任一视口溢出、图片比例误差 > 3% |
| `INCOMPLETE` | 3 | 渲染没跑成（`--skip-render` / 无 Chrome / 无 viewport 产出）：**未完成 ≠ 通过**，不得放行 |

门禁项：HTML 硬规则（div / id / class / script / position / 渐变 / flex / 占位符 / `@media` / 页面外壳）/
禁用词分级（高危 FAIL、语境词 WARN）/ 预览与导入同源 / `【】` 残留 / 图片合规（本地路径、空 `src`、缺 `alt`）；
渲染验收测三档手机视口（375/390/430）横向溢出与图片比例（natural 比例误差 > 3% 判 FAIL）。受限环境里 headless Chrome 自动加
`--no-sandbox` + 独立 `--user-data-dir`（只影响启动方式，不改判定逻辑）。

细节（md 写作语法、参数表、字幕长图命令）见 [`流水线/README_使用说明.md`](流水线/README_使用说明.md)。

---

## 🎨 视觉规范（SOP v10 §07 摘要）

| 元素 | 规格 |
|---|---|
| 正文 | 16px，行高 1.8，`#333333`，段后 18px |
| 页面 | 白底，预览最大宽 680px，左右留白 20px |
| 章节编号 | 24px 加粗 `#9a7841`，独立一行，行高 1.2 |
| 章节标题 | 20px 加粗 `#222222`，最多两行 |
| 原话引用 | 浅米灰 `#f7f6f3` 底 + 左侧 3px 品牌金 `#9a7841` |
| 图注/来源 | 13px `#666666`，左对齐 |
| 强调 | 加粗为主；主题型可少量橙红 `#f05a28` 标关键转折 |

主动避开 `div` / `class` / `id` / `flex` / `grid` / `position` / 渐变 / 脚本 / 媒体查询——保守导出策略，不依赖平台过滤的宽容度。

---

## ✍️ 写作风格（以 SOP v10 §05 与 `模板/禁用词表.md` 为准）

**呈现（v10）** 电影解说式：作者思考直接写进叙述，原话以对话嵌入。
开头人物简介卡（几人几张，采访者也要有）；对话行直标姓名缩写（`> IS：……`），
英文原话中英对照（`> EN|……`），时间戳置底；文末一次性声明
「除署名缩写的对话原话外，本文的叙述、归纳与判断均为作者观点。」

**开头** 第一句给反常点，不铺陈背景。
✅ "他说 AI 不会平均地消灭工作，但证据来自最容易被消灭的那批人。"
❌ "先从最近最热的话题说起" / "一场 32 分钟的深度访谈，拆解了……"

**小标题** 必须是判断，不是话题。主题型/观点型过四关：
可复述（一句话转述）· 有对手（挑战了什么共识）· 有代价（为真则谁受影响）· 有证据（哪个数字/原话）。
✅ "能力在飙升，价格还没动"
❌ "AI 对经济的影响分析"

**叙述** 用"我们"拉读者入伙，不用"我听到这的时候愣了一下"（自赏）；
禁止"采访者问/他回答/本文归纳：/作者判断："的机械视角切换（v10）。

**结尾** 回答或深化开头问题，留一个有依据的未解问题，不强行升华。

**篇幅** 2500—4000 字（编辑预算，不是硬指标；中英对照按中文计）；短素材不扩写，复杂材料可更长。

**不再要求**（v8 已取消）：每节必带"我不同意的地方"、固定四章、关键词冒号标题、完整生平卡、固定字数下限、每篇给行动建议。

---

## ✅ 发稿前检查清单

**事实（填不出就删）**
- [ ] 台账每个字段有信源
- [ ] 代词按实际指代检查（多人物文章不强制一种）
- [ ] 引号内容都有时间戳/页码
- [ ] 所有具体数字有出处（含单位、币种、口径、时间区间）
- [ ] 编辑归纳进台账；正文无逐段标签，文末有归属声明（v10）
- [ ] 英文对话原话中英对照，缩写与简介卡对照一致（v10）

**观点（v8 新增）**
- [ ] 只读标题 + 章节标题 + 加粗句，能复述一句主张
- [ ] 多数章节有独立判断，不是"背景/现状/建议"
- [ ] 选题卡里最强的 2—3 条材料已进正文，且不埋在末节

**去 AI 味**
- [ ] 全文无禁用词（`grep` 跑一遍 `模板/禁用词表.md`）
- [ ] 加粗只落在判断/结论/机制句，每段不超过一处

**排版**
- [ ] 人物卡与用户最新确认版一致（未授权部分零改动）
- [ ] 每张图标注"截自原视频 + 时间点"或图源
- [ ] 375 / 390 / 430px 无横向溢出，图片比例正常
- [ ] 质检报告判定码为 `PASS`（`INCOMPLETE` = 渲染没跑，不算通过）
- [ ] 预览与导入片段正文一致
- [ ] 微信编辑器保存后重开不丢样式

---

## 🗂 版本谱系

| 文件 | 行数 | 状态 |
|---|---|---|
| `_备份/...SOP_v5_原始版_2026-10-15.md` | 459 | 归档 |
| `...SOP_v6.md` | 258 | 历史 |
| `历史版本/...SOP_v7_发布复盘前_2026-10-26.md` | 299 | 归档（复盘前快照） |
| `...SOP_v7.md` | 334 | 历史 |
| `...SOP_v8.md` | 401 | 历史 |
| `SOP_v9_多智能体编排.md` | 260 | **现行（编排规则，2026-11-09 P0 加固）** |
| `...SOP_v10.md` | 430 | **现行（内容规则，2026-11-10 对话解说式）** |

每版都由一篇具体稿件的事故驱动：v2 贝莱德篇（代词错乱、履历幻觉）→ v4 李飞飞篇（`div` 被吞）→ v7 马斯克篇（发布稿与本地稿不一致）→ v8 布林约尔松篇（信息全但主张不尖）→ v9（内容规则不动，把 v8 的判据变成机器闸门）→ v10 伊利亚篇（视角反复切换、英文原话只给译文）。

---

## 🔧 维护约定

- 禁用词表与 `流水线/gzh_pipeline.py` 的 `FORBIDDEN_WORDS` / `FORBIDDEN_CONTEXT` **同步维护**。
- 硬规则与 `流水线/gzh_pipeline.py` 的 `HARD_GATES` **同步维护**。
- **判定语义唯一来源是 `orchestrator/verdict.py`**（流水线与 gatekeeper 共用）；不要再在别处手写「通过/未通过」正则。
- 视觉参数与 SOP v10 §07 一致；改版式先改 `VISUAL` 字典，再跑回归。
- **发布永不自动**：不要恢复 `auto_approve.publish`，这是本项目唯一的红线。
- 案例归属：本地跑稿落工作区根 `NN_XX_案例/`（不进 git，含媒体）；云端/编排写 `cases/` 并随 tick 回写，不要手工往里塞音视频（`.gitignore` 兜底）。案例索引登记在工作区根目录 `README.md`。
- 改动产品代码后至少跑：`python3 -m unittest discover -s orchestrator -p 'test_*.py' -t .` + `python3 orchestrator/selftest.py` + `python3 orchestrator/verify_p0.py`。
- 本 README 只做导航；规则变更改 SOP，不要只改这里。
