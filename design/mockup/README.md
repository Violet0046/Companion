# App 设计稿（高保真原型）

这个目录是**设计稿的唯一真源**。改设计 → 改 `build.py` → 重新生成 HTML → 重新出图。

```bash
python design/mockup/build.py     # 由 build.py 生成 6 个界面 HTML + 总览
python design/mockup/shoot.py     # 用 Chrome 截图（CDP 精确控制视口）
python design/mockup/shoot.py 03  # 只重出某一屏
```

产物在 `screenshots/`。

---

## 为什么是 HTML/CSS 而不是图片

- **像素级准确**：出来的图就是浏览器真实渲染的，不会有"设计图和实现对不上"
- **可以直接变成代码**：`base.css` 里的 class 可以原样搬进 `src/companion_ever/web/`
- **改一处，全屏同步**：`build.py` 改一行 → 重新出图

## 截图工具的坑（已解决）

`chrome --headless --screenshot --window-size=402,874` **在 Windows 上不可靠**：
Chrome 有最小窗口宽度（实测 ~490px），`--window-size` 给小了不生效，截出来的图右边会被裁掉。

所以 `shoot.py` 走 **Chrome DevTools Protocol**：
`Emulation.setDeviceMetricsOverride` 精确设定视口（402×874 @2x），和真机一致。
另外注意 DevTools 的 WS 连接**不能带 Origin 头**（`websockets.connect(..., origin=None)`）。

---

## 信息架构

底部四个 Tab（对齐微信的手感，但内容是这个产品自己的）：

```
┌─ 消息 ────┬─ 通讯录 ──┬─ 记忆 ────┬─ 我 ────┐
│ 聊天列表   │  角色列表  │ 她的记忆库 │  设置   │
└───────────┴───────────┴───────────┴────────┘
                     ↓ 点进去
              会话页 ←→ 角色资料页
```

| 屏 | 文件 | 说明 |
|---|---|---|
| 消息 | `01-messages.html` | 每个角色一行：头像 + 名字 + 最后一句 + 时间 + 未读角标。顶部搜索，右上「＋」新建 |
| 通讯录 | `02-contacts.html` | 上半「角色」、下半「功能」（新建角色 / 群聊） |
| 她的记忆 | `03-memory.html` | 统计条 + 类型筛选 + 记忆卡片（带重要度条）。**这是本产品的特色，不是抄微信** |
| 我 | `04-me.html` | 个人信息 + 设置项（思考过程开关、服务器地址、结束对话） |
| 会话 | `05-conversation.html` | 现有聊天界面：思考折叠块、「回到最新」悬浮按钮、Markdown 渲染 |
| 角色资料 | `06-profile.html` | 大头像 + 设定 + 统计 + 入口（她的记忆 / 聊天记录 / 关系状态）+ 结束对话 |

---

## 设计规范

全部沿用 App 现有的暗色暖调主题（`src/companion_ever/web/styles.css` 的 `:root`）：

| 变量 | 值 | 用途 |
|---|---|---|
| `--bg` | `#14100f` | 页面底色 |
| `--bg-soft` | `#1c1716` | 分组卡片底 |
| `--surface` | `#262020` | 气泡 / 搜索框 |
| `--surface-2` | `#322b2a` | 按钮 / 进度条槽 |
| `--text` | `#f2e9e6` | 正文 |
| `--text-dim` | `#a29692` | 次要正文 |
| `--text-muted` | `#857a77` | 辅助信息 |
| `--accent` | `#e8927c` | 强调色（主角色头像、选中态、重要度条） |
| `--bubble-me` | `#7a4a3d` | 我的气泡 |
| `--line` | `#ffffff14` | 分隔线 |

关键尺寸：导航栏 48px、状态栏 46px、底部 Tab 58px、列表行头像 46px、圆角 18px。

---

## 图 → 代码：落地映射

### 纯前端改动（不动后端）

| 屏 | 需要什么 |
|---|---|
| 消息 | 新增 Tab 栏 + 聊天列表页；数据来自 `/api/persona/{u}/{p}/history`（取每个角色的最后一条） |
| 通讯录 | 角色列表来自 `/api/users`（已返回 `personas` 数组）；「新建角色」需要新接口 |
| 她的记忆 | 数据来自 `/api/persona/{u}/{p}/notes`（已有）；重要度字段已有 |
| 我 | 把现有抽屉里的内容拆出来变成独立页；「服务器地址」是新增的本地配置 |
| 会话 | **已有**，只加顶栏角色头像入口 |
| 角色资料 | 统计数字来自 `/api/persona/{u}/{p}`（已有 `note_count` / `message_count` / `session_count`） |

### 需要后端加的东西（很少）

1. `POST /api/persona/{u}/personas` —— 新建角色（CLI 的 `setup` 只能建用户+第一个角色）
2. 每个角色的"最后一条消息" —— 可以在前端拿（每个角色拉一次 `/history?limit=1`），也可以后端加一个聚合接口
3. `GET /api/persona/{u}/{p}/conversation` —— 角色资料页需要的汇总

### 打包成 App 前还缺的（前端侧）

1. **服务器地址可配置** —— 现在请求都是 `location.host` 相对路径
2. **首次启动引导** —— 现在必须先在电脑上跑 `companion-cli setup`
3. **CORS** —— App 的源是 `capacitor://localhost`，跨域要放行

---

## 还没设计的

- **群聊页**（多角色同房间，谁该说话怎么调度）—— 需求还没定，暂缓
- **登录 / 多账号** —— 自用阶段不需要
- **角色创建表单**（名字、性格、头像怎么填）—— 等「新建角色」接口定了再画
