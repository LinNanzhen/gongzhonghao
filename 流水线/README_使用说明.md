# 公众号一键流水线（对应 SOP v8）

把「正文 → 双 HTML → 质检 → 三档渲染验收」固化成一条命令，替代手工拼 gen 脚本、手工跑正则、手工截图。

## 一条命令产出什么

| 产物 | 说明 |
|---|---|
| `{name}_排版预览.html` | 浏览器审阅完整页（含 `<head>`/`<body>`） |
| `{name}_导入片段.html` | 微信编辑器导入片段（无外壳、内联样式、section 结构） |
| `{name}_质检报告.md` | 硬规则门禁、禁用词、同源性、占位符、图片合规、三档渲染结果 |
| `{name}_渲染_{375|390|430}.png` | 三档手机视口整页截图 |

## 用法

```bash
cd "/Users/lnz/Desktop/公众号/gongzhonghao/流水线"

# 方式一（推荐）：Markdown 人味版正文源
python3 gzh_pipeline.py \
  --md 布林约尔松AI就业观_人味版.md \
  --card 人物卡_XX.html \
  --name 布林约尔松AI就业观_v9 \
  --series 人物与科技 --episode 13 \
  --prev "马斯克：一个担心 AI 毁灭人类的人，为什么还在全力造 AI？" \
  --outdir /Users/lnz/Desktop/公众号/14_XX_案例/

# 方式二：已有 HTML 正文片段（已含内联样式）
python3 gzh_pipeline.py \
  --body-html body_XX.html --name XX_v9 --title "页面标题" --outdir ./

# 方式三：完整联网校验图片外链（较慢，默认关闭）
python3 gzh_pipeline.py --md x.md --name x --check-links
```

参数说明：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--md` / `--body-html` | 二选一 | 正文源：Markdown 人味版 / HTML 片段 |
| `--name` | 是 | 产物名前缀（会带 _排版预览/_导入片段/…） |
| `--title` | 否 | 预览页 `<title>`；md 模式默认取正文 H1 |
| `--card` | 否 | 人物卡 HTML 文件（用户确认版，**原样插入**，不自动改写） |
| `--series` / `--episode` | 否 | 生成文首「系列 · 第 N 期」品牌行 |
| `--prev` | 否 | 文末「上一篇」导航 |
| `--outdir` | 否 | 输出目录，默认当前目录 |
| `--check-links` | 否 | 联网 HEAD 校验图片外链 |
| `--skip-render` | 否 | 跳过 Chrome 渲染验收（仅质检，快） |

## 写稿约定（md 模式）

流水线解析固定语法，写稿时按下面格式即可：

```markdown
# 主标题（作为页面 title，不进正文）

## 导语
导语段落……

## 01｜章节标题
正文段落，**加粗判断句**，具体数字 16%。

> “可定位的原话引用。”
> —— 受访者，原视频 00:01:16 [1]

> 中文观点（导读条：左侧竖杠 + 金色衬底，观点加粗显示）
> EN|English original quote（带 EN| 前缀的行渲染为灰色斜体英文原话，用于中英对照）
> —— 受访者

![图注：原视频 08:20 截帧，含原话字幕](https://图片链接)

## 02｜章节标题
……

## 结尾
结尾段落……

---

## 来源与说明
[1] 来源条目
[2] 来源条目

## 内部附录（不进入正文）
### 5 个备选标题
1. ……（附录到此为止，全部不进正文）
```

规则要点：

1. **章节**：`## NN｜标题`（两位编号 + 全角/半角竖线），自动生成「金字号 + 深色标题」章节块，编号自动从 01 连续。
2. **人物卡**：`--card` 传入的文件**原样插入**到导语之后、章节 01 之前（SOP v8 硬规则）。人物卡不受任何样式改写。
3. **引用 / 导读条**：`> ` 开头连续行 → 米灰底 + 金线引用框；末行以「——」开头自动降为 13px 署名行。**中英对照导读条**：引用块内某行以 `EN|`（或 `EN:`）开头，自动升级为「左侧 4px 金竖杠 + 金色衬底」样式，中文观点加粗、英文原话灰色斜体、署名行置底——适合展示受访者原始英文观点。
4. **图片**：`![图注](url)` 独立行 → 图片块 + 图注；图注会同时写入 `alt`。
5. **来源区**：`## 来源与说明`（或含「来源」二字）之后的段落 → 13px 来源样式。
6. **附录**：`## 内部附录`（或含「不进入正文」）之后的内容**全部截断**。
7. **加粗**：`**文字**` → `<strong>`，判断/结论句用。
8. **写作说明行不要用 `> ` 前缀**：`>` 会被当成正文引用框。正文源的说明文字请用 `<!-- 注释 -->` 或不写。
9. **导语/结尾标题不进正文**：`## 导语`、`## 结尾` 本身不输出标题，只作为分节标记。

## 质检门禁（自动）

| 门禁 | 行为 |
|---|---|
| HTML 硬规则 8 项（div/id/class/script/position/渐变/flex/占位符 + 外壳检测） | 命中即 FAIL |
| 禁用词高危词（赋能/抓手/闭环/……） | 命中即 FAIL |
| 禁用词语境词（首先/最后/心智/无疑/显然） | 命中即 WARN（人工按语境判断） |
| 预览/导入同源 | 校验两份文件由同一 BODY 生成 |
| 【】占位符残留 | 命中即 FAIL |
| 图片本地路径 / 空 src / 缺 alt | 命中即 FAIL |
| 图片外链可达（--check-links） | 可选 |

任一 FAIL 会以退出码 1 结束，报告写入 `{name}_质检报告.md`，不要带 FAIL 放行。

## 渲染验收（自动）

用本机 Chrome headless 在 **375 / 390 / 430** 三档视口：

- 横向溢出检测：任何元素 `scrollWidth > clientWidth + 1` 即 FAIL（列出标签与宽度）
- 图片比例检测：`naturalWidth/naturalHeight` 与 CSS 显示比例误差 >3% 或未加载即 FAIL
- 整页截图：按实际页高导出 PNG（默认 1x；`--force-device-scale-factor=2` 可出 2x 长图，SOP 要求长图 2x）

渲染依赖系统 Chrome（自动探测 `/Applications/Google Chrome.app` 等路径）。找不到 Chrome 时跳过渲染并在报告中注明，不影响质检。

## 字幕长图一键命令（gzh_subtitle.py，集成 native-subtitle-quote-image skill）

访谈类文章需要「视频真实帧 + 原生字幕」社交长图时使用。底层渲染脚本已本地化在
`subtitle_skill/native_subtitle_stitch.py`（skill 仓库
`chengyi-ai/native-subtitle-quote-image`，本机无法 git clone，脚本由 web_fetch 全文
落地），封装命令为 `gzh_subtitle.py`：

```bash
cd "/Users/lnz/Desktop/公众号/gongzhonghao/流水线"

# ① 半自动选句定位：从转写稿按关键词定位英文原话时间点，输出候选 manifest
python3 gzh_subtitle.py pick transcript.json \
  --quote "金丝雀已启动::it's already happening|already happening in our Canaries" \
  --quote "编码正中靶心::coding is obviously dead center" \
  --out manifest_candidate.json

# ② 渲染 3:4 社交长图（每卡 = 主图 + 字幕条，1440x1920 + 总览图）
python3 gzh_subtitle.py render 视频.mp4 \
  --manifest manifest_candidate.json \
  --out-dir /Users/lnz/Desktop/公众号/案例目录/字幕长图 \
  --overwrite
```

要点（踩坑记录，勿凭记忆调参）：

| 项 | 约定 |
|---|---|
| 模式 | **原生字幕模式**：只裁画面已烧录字幕，不 OCR 不重绘；本视频为黄色中英双语烧录字幕 |
| `--band-top / --band-bottom` | **必须 0.84 / 0.98**：默认 0.78/0.96 会截掉英文字幕底行（英文底行约在画面 0.96） |
| 选句关键词 | 需贴近转写原文（含逗号/撇号，如 `it's already happening`）；长句跨段要拆关键词 |
| 时间点间隔 | 烧录字幕按句显示，相邻时间点间隔 **≥3s** 才取不同句（pick 已按 3s 去重） |
| 每卡时间点 | 每图 1 主图 + 最多 6 字幕条；manifest 结构 `{"images":[{"title","times"}]}` |
| 重复画面 | 静态封面/重复字幕条会中止，确认无误再 `--allow-duplicate-frames`（不建议强过） |
| 副产物 | `原生字幕时间点.json` + `final_contact_sheet.jpg` 总览图 |
| 续跑 | `--resume` 跳过已渲染且有效的卡 |

流程：`transcript.json → pick 半自动选点 → band/sample 目检微调 → render → 逐张目检`
（pick 只做定位，画面/字幕完整性仍需目检后人工微调 times）。

## 生成脚本 `gen_vN.py` / `build_html.py` 的定位与弃用路径

案例目录里仍能看到 `gen_v6.py`、`gen_v7.py`、`gen_v8.py`、`build_html.py` 这类脚本：它们把整段 `BODY` 硬编码在 Python 里，再拼出预览页。这套做法与 SOP v8 §07「脚本作为单一内容源」并不冲突，但**内容源是脚本而不是正文**，一旦改版式就要同时改脚本和正文，容易漂移。

| 场景 | 做法 |
|---|---|
| **新稿** | 只走 `gzh_pipeline.py`，内容源是 `_人味版.md`。不再新写 `gen_vN.py` |
| **历史稿的版本快照** | `gen_vN.py` 原样保留，作为"这一版是怎么生成的"的可复现记录，**不再修改** |
| **需要再改历史稿** | 不要改旧脚本另存 `gen_vN+1.py`；把脚本里的 `BODY` 导出成 `body_*.html`（或还原为 md），改用 `--body-html` / `--md` 跑流水线，产出新的 `_vN+1_*` 文件，旧文件并存 |

迁移时用同源对比验收：新产物的正文文本与旧稿逐段 diff，只允许排版差异，不允许内容差异；确认后再把新产物标为当前版。视觉参数以 `gzh_pipeline.py` 的 `VISUAL` 字典为唯一来源，旧脚本里的字号、颜色不再具备参考价值。

## 与旧流程的对比

| 环节 | 旧流程 | 流水线 |
|---|---|---|
| 生成双 HTML | 每篇手写 gen_vN.py 硬编码 BODY | 一条命令，md/html 双输入 |
| 硬规则门禁 | 人工 grep / 手工跑断言 | 自动 8+ 项 |
| 禁用词 | 手工对照词表 | 自动分级（FAIL/WARN） |
| 三档渲染 | 手工 playwright/Chrome 截图 + 人工看 | 自动测量溢出/图片比例 + 截图 |
| 质检报告 | 无统一产物 | 自动生成 md 报告 |

## 维护注意

- 禁用词表与 `模板/禁用词表.md` 同步维护（脚本内 `FORBIDDEN_WORDS` / `FORBIDDEN_CONTEXT`）。
- 硬规则与 `模板/公众号HTML硬规则.md` 同步维护（脚本内 `HARD_GATES`）。
- 视觉参数与 SOP v8 §07 一致（脚本内 `VISUAL` 字典）；改版式先改这里，再重跑回归。
- 图片上传到微信后，正式稿仍按 SOP 要求用平台链接；base64 仅在当前导入路径实测通过时选用。
