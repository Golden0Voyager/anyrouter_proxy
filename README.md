# hermes_anyrouter_proxy

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

## 可调参数（环境变量，在 plist 里设置）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HERMES_PROXY_PORT` | `8989` | 监听端口 |
| `HERMES_PROXY_EFFORT` | `medium` | 推理深度（`low`/`medium`/`high`/`xhigh`）|
| `HERMES_PROXY_THINKING` | `adaptive` | 思考模式（`adaptive`/`disabled`）|
| `HERMES_PROXY_LOG` | `0` | `1` 启用 body 详细日志 |
| `HERMES_PROXY_TEMPLATE` | `<脚本同目录>/template.json` | 模板文件路径 |

## 维护

- **重抓模板**（cc 升级、anyrouter 改了校验规则后）：见详细文档第 9.1 节
- **完整文档**：`~/Code/docs/agent/hermes-anyrouter-claude-1m-proxy.md`

## 依赖

- macOS（launchd）
- Python 3.10+（用 `/opt/homebrew/bin/python3`，标准库即可，无外部依赖）
- 一个能用 Claude Code (`claude` CLI) 接通 anyrouter 的账号 —— 用于第一次抓取请求模板

## License

私人项目，仅供个人 hermes + anyrouter 使用。`template.json` 中的 `device_id` 是个人设备指纹，**勿公开**。
