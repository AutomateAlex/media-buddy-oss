# 前端样式模块地图（改样式前先读这页）

原来全站样式是一个 7921 行的 `styles.css` 巨型单文件。2026-07-18 按页面/模块拆成了这些小文件。
纯手写 CSS，没有 Tailwind / CSS-Modules。**改样式先查下表 → 去对应文件改。**

## 模块地图

| 文件 | 管什么 |
|---|---|
| `base.css` | 主题变量（`:root` / `[data-theme]`）+ 全局 reset。**所有 `var(--…)` 的来源** |
| `layout.css` | 整体骨架：app-shell、main-col、侧边栏 sidebar |
| `components.css` | 跨页面共用组件：用户菜单、关于弹窗、页头 page-header、内容卡片、状态条、tier 卡片、保存栏，以及**全局按钮 `.btn-primary/.btn-secondary/.btn-danger/.btn-icon` 的正典定义** |
| `projects.css` | 项目列表页、项目详情、存储保留、pipeline 进度、`.hint` 提示、通用页兜底 |
| `studio-legacy.css` | Studio 早期子面板：series/batch/timeline、编导对话、diagnostics、音色选择器、素材库、音频库、chat 面板 |
| `responsive-shell.css` | 平板/手机 **共享**的壳与侧栏抽屉（汉堡菜单那套） |
| `account.css` | 账户页 |
| `billing.css` | 充值页 |
| `studio.css` | Studio 现代模块化重设计（home / workbench / cockpit / voice）。**最大** |
| `channels.css` | 频道页、频道卡片、删除弹窗、建频道模板、tabs、项目条、intel/console（批量面板） |
| `overrides.css` | 一小撮"必须最后声明才能赢层叠"的响应式覆盖（见下） |
| `misc.css` | 杂项：充值不足弹窗、project-id chip、focus-params、admin suspect zone、mb-switch、帮助/FAQ、AI-loading 动画 + keyframes |

## ⚠️ 两条顺序铁律（在 `main.tsx` 里维护）

Vite 把这些文件**按 `main.tsx` 里的 import 顺序**拼成一个 CSS bundle。相同优先级的规则，**谁在后面谁赢**。所以顺序是"load-bearing"，不能随便调：

1. **`base.css` 必须最先** —— 别的文件全在用 `var(--bg-1)` 这类变量，变量得先定义。
2. **`components.css` 必须排在 `studio-legacy.css` 之前** —— 全局 `.btn-*` 在 `components.css` 定义一版（正典），又在 `studio-legacy.css` 里被**重复覆盖**一版（历史遗留，同优先级、后者生效）。两者顺序一颠倒，全站按钮的 hover/disabled 样式就变了。

其余文件各自带着自己的 `@media` 手机断点，段内自洽，顺序不敏感。

## 新增一个页面模块怎么办

1. 建 `styles/<页面名>.css`。
2. 在 `main.tsx` 的 import 列表里**按它在页面层级中的位置**插一行 `import './styles/<页面名>.css'`（一般放在其他页面文件旁边、`overrides.css` 之前）。
3. 若新样式要覆盖某个共用组件（同优先级取胜），确保它排在被覆盖者之后。

## 改完怎么验证没搞坏别的

这次拆分是"行为保持型重构"：拆完构建产物跟拆分前**逐字节相同**（`cmp` 证过）。以后你改样式后想确认没误伤别处：
```
cd src/frontend && npx vite build
# 对比改动前后的 dist/renderer/assets/index-*.css
```
再用手机宽度(390)扫一眼改动的页面有没有横向溢出。

## 调试提示

- 手机布局调试：390px 视口 + 遍历元素右边界（`scrollWidth` 会骗你，内容可能被祖先裁掉而不产生横滚）。
- grid 手机溢出通用元凶：`grid-template-columns: 1fr` 其实是 `minmax(auto,1fr)` 会被内容撑破 → 用 `minmax(0, 1fr)`。
