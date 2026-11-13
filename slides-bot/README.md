# slides-bot

通过 Telegram Bot 的 `/slides` 指令自动发布、编译和更新静态幻灯片目录页。

## 功能

- 支持发送 **`.html` / `.htm`**、**Marp `.md` / `.markdown`**（自动调用 `@marp-team/marp-cli` 编译为 HTML），以及 **包含图片资源的 `.zip`**（自动解包并校验路径安全）。
- 自动提取标题、开头 Emoji 图标、副标题与真实页数，并支持通过参数覆写元数据。
- 自动维护目录索引文件 `decks.json`，并以注释标记原子更新 `index.html` 中的计数与 `.collection` 卡片列表。
- 支持在 Telegram 内通过交互按钮查看列表、切换卡片封面配色（`forest`、`tram`、`sand`、`sky`、`slate`）、编辑信息及二次确认删除。

## 目录结构

```
slides-bot/
├── slides_manager.py      # 幻灯片解析、Marp 编译与 index.html 渲染模块
├── slides_bot.py          # 独立运行的 Telegram Bot（仅依赖 Python 标准库）
├── slides-bot.env.example # 环境变量模板
├── slides-bot.service     # systemd 服务示例
└── README.md
```

## 部署说明

1. 若需要服务端编译 `.md` 源稿，先安装 Marp CLI：

   ```bash
   npm install -g @marp-team/marp-cli
   ```

2. 准备环境变量文件（参考 `slides-bot.env.example`）：

   - `TG_BOT_TOKEN`：Telegram Bot Token
   - `TG_OWNER_ID`：管理员的 Telegram 数字用户 ID（仅私聊管理员可用）
   - `SLIDES_ROOT`：静态幻灯片站点根目录（需包含带 `.count` 与 `.collection` 的 `index.html`）
   - `SLIDES_BASE_URL`：幻灯片站点公开访问前缀（如 `https://slides.example.com`）
   - `MARP_BIN`：`marp` 可执行文件路径或命令名（默认 `marp`）

3. 运行机器人或挂载 systemd 服务：

   ```bash
   set -a && source slides-bot.env && set +a
   python3 slides_bot.py
   ```

## 使用示例

- `/slides`：列出已收录幻灯片并打开交互管理面板
- 发送 `.html` / `.md` / `.zip` 时附带说明 `/slides`，或回复含文件的消息发送 `/slides`：
  - `/slides`（全自动提取 slug、标题与页数）
  - `/slides my-talk | 标题 | 分类 | 简介`
  - `/slides slug=my-talk title="主题分享" cat=技术 sub=副标题 icon=🤖 theme=tram`
- `/slides edit <slug> title=新标题 cat=新分类`：更新已有卡片元数据
- `/slides del <slug>`：删除演示（需按钮二次确认）
- `/slides rebuild`：根据 `decks.json` 重新渲染 `index.html`
