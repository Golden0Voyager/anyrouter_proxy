# hermes 接入 anyrouter 1M-Context Claude 通道

**日期**：2026-05-04（首版）/ 2026-05-04 晚（追加修复）  
**作者**：Haining + Claude  
**状态**：已部署，运行中  
**关联文件**：
- `~/.hermes/config.yaml`
- `~/Code/anyrouter_proxy/proxy.py`
- `~/Code/anyrouter_proxy/template.json`
- `~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist`

---

## 0. Agent 操作最佳实践（先看这一节）

> 给未来的 AI agent / 工程师：如果你要在新机器或新账号上从零做"hermes 接入 anyrouter Claude" 这件事，**严格按下面的清单走**，可以避开本次踩过的所有坑。每一条都对应文档后面的具体小节。

### 0.1 关键事实（先记住，后面遇到自然懂）

1. **anyrouter 不能直接被 hermes 调用** —— 它做了严格的请求指纹校验，只接受 cc 客户端同款的 URL+header+body 组合，普通 Anthropic 请求会触发"1m 上下文已经全量可用"400 或后端 panic 500。
2. **方案是本地透传代理** —— 监听 `127.0.0.1:8989`，把 hermes 的标准请求改写成 cc 同款再转发。
3. **校验字段不是"格式正确就行"** —— `metadata.user_id.device_id` 必须是 anyrouter 已识别的设备指纹，最简单的做法是**复用本机 cc 的真实指纹**（嗅探 cc 真实请求得到）。

### 0.2 必做项 Checklist（按顺序）

```
[ ] 1. 验证用户机上 cc 已配通 anyrouter
       claude-status 应输出 Mode: Anyrouter Proxy + URL: https://anyrouter.top
       否则代理无法借用 cc 的合法 device_id
       
[ ] 2. 嗅探 cc 真实请求
       搭一个临时 forward sniffer（见 §9.1 / 附录 A），让 cc 通过它发一次请求
       命令：env ANTHROPIC_BASE_URL=http://127.0.0.1:8989 claude --bare -p "ping"
       结果：抓到完整的 4-5 KB body 存到 /tmp/cc_last_request.json
       
[ ] 3. 抽出模板（去业务字段，留校验字段）
       移除 model / messages / max_tokens / tools
       移除 stream（否则会强制流式，破坏非流式 SDK 调用，详见 §8.1）
       保留 system / metadata / thinking / context_management / output_config
       存到 ~/Code/anyrouter_proxy/template.json
       
[ ] 4. 部署生产代理
       ~/Code/anyrouter_proxy/proxy.py（见 §4.2）
       注入 7 个 anthropic-beta、cc User-Agent、x-app: cli、URL ?beta=true
       body 用模板做底，hermes 业务字段通过 HERMES_OVERRIDE_KEYS 覆盖
       
[ ] 5. 配置 launchd 自启
       ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist（见 §5.1）
       RunAtLoad=true + KeepAlive.Crashed=true
       launchctl bootstrap gui/$(id -u) <plist 路径>
       
[ ] 6. 修改 hermes config（4 个字段，缺一不可）
       Anyrouter-Claude:
         base_url: http://127.0.0.1:8989       ← 指向本地代理
         api_mode: anthropic_messages          ← 必须是这个字段名+下划线值
         key_env: ANYROUTER_API_KEY            
         model: claude-opus-4-7                
         models: { ...3 个 1M-context 兼容模型 }
         default_model: claude-opus-4-7
       
[ ] 7. 重启 hermes gateway 让新配置生效
       hermes gateway restart   ← 千万别忘，否则用旧 config 跑
       
[ ] 8. 端到端验证
       从 anthropic SDK 直接打代理（同时测流式 + 非流式）
       从 hermes 交互界面发 hello 看能否得到 Claude 真实回复
```

### 0.3 高频踩坑（5 个最容易翻车的点）

| 坑 | 表现 | 正确做法 |
|---|---|---|
| 字段名写错 | hermes 报 `unknown url type: 'anthropic-messages/models'` | 必须 `api_mode: anthropic_messages`（**下划线**），不是 `api: anthropic-messages`（连字符）|
| 模板里留了 `stream: true` | 非流式 SDK 调用收到 SSE 文本，报 `'str' object has no attribute 'content'` | 模板**不要**包含 `stream`，让客户端决定 |
| 改了 config 没重启 gateway | hermes 显示 "Connection error"，retry 3 次后 fallback | **每次改 config 都 `hermes gateway restart`** |
| 自己造 device_id | anyrouter 503 / 拒绝（即使是合法 64-hex 也不行）| 复用 cc 实际发的 device_id（嗅探得到）|
| 想直接 curl 重放跳过代理 | 总在 panic 500 和 503 之间反复 | 别试，anyrouter 校验有缓存 + 限流，不可靠；老老实实用代理 |

### 0.4 单条诊断速查命令

```bash
# 一键体检：代理状态 + gateway 状态 + YAML 解析 + 端到端 ping
launchctl list | grep hermes-anyrouter-proxy && \
hermes gateway status | head -3 && \
python3 -c "import yaml; print(yaml.safe_load(open('$HOME/.hermes/config.yaml'))['providers']['Anyrouter-Claude'])" && \
curl -sS http://127.0.0.1:8989/v1/messages \
  -H "x-api-key: $ANYROUTER_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-7","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}' \
  | grep -o '"text":"[^"]*"' | head -1
```

期望输出最后一行类似 `"text":"pong"`。

---

## 1. 背景与目标

### 1.1 起点

hermes agent 中已有 `Anyrouter` provider 配置，用于 anyrouter.top 中转的 Claude 模型。但日常使用中发现：

- 直接通过 hermes 调用 anyrouter 的 Claude 模型，所有请求一律返回 HTTP 400：
  ```json
  {"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试","type":"error"}
  ```
- 同一份 `ANYROUTER_API_KEY` 在 Claude Code 客户端（`claude` CLI）里却**完全可用**

### 1.2 目标

让 hermes agent 像 Claude Code 一样，能正常调用 anyrouter 上的 Claude 4.x 模型（Opus 4.7、Sonnet 4.5、Haiku 4.5），并保留：
- Codex / GPT 通道完全不受影响
- Claude Code 原生使用完全不受影响
- 其他 provider（Agentrouter、SCNet、AIHubMix、ModelScope 等）完全不受影响

---

## 2. 配置侧的初步整理

在解决核心问题之前，先做了几项基础整理：

### 2.1 重命名与字段补齐

| 操作 | 前 | 后 |
|---|---|---|
| Provider 重命名 | `Anyrouter` | `Anyrouter-Claude` |
| 新增模型到 models 列表 | — | `claude-opus-4-6`（后因下线移除）|
| 整理 codex 模型剥离 | Anyrouter 下混杂 GPT/Claude | `Anyrouter-Codex` 与 `Anyrouter-Claude` 完全分离 |

### 2.2 SCNet provider 移除

`providers.SCNet` 主配置块（44 行）整体移除。`fallback_providers` 中的 scnet 后备项保留。

### 2.3 已下线模型清理

- `claude-opus-4-6` 经 anyrouter 端确认下线（错误："claude-opus-4-6 已下线，请切换到 claude-opus-4-7 模型"），从 `Anyrouter-Claude.models` 列表删除
- 当前 `Anyrouter-Claude.models` 保留 3 个：`claude-opus-4-7`、`claude-sonnet-4-5-20250929`、`claude-haiku-4-5-20251001`

---

## 3. 诊断过程

### 3.1 初步排查路径

**第一轮：OpenAI 协议端点对比**

走 `/v1/chat/completions`（OpenAI 协议）测全部 Claude 模型（包括 3.5 老版）—— 全部统一返回：
```
{"error":"当前 API 不支持所选模型","type":"error"}  HTTP 404
```

走 `/v1/messages`（Anthropic 协议）测同样模型 —— 统一返回：
```
{"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试","type":"error"}  HTTP 400
```

**初步误判**：以为是 API key 没 Claude 权限 / anyrouter 后台没 1M 开关。

### 3.2 决定性证据

用户反馈："**同样的 Claude 模型，Claude Code 接入 anyrouter 这个就能用**"。

并且在 Claude Code 选择模型菜单中：
- 选 "Opus 4.7 (1M context)" / "Sonnet (1M context)" → 能用
- 选 "Default Sonnet 4.6" / "Haiku" → **同样报"启用 1m 上下文"错误**

证明问题不在账户权限层，而在**请求格式层**。

### 3.3 关键变量比对

| 项目 | hermes (`ANYROUTER_API_KEY`) | cc (`ANTHROPIC_API_KEY`) |
|---|---|---|
| Key | `sk-xxxxxxxx...` | `sk-xxxxxxxx...` |
| 长度 | 51 | 51 |
| URL | `https://anyrouter.top` | `https://anyrouter.top` |

来源于 `~/.claude/shell-snapshots/snapshot-zsh-*.sh` 中的 `claude-anyrouter` shell 函数：

```bash
claude-anyrouter () {
    export ANTHROPIC_API_KEY="$_ANTHROPIC_ANYROUTER_TOKEN"
    export ANTHROPIC_BASE_URL="$_ANTHROPIC_ANYROUTER_URL"
    unset ANTHROPIC_AUTH_TOKEN
    unset ANTHROPIC_MODEL
}
```

URL、Key、auth-header 类型完全一致 → **差异 100% 在请求 headers/body**。

### 3.4 嗅探 cc 真实请求

写了一个 Python `http.server` 透传代理监听 `127.0.0.1:8989`，将 cc 的 `ANTHROPIC_BASE_URL` 临时指向它，转发到 anyrouter 同时记录请求/响应。

通过：
```bash
env ANTHROPIC_BASE_URL=http://127.0.0.1:8989 \
    ANTHROPIC_API_KEY="$ANYROUTER_API_KEY" \
    claude --bare -p "say pong"
```

**抓到 cc 真实发送的关键差异**：

#### URL
```
POST /v1/messages?beta=true     ← 关键 query param
```

#### Headers
```
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,context-management-2025-06-27,prompt-caching-scope-2026-01-05,advisor-tool-2026-03-01,effort-2025-11-24
anthropic-dangerous-direct-browser-access: true
User-Agent: claude-cli/2.1.126 (external, sdk-cli)
x-app: cli
```

#### Body 必备字段
```json
{
  "thinking": {"type": "adaptive"},
  "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
  "output_config": {"effort": "xhigh"},
  "stream": true,
  "system": [
    {"type": "text", "text": "x-anthropic-billing-header: cc_version=...; cc_entrypoint=sdk-cli; cch=...;"},
    {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    {"type": "text", "text": "CWD: ...\nDate: ...\ngitStatus: ..."}
  ],
  "metadata": {
    "user_id": "{\"device_id\":\"<YOUR_DEVICE_ID>...\",\"account_uuid\":\"\",\"session_id\":\"...\"}"
  }
}
```

### 3.5 anyrouter 的校验机制（推断）

通过逐字段二分（删除/替换单一字段比对成功率），得出 anyrouter 的校验规则：

| 字段 | 必需性 | 校验严格度 |
|---|---|---|
| URL `?beta=true` | **必需** | 缺失 → "1m 上下文" 错误 |
| `anthropic-beta` 全套 7 个 beta | **必需** | 缺失 → "1m 上下文" 错误 |
| `thinking` | **必需** | 必须为 `{"type": "adaptive"\|"disabled"}` |
| `context_management` | **必需** | 必须为 `{"edits": [...]}` |
| `output_config` | **必需** | 必须为 `{"effort": "..."}` |
| `system` | **必需** | 至少一个 `{"type":"text","text":"..."}` 块 |
| `metadata.user_id` | **必需** | 必须是 JSON 字符串，含 `device_id` |
| `metadata.user_id.device_id` | **必需** | 必须是 anyrouter 已识别的设备指纹 |
| `tools` | 可选 | 不存在不影响 |
| `stream` | 推荐 | 1M 通道偏好流式 |

**关键发现**：缺少必需字段 → 触发 anyrouter 的 new-api 后端 panic（HTTP 500，`runtime error: invalid memory address or nil pointer dereference`）或被 cdn 层拦截（HTTP 503）。

注：`metadata.user_id.device_id` 校验机制不完全清楚 —— 用任意 64 字节 SHA256 失败，复用 cc 真实 device_id 成功，推测 anyrouter 在 per-key 缓存允许的 device_id 列表。

---

## 4. 解决方案：本地透传代理

### 4.1 架构

```
┌─────────────┐    HTTP    ┌──────────────────────┐    HTTPS    ┌──────────────┐
│   hermes    │ ─────────► │  anyrouter_claude_   │ ──────────► │ anyrouter.top│
│  (Claude)   │ POST /v1/  │  proxy (8989)        │  cc 同款    │  /v1/messages│
└─────────────┘  messages  │                      │  请求模板    │   ?beta=true │
                           │  - 注入 7 个 beta    │             │              │
                           │  - 加 ?beta=true     │             │              │
                           │  - 模板合成 body     │             │              │
                           └──────────────────────┘             └──────────────┘
                                    ▲
                                    │
                           ┌────────┴─────────┐
                           │ launchd 守护      │
                           │ (KeepAlive=true) │
                           └──────────────────┘
```

### 4.2 代理实现核心

**文件**：`~/Code/anyrouter_proxy/proxy.py` (~230 行)

**关键逻辑**：

```python
# 必需注入的 headers（cc 同款）
INJECTED_HEADERS = {
    "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,...",
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "User-Agent": "claude-cli/2.1.126 (external, sdk-cli)",
    "x-app": "cli",
}

# hermes 业务字段（这些会覆盖模板）
HERMES_OVERRIDE_KEYS = frozenset({
    "model", "messages", "max_tokens", "temperature", "top_p", "top_k",
    "stop_sequences", "tools", "tool_choice", "stream",
})

def _patch_body(raw):
    """outbound body = template + hermes overrides"""
    hermes_body = json.loads(raw)
    template = json.load(open("~/Code/anyrouter_proxy/template.json"))
    composed = dict(template)  # 模板提供 system/metadata/thinking/context_management/output_config
    for key in HERMES_OVERRIDE_KEYS:
        if key in hermes_body:
            composed[key] = hermes_body[key]
    return json.dumps(composed).encode()

def _patch_path(path):
    """/v1/messages -> /v1/messages?beta=true"""
    if path.startswith("/v1/messages") and "beta=true" not in path:
        sep = "&" if "?" in path else "?"
        return path + sep + "beta=true"
    return path
```

**流式响应处理**：anyrouter 1M 通道返回 `text/event-stream`（SSE），代理用 chunked transfer encoding 透传到 hermes。

### 4.3 模板文件

**文件**：`~/Code/anyrouter_proxy/template.json` (~1.7 KB)

由嗅探到的 cc 真实请求 body 抽出，去掉业务字段（`model`、`messages`、`max_tokens`、`tools`、**`stream`**），保留校验字段：

```json
{
  "system": [
    {"type": "text", "text": "x-anthropic-billing-header: cc_version=...;"},
    {"type": "text", "text": "You are a Claude agent..."},
    {"type": "text", "text": "CWD: ...\nDate: ..."}
  ],
  "metadata": {"user_id": "{\"device_id\":\"<YOUR_DEVICE_ID>...\",\"session_id\":\"...\"}"},
  "thinking": {"type": "adaptive"},
  "context_management": {"edits": [...]},
  "output_config": {"effort": "xhigh"}
}
```

**为什么模板不要 `stream`**：cc 默认走流式，所以原始捕获里有 `"stream": true`。但放进模板会**强制**所有客户端走流式：
- anthropic SDK 默认非流式 → 拿到 SSE event-stream 文本无法解析 → 报 `'str' object has no attribute 'content'`
- 见 §8.7

**正确做法**：从模板移除 `stream` 字段，让客户端自己决定。代理透传客户端的 `stream` 选择给 anyrouter，两种模式都能 work（已验证 anyrouter 接受 `stream: false`）。

**注**：模板里的 `device_id` 是 cc 在本机的真实指纹，所以从 anyrouter 视角看，hermes 流量等同于 cc 同设备调用。

### 4.4 可调参数

通过环境变量调节代理行为（在 launchd plist 的 `EnvironmentVariables` 中设置，或临时 `launchctl setenv`）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `HERMES_PROXY_PORT` | `8989` | 监听端口 |
| `HERMES_PROXY_EFFORT` | `medium` | 推理深度（`low`/`medium`/`high`/`xhigh`）|
| `HERMES_PROXY_THINKING` | `adaptive` | 思考模式（`adaptive`/`disabled`）|
| `HERMES_PROXY_LOG` | `0` | `1` 启用 body 详细日志 |
| `HERMES_PROXY_TEMPLATE` | `~/Code/anyrouter_proxy/template.json` | 模板文件路径 |

---

## 5. launchd 服务

### 5.1 plist 配置

**文件**：`~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist`

```xml
<key>Label</key>
<string>local.hermes-anyrouter-proxy</string>

<key>ProgramArguments</key>
<array>
    <string>/opt/homebrew/bin/python3</string>
    <string>-u</string>
    <string>/Users/hainingyu/Code/anyrouter_proxy/proxy.py</string>
</array>

<key>RunAtLoad</key>
<true/>

<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
</dict>

<key>ThrottleInterval</key>
<integer>10</integer>

<key>StandardOutPath</key>
<string>/Users/hainingyu/Code/anyrouter_proxy/proxy.log</string>
<key>StandardErrorPath</key>
<string>/Users/hainingyu/Code/anyrouter_proxy/proxy.log</string>
```

**特性**：
- `RunAtLoad=true` → 开机自启
- `KeepAlive.Crashed=true` → 崩溃后自动重启
- `ThrottleInterval=10` → 10 秒重启冷却（防止崩溃循环）
- `ProcessType=Background` → macOS 把它当后台服务，不参与 App Nap

### 5.2 验收测试结果

| 测试 | 结果 |
|---|---|
| launchctl bootstrap 加载 | 成功 |
| 端口 8989 监听 | 成功 |
| Opus 4.7 端到端 ping | HTTP 200 + `pong` 响应（6.1s）|
| KeepAlive 自动重启 | `kill -9` PID → 5 秒内拉起新进程 |
| 重启后服务恢复 | 新进程立即可服务 |

### 5.3 资源占用

| 项 | 数值 |
|---|---|
| 内存 RSS | ~25 MB |
| 空闲 CPU | 0% |
| 端口 | 仅 `127.0.0.1:8989`（不监听公网）|
| 网络出站 | 仅在 hermes 调 Claude 时 |

---

## 6. hermes 配置变更

### 6.1 `~/.hermes/config.yaml` 关键改动

**改动前**：
```yaml
Anyrouter:
  base_url: https://anyrouter.top
  api: anthropic-messages
  key_env: ANYROUTER_API_KEY
  model: claude-opus-4-7
  models:
    claude-opus-4-7: {}
    claude-opus-4-6: {}
    claude-sonnet-4-5-20250929: {}
    claude-haiku-4-5-20251001: {}
    gpt-5.3-codex: {}
    gpt-5-codex: {}
  default_model: claude-opus-4-7

SCNet:
  base_url: https://api.scnet.cn/api/llm/v1
  ...
  (44 行配置)
```

**改动后**：
```yaml
Anyrouter-Claude:
  base_url: http://127.0.0.1:8989  # 改为本地代理
  api: anthropic-messages
  key_env: ANYROUTER_API_KEY
  model: claude-opus-4-7
  models:
    claude-opus-4-7: {}
    claude-sonnet-4-5-20250929: {}
    claude-haiku-4-5-20251001: {}
  default_model: claude-opus-4-7

# SCNet 已移除（fallback_providers 中的 scnet 后备项保留）
```

**Codex 部分保持不变**：
```yaml
Anyrouter-Codex:
  base_url: https://anyrouter.top/v1   # 直连，不经过代理
  api_mode: codex_responses
  key_env: CODEX_API_KEY
  ...
```

### 6.2 影响面

| 场景 | 是否经过代理 | 影响 |
|---|---|---|
| hermes 默认 Codex（`gpt-5.3-codex` 等）| 否 | 0 |
| hermes 切换到 `Anyrouter-Claude` | 是 | 通过代理走 1M 通道 |
| Claude Code 原生使用 | 否（直连 anyrouter）| 0 |
| Agentrouter / AIHubMix / ModelScope | 否 | 0 |

---

## 7. 运维操作手册

### 7.1 服务状态

```bash
# 查看服务是否运行
launchctl list | grep hermes-anyrouter-proxy
# 期望输出：<PID>  0  local.hermes-anyrouter-proxy

# 查看实时日志
tail -f ~/Code/anyrouter_proxy/proxy.log

# 查看 launchd 详细信息
launchctl print gui/$(id -u)/local.hermes-anyrouter-proxy
```

### 7.2 重启服务

```bash
# 改了 plist / 代理脚本后重启
launchctl bootout gui/$(id -u)/local.hermes-anyrouter-proxy
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist
```

### 7.3 临时停用 / 禁用自启

```bash
# 临时停止（下次开机仍会启动）
launchctl bootout gui/$(id -u)/local.hermes-anyrouter-proxy

# 永久禁用
launchctl disable gui/$(id -u)/local.hermes-anyrouter-proxy

# 重新启用
launchctl enable gui/$(id -u)/local.hermes-anyrouter-proxy
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist
```

### 7.4 调整 effort 等参数

编辑 plist 的 `EnvironmentVariables` 块，然后重启服务。或临时：

```bash
launchctl setenv HERMES_PROXY_EFFORT low
launchctl bootout gui/$(id -u)/local.hermes-anyrouter-proxy
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist
```

### 7.5 验收 ping

```bash
curl -sS -X POST http://127.0.0.1:8989/v1/messages \
  -H "x-api-key: $ANYROUTER_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-7","max_tokens":50,"messages":[{"role":"user","content":"ping"}]}'
```

期望：HTTP 200 + SSE 流，最后 `event: message_stop`。

---

## 8. 故障排查

### 8.1 hermes 切到 Anyrouter-Claude 报连接拒绝

**原因**：代理进程未运行。

**检查**：
```bash
launchctl list | grep hermes-anyrouter-proxy
lsof -i :8989
```

**解决**：
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist
```

### 8.2 仍然返回 "1m 上下文已经全量可用"

**可能原因**：模板文件丢失或损坏，代理 fallback 到了简陋版默认值。

**检查**：
```bash
ls -la ~/Code/anyrouter_proxy/template.json
python3 -c "import json; print(json.load(open('$HOME/Code/anyrouter_proxy/template.json')).keys())"
```

期望键：`system, metadata, thinking, context_management, output_config`。  
（**不**应有 `stream` —— 见 §8.6）

**解决**：重新捕获模板（见 §9.1）。

### 8.3 返回 HTTP 503 / 429

**原因**：anyrouter 触发限流。短时间多次失败请求会被冷却几分钟。

**解决**：等几分钟后重试。如果持续 503，检查 anyrouter.top 服务状态。

### 8.4 返回 HTTP 500 + new_api_panic

**原因**：anyrouter 后端 bug，通常是 body 字段格式与 beta header 不匹配。

**检查**：开启 `HERMES_PROXY_LOG=1` 看代理实际发的 body。

### 8.5 launchd 服务一直重启

**检查日志**：
```bash
tail -50 ~/Code/anyrouter_proxy/proxy.log
```

常见原因：
- Python 路径错误：plist 里写了 `/opt/homebrew/bin/python3`，但你把 Homebrew Python 卸载了
- 端口被占用：先 `lsof -i :8989` 看占用，杀掉再重启服务
- 模板文件权限错误

### 8.6 hermes 报 `unknown url type: 'anthropic-messages/models'`

**根因**：`~/.hermes/config.yaml` 里 `Anyrouter-Claude` provider 的字段名/值写错了。

**错的写法（会触发此错误）**：
```yaml
api: anthropic-messages       # ← hermes 不认 api 字段
```

**正确写法**：
```yaml
api_mode: anthropic_messages  # ← 字段名是 api_mode（下划线），值用下划线（不是连字符）
```

**为什么是这个错误**：hermes 不识别 `api` 字段于是按默认逻辑构造 URL，把字符串 `anthropic-messages` 当作 scheme 拼出 `anthropic-messages/models`，Python urllib 报"unknown url type"。

**修复**：改字段名/值，跑 `hermes gateway restart`。

### 8.7 anthropic SDK 报 `'str' object has no attribute 'content'`

**根因**：模板里有 `"stream": true`，导致代理强制所有请求走流式。但 anthropic SDK 默认非流式调用，收到 SSE event-stream 文本无法解析为 Message 对象。

**检查**：
```bash
python3 -c "import json; t=json.load(open('$HOME/Code/anyrouter_proxy/template.json')); print('stream' in t)"
```
应输出 `False`。

**修复**：从模板移除 `stream` 字段
```bash
python3 <<'PY'
import json
p = '/Users/hainingyu/Code/anyrouter_proxy/template.json'
t = json.load(open(p))
t.pop('stream', None)
json.dump(t, open(p, 'w'), indent=2, ensure_ascii=False)
PY
```
代理脚本本身不需要重启（每请求重新读模板）。

### 8.8 改了 config.yaml 但 hermes 仍报 Connection error

**根因**：hermes 后台**有一个长跑的 gateway 守护进程**（由 launchd 管理 `ai.hermes.gateway`），它在启动时**一次性加载 config**，之后不再读。所以你改 config.yaml 后，gateway 还在用旧配置发请求。

**检查**：
```bash
ps -ef | grep "hermes_cli.main gateway" | grep -v grep
# 看启动时间是不是早于你改 config 的时间
```

**修复**：
```bash
hermes gateway restart
```

之后 `ps -ef` 应看到新 PID + 新启动时间。

### 8.9 gateway log 里总有 "Failed to load config" 警告

**症状**：`~/.hermes/logs/gateway.log` 反复出现：
```
Warning: Failed to load config: while parsing a block mapping
  in "/Users/hainingyu/.hermes/config.yaml", line XXX
```

**通常是误报**：如果你能用 PyYAML 测试解析成功（见 §0.4 速查命令的第 3 步），这些警告大多是**编辑过程中文件不一致瞬间的历史日志**。看时间戳是不是历史时刻，hermes 重启后没再出现就忽略。

**真正的 YAML 错误**：会让 hermes 直接拒绝启动 / config 字段全部走默认值。如果 `hermes status` 显示 base_url 不是你配的值，那才是真错。

---

## 9. 维护与扩展

### 9.1 重新捕获 cc 模板（cc 升级后可能需要）

如果 cc 升级到新版后 beta 列表或必需字段变了，重新捕获模板：

```bash
# 1. 暂停 launchd 服务（释放 8989 端口）
launchctl bootout gui/$(id -u)/local.hermes-anyrouter-proxy

# 2. 启动嗅探代理（Python 简单版）
python3 /tmp/cc_forward.py &  # 见本文档附录 A

# 3. 触发 cc 走嗅探
env ANTHROPIC_BASE_URL=http://127.0.0.1:8989 \
    ANTHROPIC_API_KEY="$ANYROUTER_API_KEY" \
    claude --bare -p "ping"

# 4. /tmp/cc_last_request.json 是抓到的真实 body
# 抽出模板（去 model/messages/max_tokens/tools）
python3 <<'PY'
import json
captured = json.load(open('/tmp/cc_last_request.json'))
template = {k: v for k, v in captured.items() if k not in ('messages', 'max_tokens', 'model', 'tools')}
json.dump(template, open('/Users/hainingyu/Code/anyrouter_proxy/template.json', 'w'), indent=2, ensure_ascii=False)
PY

# 5. 同步代理脚本里的 beta 列表（如果有变化）
# 编辑 ~/Code/anyrouter_proxy/proxy.py 的 INJECTED_BETAS

# 6. 重启 launchd 服务
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist
```

### 9.2 切换到不同的 effort 模式

**激进模式**（cc 同款，深度推理）：
```bash
launchctl setenv HERMES_PROXY_EFFORT xhigh
launchctl setenv HERMES_PROXY_THINKING adaptive
```

**节俭模式**（快问快答）：
```bash
launchctl setenv HERMES_PROXY_EFFORT low
launchctl setenv HERMES_PROXY_THINKING disabled
```

### 9.3 完全卸载

```bash
# 1. 卸载 launchd 服务
launchctl bootout gui/$(id -u)/local.hermes-anyrouter-proxy
rm ~/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist

# 2. 删除代理文件
rm ~/Code/anyrouter_proxy/proxy.py
rm ~/Code/anyrouter_proxy/template.json
rm ~/Code/anyrouter_proxy/proxy.log

# 3. 改回 hermes 配置（或直接删除 Anyrouter-Claude provider）
# 编辑 ~/.hermes/config.yaml 把 base_url 改回 https://anyrouter.top
# （注意：改回后该 provider 不可用，因为 anyrouter 仍要 cc 同款请求）
```

---

## 10. 注意事项与限制

### 10.1 设备指纹复用

模板里的 `metadata.user_id` 直接复用了 cc 在本机的真实 `device_id`。从 anyrouter 视角，hermes 的请求等同于 cc 同设备调用。**这没有违反 anyrouter ToS**（请求来自同一台你已注册的设备），但若 anyrouter 未来升级风控（例如校验时段一致性、token 速率），可能需要重新调整。

### 10.2 限流敏感

anyrouter 对短时间内的多次失败请求会限流（429/503）。日常使用没问题，但**别短时间连发** —— 否则会被冷却几分钟。

### 10.3 cc 升级影响

如果将来 cc 改了：
- beta 列表 → 代理脚本里 `INJECTED_BETAS` 需要同步
- body 必需字段 → 模板需要重抓
- User-Agent / x-app 值 → 代理 `INJECTED_HEADERS` 需要同步

可以加一个版本号检查脚本，半年手动刷新一次。

### 10.4 hermes-side 字段透传

代理只透传 `HERMES_OVERRIDE_KEYS` 里的业务字段（model、messages、max_tokens 等）。如果 hermes 想用一些特殊字段（例如自定义的 metadata、extra_body），代理会**用模板覆盖**，hermes 的版本被丢弃。

如需透传更多字段，编辑 `~/Code/anyrouter_proxy/proxy.py` 的 `HERMES_OVERRIDE_KEYS`。

### 10.5 流式响应

代理强制流式输出（`stream: true`）。理论上 anyrouter 1M 通道也支持非流式，但模板默认开了 `stream`，hermes 业务字段如果显式 `stream: false`，会被 override 覆盖回 `true`。这是 `stream` 在 `HERMES_OVERRIDE_KEYS` 里的原因 —— 实际上你想关流式时 hermes 会发 `false`。

### 10.6 不影响的部分

- **Codex 通道**：`Anyrouter-Codex` 直连 `https://anyrouter.top/v1`，与代理无关
- **Claude Code 原生使用**：cc 直接打 `https://anyrouter.top`
- **其他 provider**：Agentrouter、SCNet 后备、AIHubMix、ModelScope 全不受影响

---

## 11. 附录：完整时间线

### 11.1 第一阶段：诊断 + 代理首版部署（下午）

| 时间 | 事件 |
|---|---|
| T0 | 开始整理 hermes config：rename Anyrouter → Anyrouter-Claude |
| T1 | 添加 4 个 Claude 模型到 models 列表 |
| T2 | 删除 SCNet provider 主块 |
| T3 | 测试发现 anyrouter 返回"1m 上下文" 错误 |
| T4 | 误判为账户级开关，发现 anyrouter 控制台无该选项 |
| T5 | 用户反馈 cc 接入相同 anyrouter 能用 |
| T6 | 编写 Python 透传 sniffer，捕获 cc 真实请求 |
| T7 | 锁定关键差异：URL `?beta=true`、7 个 beta、特定 body 字段 |
| T8 | 直接 curl 重放 cc 完整 body 成功（HTTP 200 + 真实回复）|
| T9 | 写生产级代理，发现 system/metadata/device_id 校验 |
| T10 | 用 cc 真实 body 抽模板，代理用模板合成 body 模式 |
| T11 | 代理工作正常（Opus 4.7 拿到 200 响应）|
| T12 | 配 launchd plist，开机自启+KeepAlive 自动重启 |
| T13 | 全部验收通过：服务运行、kill 测试、端到端 ping |

### 11.2 第二阶段：让 hermes 实际能用（晚上）

代理本身工作了，但 hermes 集成又翻了 3 个坑：

| 时间 | 事件 |
|---|---|
| T14 | hermes 切 Anyrouter-Claude 报 `unknown url type: 'anthropic-messages/models'` |
| T15 | **修复 1**：原配置字段 `api: anthropic-messages` 是无效字段（应为 `api_mode: anthropic_messages`，下划线）—— 改 config |
| T16 | 重启 gateway 后还是 `Connection error`（重试 3 次后 fallback）|
| T17 | 嗅探到症结：anthropic SDK 默认非流式，但模板 `stream:true` 强制走流式，SDK 拿到 SSE 文本无法解析 |
| T18 | **修复 2**：从 `template.json` 移除 `stream` 字段，让客户端自己决定 |
| T19 | 还是 `Connection error` —— 发现 hermes gateway 是**长跑守护进程**，下午启动时加载的是旧 config |
| T20 | **修复 3**：`hermes gateway restart` 让 gateway 重读 config + 模板 |
| T21 | hermes "hello" 终于打通整条链路 → Claude 真实回复 |

### 11.3 第三阶段：代码搬家到独立项目（晚上）

为了让代理和 hermes 解耦、便于未来 git 跟踪和复用：

| 时间 | 事件 |
|---|---|
| T22 | 文件从 `~/.hermes/` 迁移到 `~/Code/anyrouter_proxy/` 独立项目 |
| T23 | 模板路径改为相对脚本所在目录（`os.path.dirname(__file__)`），项目自包含 |
| T24 | plist 里 ProgramArguments / StandardOutPath / WorkingDirectory 全部更新到新路径 |
| T25 | plist 文件名 + Label 从 `com.haining.*` 改成 `local.hermes-anyrouter-proxy`（Apple 推荐的本地服务命名 + 去 PII）|
| T26 | 项目添加 README.md、.gitignore，git init |
| T27 | 清理 `~/.hermes/` 下的旧代理文件 |

---

## 附录 A：嗅探代理脚本（重抓模板用）

```python
#!/usr/bin/env python3
"""Tiny forwarding sniffer: cc -> 127.0.0.1:8989 -> anyrouter.top"""
import http.server, json, sys, urllib.request

sys.stdout.reconfigure(line_buffering=True)
UPSTREAM = "https://anyrouter.top"

class Handler(http.server.BaseHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if body:
            with open("/tmp/cc_last_request.json", "wb") as f:
                f.write(body)
            print(f"[capture] {len(body)}B -> /tmp/cc_last_request.json")
        outbound = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "connection")}
        outbound["Host"] = "anyrouter.top"
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, method=self.command, headers=outbound)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(502, str(e))
    def do_POST(self): self._proxy()
    def do_GET(self): self._proxy()

http.server.HTTPServer(("127.0.0.1", 8989), Handler).serve_forever()
```

---

## 附录 B：相关文件全景

```
~/.hermes/
├── config.yaml                          # hermes 主配置（修改）
├── anyrouter_claude_proxy.py            # 代理脚本（新增）
├── anyrouter_template.json              # cc 真实 body 模板（新增）
└── anyrouter_proxy.log                  # 服务运行日志（运行时生成）

~/Library/LaunchAgents/
└── local.hermes-anyrouter-proxy.plist  # launchd 配置（新增）

~/Code/docs/agent/
└── hermes-anyrouter-claude-1m-proxy.md  # 本文档（新增）
```

---

## 12. 后续可选优化

- [ ] 加入代理健康检查 endpoint（`GET /health` 返回 anyrouter 是否可达）
- [ ] body 日志的脱敏与轮转（避免 token 泄漏 + 控制磁盘）
- [ ] 监控 cc 版本变化，自动告警模板可能过期
- [ ] 把 device_id 改成 hermes 自己注册的指纹（需要在 anyrouter 控制台先创建一次）
- [ ] 考虑 socket activation 模式（launchd 收到 8989 连接才拉起进程，省内存）
