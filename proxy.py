#!/usr/bin/env python3
"""
Anyrouter Claude Proxy
======================

Bridges hermes (or any plain Anthropic-messages client) to anyrouter.top's
1M-context Claude channel by injecting the request shape that Claude Code
sends. Without this, anyrouter rejects requests with "1m 上下文已经全量可用，
请启用 1m 上下文后重试" or panics on missing body fields.

Listens on 127.0.0.1:8989, forwards POST /v1/messages -> anyrouter.top,
and on the way:
  - rewrites URL to /v1/messages?beta=true
  - injects headers:
      anthropic-beta: <full Claude Code beta set>
      anthropic-dangerous-direct-browser-access: true
      User-Agent: claude-cli/2.1.126 (external, sdk-cli)
      x-app: cli
  - injects body fields (only if missing) required by the declared betas:
      thinking, context_management, output_config

Configurable via env vars:
  HERMES_PROXY_PORT      (default 8989)
  HERMES_PROXY_EFFORT    low | medium | high | xhigh   (default medium)
  HERMES_PROXY_THINKING  adaptive | disabled            (default adaptive)
  HERMES_PROXY_LOG       0 | 1                          (default 0, no body logging)

Streaming SSE responses are passed through unmodified.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.request
from typing import Any


sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# --- Configuration -----------------------------------------------------------

PORT = int(os.environ.get("HERMES_PROXY_PORT", "8989"))
EFFORT = os.environ.get("HERMES_PROXY_EFFORT", "medium")
THINKING_MODE = os.environ.get("HERMES_PROXY_THINKING", "adaptive")
LOG_BODIES = os.environ.get("HERMES_PROXY_LOG", "0") == "1"

UPSTREAM_HOST = "anyrouter.top"
UPSTREAM_BASE = f"https://{UPSTREAM_HOST}"

# Header set Claude Code 2.1.126 sends for the 1M-context Claude channel.
# Split into two profiles: opus_1m (full 7-beta set) vs. standard (drops the
# two 1M-context-specific betas). Selected per-request based on the model.
BETAS_OPUS_1M = [
    "claude-code-20250219",
    "context-1m-2025-08-07",          # 1M-context only
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",  # paired with context_management body field
    "prompt-caching-scope-2026-01-05",
    "advisor-tool-2026-03-01",
    "effort-2025-11-24",
]
_ONE_M_ONLY_BETAS = {"context-1m-2025-08-07", "context-management-2025-06-27"}
BETAS_STANDARD = [b for b in BETAS_OPUS_1M if b not in _ONE_M_ONLY_BETAS]

INJECTED_BETAS_FULL = ",".join(BETAS_OPUS_1M)
INJECTED_BETAS_STD = ",".join(BETAS_STANDARD)

# Body fields that only belong to the 1M-context channel; stripped for
# standard models (haiku / sonnet) to avoid anyrouter backend rejection (520).
ONE_M_BODY_FIELDS = ("context_management",)

# Headers always injected for the opus_1m channel (full Claude Code fingerprint).
# anthropic-beta is filled per-request from the matching profile in _proxy().
# For the standard channel, NONE of these are injected — anyrouter routes
# requests carrying any cc fingerprint (User-Agent, x-app, cc-style betas,
# cc billing system header) into the 1M-context check, which then 400s for
# non-Opus models. Standard requests must therefore look like plain Anthropic
# SDK calls.
INJECTED_HEADERS_OPUS_1M = {
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "User-Agent": "claude-cli/2.1.126 (external, sdk-cli)",
    "x-app": "cli",
}
INJECTED_HEADERS_STANDARD = {
    "anthropic-version": "2023-06-01",
}

# Header keys that hermes/CD might forward but which leak the cc identity to
# anyrouter. Stripped from outbound requests on the standard channel.
STANDARD_STRIP_HEADER_PREFIXES = ("user-agent", "x-app", "anthropic-beta")

# Path to a captured Claude Code request body. Used as the base body so that
# anyrouter's strict validation (system fingerprint + metadata.device_id) sees
# fields it recognizes. Hermes business fields (model/messages/max_tokens/...)
# override matching keys; everything else (system, metadata, thinking,
# context_management, output_config) is kept from the template verbatim.
TEMPLATE_PATH = os.environ.get(
    "HERMES_PROXY_TEMPLATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.json"),
)

# Keys that hermes' incoming body controls. Anything else is taken from
# the template.
HERMES_OVERRIDE_KEYS = frozenset({
    "model", "messages", "max_tokens", "temperature", "top_p", "top_k",
    "stop_sequences", "tools", "tool_choice", "stream",
})


def _load_template() -> dict[str, Any]:
    try:
        with open(TEMPLATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[error] failed to load template at {TEMPLATE_PATH}: {e}")
        return {}


# Body fields anyrouter's new-api backend dereferences when the corresponding
# beta flag is present. Used as fallback if no template is available.
INJECTED_BODY_DEFAULTS = {
    "thinking": {"type": THINKING_MODE},
    "context_management": {
        "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
    },
    "output_config": {"effort": EFFORT},
    "system": [
        {"type": "text", "text": "You are a helpful assistant."}
    ],
    "metadata": {"user_id": "hermes-anyrouter-proxy"},
}

# --- Helpers -----------------------------------------------------------------


def _redact(name: str, value: str) -> str:
    if name.lower() in ("authorization", "x-api-key"):
        return value[:14] + "***" if len(value) > 14 else "***"
    return value


def _classify_channel(raw: bytes) -> str:
    """Pick the request profile based on the body's `model` field.

    - `opus_1m` for Opus and Sonnet (both support the 1M-context channel via
      `context-1m-2025-08-07` beta) → full 1M-context header + body
      fingerprint required by anyrouter.
    - `standard` for Haiku (no 1M support) → drop 1M-only betas, body
      fields, and the `?beta=true` query so anyrouter routes through the
      plain channel and doesn't 520.

    Empty / non-JSON body falls back to `opus_1m` so legacy hermes / cc-style
    callers without an explicit model field keep their working behavior.
    """
    if not raw:
        return "opus_1m"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "opus_1m"
    if not isinstance(parsed, dict):
        return "opus_1m"
    model = str(parsed.get("model", "")).lower()
    # Models known NOT to support the 1M-context channel on anyrouter.
    # Everything else (opus, sonnet) gets the full 1M fingerprint.
    if "haiku" in model:
        return "standard"
    return "opus_1m"


def _patch_body(raw: bytes, channel: str = "opus_1m") -> bytes:
    """Compose outbound body based on the routing channel.

    opus_1m: take the captured CC template as base, overlay hermes-controlled
    keys (model, messages, max_tokens, ...) on top. This is the only way
    anyrouter's 1M-context channel accepts requests.

    standard: pass the hermes/Claude Desktop body through unchanged. Adding
    the CC template's `system` (with `x-anthropic-billing-header: cc_version=...`),
    `metadata.device_id`, `thinking`, `output_config`, etc. tags the request
    as a Claude Code call and forces anyrouter into the 1M-context check,
    which then 400s for non-1M models like Haiku.
    """
    if not raw:
        return raw
    try:
        hermes_body = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(hermes_body, dict):
        return raw

    if channel == "standard":
        # Strip any leaked cc-fingerprint fields the client might have set,
        # then forward as-is. Don't merge the template.
        for field in ("context_management",) + (
            # Common cc-only fields hermes won't set but Claude Desktop might.
        ):
            hermes_body.pop(field, None)
        return json.dumps(hermes_body, ensure_ascii=False).encode("utf-8")

    # opus_1m channel: full CC template + hermes overrides.
    template = _load_template()
    if template:
        composed = dict(template)
        for key in HERMES_OVERRIDE_KEYS:
            if key in hermes_body:
                composed[key] = hermes_body[key]
    else:
        # Fallback: just inject the bare-minimum defaults.
        composed = dict(hermes_body)
        for key, default in INJECTED_BODY_DEFAULTS.items():
            composed.setdefault(key, default)
        composed.setdefault("stream", True)

    return json.dumps(composed, ensure_ascii=False).encode("utf-8")


def _patch_path(path: str, channel: str = "opus_1m") -> str:
    """Align /v1/messages query string with the routing channel.

    The `?beta=true` query is anyrouter's switch into the 1M-context route;
    a request landing there without the matching `context-1m-2025-08-07`
    beta header is rejected with "1m 上下文已经全量可用，请启用 1m 上下文
    后重试". Therefore:

    - opus_1m channel: ensure `?beta=true` is present.
    - standard channel: ensure `?beta=true` is NOT present, even if the
      client (e.g. Claude Desktop) added it on its own.
    """
    if not path.startswith("/v1/messages"):
        return path

    has_beta = "beta=true" in path

    if channel == "opus_1m":
        if has_beta:
            return path
        sep = "&" if "?" in path else "?"
        return path + sep + "beta=true"

    # standard channel: strip beta=true if the client added it.
    if not has_beta:
        return path
    base, _, query = path.partition("?")
    remaining = [p for p in query.split("&") if p and p != "beta=true"]
    return base if not remaining else base + "?" + "&".join(remaining)


# --- Handler -----------------------------------------------------------------


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Set in _proxy() per-request. Used by log_message() so each access line
    # records which routing profile was applied.
    _channel: str = "-"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Always emit a one-line access log (no body)
        line = fmt % args if args else fmt
        print(f"[access] {self.command} {self.path} [{self._channel}] -> {line}")

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        # Classify before rewriting so log_message can report the channel.
        channel = _classify_channel(body)
        self._channel = channel
        body = _patch_body(body, channel)

        # Build outbound headers: take everything except hop-by-hop headers,
        # then overlay our injected headers per-channel.
        hop_by_hop = ("host", "content-length", "connection", "accept-encoding")
        outbound: dict[str, str] = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in hop_by_hop:
                continue
            # On the standard channel, drop any cc-fingerprint headers the
            # client added (User-Agent, x-app, anthropic-beta). We will
            # re-inject only the minimum set below.
            if channel == "standard" and kl in STANDARD_STRIP_HEADER_PREFIXES:
                continue
            outbound[k] = v

        if channel == "opus_1m":
            for k, v in INJECTED_HEADERS_OPUS_1M.items():
                outbound[k] = v
            outbound["anthropic-beta"] = INJECTED_BETAS_FULL
        else:
            for k, v in INJECTED_HEADERS_STANDARD.items():
                outbound[k] = v
            # Standard channel: no anthropic-beta at all — any cc-style beta
            # tags this as a Claude Code request and anyrouter forces 1M check.
            outbound.pop("anthropic-beta", None)

        outbound["Host"] = UPSTREAM_HOST
        outbound["Accept-Encoding"] = "identity"  # easier to relay raw bytes
        outbound["Content-Length"] = str(len(body))

        upstream_url = UPSTREAM_BASE + _patch_path(self.path, channel)

        if LOG_BODIES:
            print(f"[upstream] {self.command} {upstream_url}")
            for k, v in outbound.items():
                print(f"  {k}: {_redact(k, v)}")
            print(f"  -- body ({len(body)} bytes) --")
            try:
                parsed = json.loads(body)
                print(json.dumps(parsed, indent=2, ensure_ascii=False)[:1500])
            except Exception:
                print(body[:500].decode("utf-8", errors="replace"))

        req = urllib.request.Request(
            upstream_url,
            data=body if body else None,
            method=self.command,
            headers=outbound,
        )

        try:
            resp = urllib.request.urlopen(req, timeout=600)
            resp_status = resp.status
            resp_headers = list(resp.headers.items())
        except urllib.error.HTTPError as e:
            resp = e
            resp_status = e.code
            resp_headers = list(e.headers.items()) if e.headers else []
        except Exception as e:
            print(f"[error] upstream failure: {e!r}")
            self.send_error(502, f"Upstream error: {e}")
            return

        # Stream response back to client. Strip hop-by-hop headers so our
        # own connection management stays consistent.
        self.send_response(resp_status)
        skip = {"transfer-encoding", "connection", "content-encoding",
                "content-length", "keep-alive"}
        for k, v in resp_headers:
            if k.lower() in skip:
                continue
            self.send_header(k, v)
        # We will use chunked encoding ourselves for streaming bodies.
        is_sse = any(
            k.lower() == "content-type" and "event-stream" in v.lower()
            for k, v in resp_headers
        )
        if is_sse:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._stream_chunked(resp)
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _stream_chunked(self, resp: Any) -> None:
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        self._proxy()

    def do_GET(self) -> None:
        self._proxy()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --- Main --------------------------------------------------------------------


def main() -> None:
    addr = ("127.0.0.1", PORT)
    print(
        f"[anyrouter-proxy] listening on http://{addr[0]}:{addr[1]} "
        f"-> {UPSTREAM_BASE}  (effort={EFFORT}, thinking={THINKING_MODE})"
    )
    server = ThreadingServer(addr, ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[anyrouter-proxy] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
