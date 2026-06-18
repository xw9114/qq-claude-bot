# QQ AI Bot

基于 [NoneBot2](https://nonebot.dev/) 与 OneBot v11 的 QQ 机器人，通常配合 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 使用。项目提供 OpenAI 兼容接口聊天，并集成角色扮演、知识问答、棋类游戏、一言、随机老婆和表情包等功能。

> 仓库名中的 `claude` 为历史命名；当前聊天实现使用 OpenAI Python SDK，可通过 `OPENAI_BASE_URL` 接入兼容服务。

## 功能

- AI 对话：问候后进入连续对话，也可直接 @机器人
- 角色扮演：柯南、猫娘、古代谋士、毒舌导师
- 娱乐功能：知识问答、塔罗牌、笑话、一言、随机老婆
- 群聊游戏：五子棋、围棋、黑白棋
- 图片功能：PetPet 与 Memes 表情包
- `/help` 查看机器人内置帮助

## 环境要求

- Python 3.9+
- 支持 OneBot v11 的 QQ 协议实现，例如 NapCatQQ
- 一个 OpenAI 兼容 API 的密钥与 Base URL

## 快速开始

### 1. 安装依赖

Windows：

```bat
setup.bat
```

Linux / macOS：

```bash
chmod +x setup.sh start.sh
./setup.sh
```

也可以手动安装：

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

复制配置模板：

```bash
cp .env.example .env
```

Windows 可使用：

```bat
copy .env.example .env
```

至少填写：

```env
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
```

`.env` 包含密钥，已被 Git 忽略，请勿提交。

### 3. 配置 NapCatQQ

1. 安装并登录 NapCatQQ。
2. 新建 OneBot v11 反向 WebSocket（Universal）连接。
3. 将 URL 设置为 `ws://127.0.0.1:8080/onebot/v11/ws`。
4. 确认 `.env` 中的 `HOST` 与 `PORT` 和该 URL 一致。

### 4. 启动

Windows：

```bat
start.bat
```

Linux / macOS：

```bash
./start.sh
```

也可以在虚拟环境中直接运行：

```bash
python bot.py
```

启动后，在 QQ 中发送“你好”或 @机器人即可开始对话。

## 配置说明

| 配置项 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | 是 | - | OpenAI 或兼容服务的 API Key |
| `OPENAI_BASE_URL` | 否 | SDK 默认地址 | OpenAI 兼容接口地址 |
| `DRIVER` | 是 | `~fastapi+~httpx+~websockets` | NoneBot 驱动器组合 |
| `HOST` | 否 | `127.0.0.1` | NoneBot 监听地址 |
| `PORT` | 否 | `8080` | NoneBot 监听端口 |
| `COMMAND_START` | 否 | `["/"]` | 命令前缀 |
| `SUPERUSERS` | 否 | `[]` | 超级用户 QQ 号集合 |
| `DATABASE_URL` | 否 | SQLite | 棋类等插件使用的数据库地址 |
| `BOARDGAME_TIMEOUT` | 否 | `600` | 棋局超时时间（秒） |

当前 AI 模型在 `plugins/claude_chat.py` 中设置为 `gpt-5`。使用兼容服务时，请确认该服务提供同名模型，或按需修改模型名。

## API 连通性测试

配置好 `.env` 后运行：

```bash
python test_api.py
```

脚本只输出连接结果，不会打印 API Key。

## 项目结构

```text
qq-claude-bot/
├── bot.py                  # NoneBot 入口与第三方插件加载
├── plugins/
│   └── claude_chat.py      # AI 对话、角色扮演、问答与娱乐指令
├── .env.example            # 可公开的配置模板
├── requirements.txt        # Python 依赖
├── setup.bat / setup.sh    # 环境初始化脚本
├── start.bat / start.sh    # 启动脚本
└── test_api.py             # OpenAI 兼容接口连通性测试
```

运行过程中生成的虚拟环境、日志、缓存、数据库和插件资源不会纳入 Git。

## 常见问题

### NapCat 已启动，但机器人没有收到消息

- 检查反向 WebSocket URL 是否为 `ws://127.0.0.1:8080/onebot/v11/ws`。
- 检查 NoneBot 日志中是否出现 OneBot 连接记录。
- 如果修改了 `.env` 的 `HOST` 或 `PORT`，同步修改 NapCat 的连接 URL。

### AI 功能提示“API 未配置”

- 确认 `.env` 位于项目根目录。
- 确认配置项名称是 `OPENAI_API_KEY`，而不是旧版的 `ANTHROPIC_API_KEY`。
- 使用 `python test_api.py` 检查兼容接口是否可用。

### Memes 插件在 Windows 无报错退出

安装 [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/VC_redist.x64.exe) 后重试。

## 安全说明

- 不要提交 `.env`、数据库、日志或包含密钥的压缩备份。
- 如果密钥曾被上传到 GitHub，请立即在服务商后台撤销并重新生成。
- 建议使用专门的机器人 QQ 账号，并遵守 QQ、NapCatQQ 和 API 服务商的使用规则。

## 许可证

[MIT](LICENSE)
