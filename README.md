# anyrouter_proxy

让 [hermes agent](https://github.com/NousResearch/hermes-agent) 能够通过 [anyrouter.top](https://anyrouter.top) 中转调用 Anthropic Claude 模型（1M-context 通道）的本地透传代理。

## 为什么需要

anyrouter 的 Claude 1M-context 通道对请求做了**严格的指纹校验**，只接受 Claude Code (`claude` CLI) 同款的请求结构：
- URL 必须带 `?beta=true`
- 必须有 7 个特定的 `anthropic-beta` header
- body 必须含 `system` / `metadata` / `thinking` / `context_management` / `output_config` 等字段
- `metadata.user_id.device_id` 必须是 anyrouter 已识别的设备指纹

普通 Anthropic SDK 不会发这些 → 直接被 anyrouter 拒绝（"1m 上下文已经全量可用，请启用 1m 上下文后重试" / 后端 panic 500）。

本代理监听 `127.0.0.1:8989`，**用 cc 真实捕获到的请求做模板**，把 hermes 普通的 Anthropic 请求改写成 cc 同款再转发出去。

## 架构

```
hermes  →  http://127.0.0.1:8989  →  https://anyrouter.top  →  Claude
              │
              └─ proxy.py 注入 cc 同款 header/query/body
                 template.json 提供 anyrouter 必需的校验字段
```

## 文件清单

| 文件 | 用途 |
|---|---|
| `proxy.py` | 代理主程序（HTTP 服务器 + body 改写）|
| `template.json` | cc 真实请求抽出的 body 模板（system/metadata/thinking/...）|
| `proxy.log` | 运行时日志（被 .gitignore 排除）|
| `local.hermes-anyrouter-proxy.plist` | launchd 配置参考（实际加载的副本在 `~/Library/LaunchAgents/`）|

## 首次部署（新用户）

### 1. 克隆项目

```bash
git clone https://github.com/Golden0Voyager/anyrouter_proxy.git ~/Code/anyrouter_proxy
```

### 2. 准备 `template.json`

`template.json` 包含 anyrouter 必需的设备指纹和请求模板，**不在 git 中**（含个人 `device_id`）。你有三种方式获取：

| 方式 | 适用场景 | 步骤 |
|---|---|---|
| **A. 拷贝现有用户的 `template.json`** | 家人/团队成员已有配置 | `cp /现有用户路径/anyrouter_proxy/template.json ~/Code/anyrouter_proxy/` |
| **B. 用 Claude Code 抓取** | 自己首次部署 | 见下方"抓取 template.json" |
| **C. 基于 `template.example.json` 修改** | 无法抓包时 fallback | `cp template.example.json template.json`，手动填入你的 `device_id` |

#### 方式 B：用 Claude Code 抓取 `template.json`

前提：你的 Claude Code 已通过 anyrouter 官方通道正常对话。

```bash
# 1. 启动 Claude Code（会自动走 anyrouter）
claude

# 2. 另开终端，找到 Claude Code 最新的请求日志
# Claude Code 日志路径示例：
# ~/Library/Caches/anthropic/claude-code/logs/
# 找到任意一条 POST /v1/messages 的完整请求 body

# 3. 提取 body 中以下字段保存为 template.json：
# - system（完整数组，含 billing header）
# - metadata（含 device_id）
# - thinking
# - context_management
# - output_config
```

### 3. 替换 plist 中的用户名路径

项目中的 plist 模板使用占位用户名 `/Users/hainingyu/`，需要替换成你自己的：

```bash
cd ~/Code/anyrouter_proxy
sed -i '' "s|/Users/hainingyu/|$HOME/|g" local.hermes-anyrouter-proxy.plist
```

### 4. 设置 API Key

在 `~/.zshenv.secrets`（或 `~/.bash_profile`、`~/.zshrc`）中：

```bash
export ANYROUTER_API_KEY="your-anyrouter-api-key"
```

## 部署

```bash
# 1. 复制 plist 到 LaunchAgents
cp local.hermes-anyrouter-proxy.plist ~/Library/LaunchAgents/

# 2. 加载服务
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist

# 3. 验证
launchctl list | grep hermes-anyrouter-proxy
curl -sS http://127.0.0.1:8989/v1/messages \
  -H "x-api-key: $ANYROUTER_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-7","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
```

## 配置 hermes 用这个代理

修改 `~/.hermes/config.yaml`：

```yaml
providers:
  Anyrouter-Claude:
    base_url: http://127.0.0.1:8989       # 指向本代理
    api_mode: anthropic_messages          # 注意：是 api_mode（下划线），不是 api（连字符）
    key_env: ANYROUTER_API_KEY
    model: claude-opus-4-7
    models:
      claude-opus-4-7: {}
      claude-sonnet-4-5-20250929: {}
      claude-haiku-4-5-20251001: {}
    default_model: claude-opus-4-7
```

改完跑：

```bash
hermes gateway restart
```

## 配置 Claude Desktop (Mac) 用这个代理

Claude Desktop 走标准 Anthropic SDK，代理会根据 `model` 字段**自动分流**：
- **Opus / Sonnet** → `opus_1m` 通道，注入完整 cc 指纹
- **Haiku / 其他** → `standard` 通道，仅保留最小 header，避免 anyrouter 520

### 1. 修改 settings.json

文件路径：
```
~/Library/Application Support/Claude/settings.json
```

完整配置模板（**两个区块都要写**，缺一不可）：

```json
{
  "inferenceProvider": "gateway",
  "gateway": {
    "apiUrl": "http://127.0.0.1:8989",
    "apiKey": "YOUR_ANYROUTER_API_KEY",
    "anthropicVersion": "2023-06-01"
  },
  "modelSelection": {
    "allowModelSelection": true,
    "enableModelPicker": true
  },
  "overrides": {
    "apiUrl": "http://127.0.0.1:8989",
    "apiKey": "YOUR_ANYROUTER_API_KEY",
    "anthropicVersion": "2023-06-01"
  }
}
```

**关键点**：
- `gateway.apiUrl` 决定模型列表拉取地址
- `overrides.apiUrl` 决定实际对话请求地址
- Claude Desktop **自带 `x-api-key`**，代理不会读取本机的 `$ANYROUTER_API_KEY`
- 建议使用**独立的 anyrouter API Key**，与 hermes / claude-code 分开，方便额度管控

### 2. 重启 Claude Desktop

完全退出（Cmd+Q）后重新打开，否则 `settings.json` 缓存不生效。

### 3. 验证模型列表

打开新对话 → 模型选择器应出现如下条目：

| 显示名称 | 实际 model 字段 | 代理通道 | 说明 |
|---|---|---|---|
| Claude Opus 4.7 | `claude-opus-4-7` | `opus_1m` | 完整 cc 指纹 |
| Claude Opus 4.7 1M | `claude-opus-4-7` + 1M suffix | `opus_1m` | 同上，1M 上下文开关 |
| Claude Sonnet 4.5 | `claude-sonnet-4-5-20250929` | `opus_1m` | 实测支持 1M 通道 |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | `standard` | 纯净 Anthropic SDK 请求 |

> **注意**：模型列表中可能出现重复条目（如两个 Opus 4.7），这是 Claude Desktop 对同名模型带/不带 1M suffix 的显示策略，不影响使用。

## 可调参数（环境变量，在 plist 里设置）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HERMES_PROXY_PORT` | `8989` | 监听端口 |
| `HERMES_PROXY_EFFORT` | `medium` | 推理深度（`low`/`medium`/`high`/`xhigh`）|
| `HERMES_PROXY_THINKING` | `adaptive` | 思考模式（`adaptive`/`disabled`）|
| `HERMES_PROXY_LOG` | `0` | `1` 启用 body 详细日志 |
| `HERMES_PROXY_TEMPLATE` | `<脚本同目录>/template.json` | 模板文件路径 |

## 日常管理（快捷命令）

项目附带交互式管理脚本 `anyrouter-proxy.sh`，支持数字菜单管理代理。

### 添加快捷命令

在 `~/.zshrc`（或 `~/.bashrc`）中添加 alias：

```bash
alias anyrouter-proxy='bash /Users/hainingyu/Code/anyrouter_proxy/anyrouter-proxy.sh'
```

然后 `source ~/.zshrc` 或新开终端即可使用。

### 菜单功能

```bash
anyrouter-proxy
```

```
======================================
   Hermes Anyrouter Proxy 管理菜单
======================================

  1) 启动代理
  2) 停止代理
  3) 重启代理
  4) 查看状态
  5) 查看实时日志
  6) 修改推理强度 (effort)
  7) 退出
```

### 切换推理强度

选 `6` 后按数字选择：

| 选项 | 强度 | 说明 |
|---|---|---|
| 1 | `low` | 快速响应，消耗 token 最少 |
| 2 | `medium` | 默认平衡 |
| 3 | `high` | 更深推理 |
| 4 | `xhigh` | 极致深度，最耗 token |

选择后代理会自动重启，新配置立即生效。

## 故障速查

| 现象 | 可能原因 | 排查 / 解决 |
|---|---|---|
| `Connection refused` | 代理未启动 | `anyrouter-proxy` → 选 4 看状态，或 `curl http://127.0.0.1:8989` |
| `520 Unknown Error` | standard 通道请求被 anyrouter 拒绝 | 检查模型是否在 standard 列表（Haiku），或账号是否仅支持 1M 通道 |
| `429 Too Many Requests` | anyrouter 账号该模型的额度耗尽 | 换模型或联系 anyrouter 客服；与 proxy 配置无关 |
| `503 Service Unavailable` | anyrouter 上游 Claude 侧瞬时不可用 | Claude Desktop 内置重试通常可恢复；若持续出现，检查 anyrouter 状态页 |
| `"1m 上下文已经全量可用..."` | 1M 通道缺少 `context-1m-2025-08-07` beta 或 `?beta=true` | 确认请求走的是 `opus_1m` 通道（看 `proxy.log` 中的 `[opus_1m]` 标记）|
| 模型列表为空/不更新 | `settings.json` 缓存未刷新 | Cmd+Q 完全退出 Claude Desktop 再重开 |
| 选 Haiku 仍报错 | anyrouter 账号本身不支持标准通道 | 部分 1M-only 订阅账号会拒绝所有非 1M 请求，与 proxy 无关 |

代理访问日志格式：
```
[access] POST /v1/messages [opus_1m] -> 200
```
方括号里的 `opus_1m` / `standard` 即为当前请求选用的通道，排错时先看这一位。

## 维护

- **重抓模板**（cc 升级、anyrouter 改了校验规则后）：见详细文档第 9.1 节
- **完整文档**：`~/Code/docs/agent/hermes-anyrouter-claude-1m-proxy.md`

## 依赖

- macOS（launchd）
- Python 3.10+（用 `/opt/homebrew/bin/python3`，标准库即可，无外部依赖）
- 一个能用 Claude Code (`claude` CLI) 接通 anyrouter 的账号 —— 用于第一次抓取请求模板

## 从旧版本迁移

如果你之前部署过 `hermes_anyrouter_proxy`，按以下步骤同步改名：

```bash
# 1. 停止代理
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist

# 2. 更新 remote
cd ~/Code/hermes_anyrouter_proxy
git remote set-url origin https://github.com/Golden0Voyager/anyrouter_proxy.git
git pull

# 3. 重命名目录
cd ~/Code
mv hermes_anyrouter_proxy anyrouter_proxy

# 4. 同步 plist 路径
sed -i '' 's|hermes_anyrouter_proxy|anyrouter_proxy|g' ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist

# 5. 重新加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist

# 6. 更新 zshrc alias（如果有）
# alias anyrouter-proxy='bash ~/Code/anyrouter_proxy/anyrouter-proxy.sh'
```

## License

私人项目，仅供个人 hermes + anyrouter 使用。`template.json` 中的 `device_id` 是个人设备指纹，**勿公开**。
