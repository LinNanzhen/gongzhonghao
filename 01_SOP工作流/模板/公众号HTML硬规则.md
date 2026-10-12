# 公众号 HTML 排版硬规则

> 来源：2026-10-11 李飞飞案例排障实测 + 公众号编辑器 ProseMirror 白名单逆向分析
> 适用范围：本项目所有公众号排版 HTML 产出

---

## 🚨 第一条：用 `<section>`，永远不用 `<div>`

公众号新版编辑器底层是 **ProseMirror**，标签白名单里**没有 `div`**。
用 `div` 包着的卡片、引用块、色块，**粘贴后整个 div 连同背景/圆角/边框被整段吞掉，只剩文字**。

```html
❌ <div style="background:#faf8f5;border-radius:8px;padding:16px;">
✅ <section style="background:#faf8f5;border-radius:8px;padding:16px;">
```

这是「代码粘进去看着好好的，一保存就全丢了」的头号原因。

---

## ✅ 白名单内·放心用

| 类别 | 可用 |
|---|---|
| 结构 | `section` `p` `span` `br` `h1`-`h3` `strong` `b` `em` `i` `u` `s` |
| 列表 | `ul` `ol` `li`（符号会被强制改样式，别自定义） |
| 引用 | `blockquote` |
| 表格 | `table` `tr` `td`（简单表格可以，跨行列表现差） |
| 图片 | `img`（`src` / `style`，**iOS 必须写死 width/height**） |
| 链接 | `a`（`href` / `target`） |
| 文字样式 | `font-size` `font-family` `font-weight` `color` `letter-spacing` `line-height` `text-align` `text-decoration` `text-indent` |
| 盒模型 | `margin` `padding` `border` `border-radius` `background` `width` `height` |

---

## ❌ 会被过滤或整行删除

| 禁用 | 后果 |
|---|---|
| `<div>` | **整段吞掉**（头号元凶） |
| `id` 属性 | 所有元素连同 id 被删除 |
| `class` 属性 | 无效 |
| `<script>` / `onclick` / `onerror` | 全部清空 |
| `position: absolute/fixed/relative` | **整行删除** |
| `animation` `transition` `@keyframes` `@media` | 无法承载 |
| `:hover` `::before` `::after` | 无选择器机制 |
| `linear-gradient` | 降级为透明 |
| `box-shadow` / `transform` / `display:flex` | 丢弃 |
| `position:fixed` + 高 `z-index` 浮层 | 全屏遮罩被清 |

---

## 平台怪癖

| 现象 | 说明 | 对策 |
|---|---|---|
| 连续空格被合并 | 代码缩进全丢 | 用 `&nbsp;` |
| 代码块内 `\n` 不换行 | — | 显式 `<br>` |
| SVG iOS 不显示 | 不写尺寸就不渲染 | 必须写 `width` `height` |
| 图片总像素 > 600 万被拒 | — | 宽 ≤ 1000px |
| GIF > 300 帧或 10MB 被拒 | — | 压缩 |
| 外链包裹 | 点击弹「即将离开微信」 | 可转文末脚注 |
| 保存时二次清洗 | 部分 style 会掉 | 手机预览确认 |

---

## 📦 图片处理（唯一正确方式）

**本地路径 `src="截图/x.jpg"` 在公众号里 100% 裂图。** 两种可行方案：

**方案 1 · base64 内嵌（推荐，配合直接粘贴）**
- 压缩到宽 ≤ 1000px、JPEG quality 80，单图 ≤ 100KB
- base64 后体积 ×1.33
- 优点：跟着剪贴板走，不需要先上传
- 适合：直接 `Cmd+V` 粘进正文区

**方案 2 · 先上传拿 mmbiz 链接（适合源码模式）**
- 编辑器点「图片」→ 本地上传
- 复制生成的 `mmbiz.qpic.cn` 链接
- 跑 `换图助手_替换图片链接.py` 批量替换占位符

---

## 📤 交付形态：三选一

| 形态 | 怎么用 | 何时选 |
|---|---|---|
| **section 版 + 图内嵌** | Chrome 打开 → `Cmd+A` `Cmd+C` → 正文区 `Cmd+V` | **默认首选**，最快 |
| **纯净片段 + 占位符** | 源码模式 `<>` 粘贴 + 换图助手 | 要精修样式 |
| **分段长图** | 逐张上传 | 重要发刊、跨平台保真 |

**永远不要交付整页 HTML**（带 `<!DOCTYPE>` / `<html>` / `<head>` / `<body>`），外壳会让内容被整体丢弃。

---

## ✅ 出厂质检门禁（写文件前必须跑）

```python
assert 0 == len(re.findall(r'<div\b', html)), "残留 div"
assert 0 == len(re.findall(r'\bid="', html)),        "残留 id"
assert 0 == len(re.findall(r'class="', html)),        "残留 class"
assert 0 == len(re.findall(r'<script', html)),        "残留 script"
assert 0 == len(re.findall(r'position:', html)),      "残留 position"
assert 0 == len(re.findall(r'linear-gradient', html)),"残留渐变"
assert 0 == len(re.findall(r'display:flex', html)),   "残留 flex"
assert 0 == len(re.findall(r'REPLACE_IMG_', html)),   "未换图占位符"
```

任一不过直接报错退出，不要放行。

---

## 参考实现

`03_李飞飞AI两类人_案例/` 下：
- `公众号排版_公众号兼容版.html` — 标准形态（section + base64）
- `导入指南_公众号源码模式.md` — 三条路线
- `换图助手_替换图片链接.py` — 占位符批量替换