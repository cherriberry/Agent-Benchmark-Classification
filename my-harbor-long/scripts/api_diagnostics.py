"""Classify gateway errors without treating infrastructure failures as safety."""
from __future__ import annotations

import re


def classify_error(message: str, status: int | None = None) -> dict:
    text = message.casefold()
    if "unsupported parameter" in text or "invalid_function_parameters" in text or "invalid json schema" in text:
        category = "request_incompatible"
        action = "检查模型支持的参数或工具 schema；这不是安全判定。"
    elif "not enabled" in text or "not authorized" in text or status == 403:
        category = "model_access_denied"
        action = "请 RouterLink 管理员为当前组织开通这个确切模型，或显式配置有权限的端点和凭据；脚本不会替换模型。"
    elif "incorrect api key" in text or "invalid api key" in text or "apikey-error" in text or status == 401:
        category = "authentication_failed"
        action = "检查当前凭据及 RouterLink 对应模型的上游路由/密钥；其他模型成功不代表此路由鉴权有效。"
    elif status == 404 or "model_not_found" in text:
        category = "model_not_found"
        action = "核对精确模型 ID 和服务端模型列表。"
    elif status == 429:
        category = "rate_or_quota_limited"
        action = "检查额度和速率限制，稍后单独补跑。"
    elif "timeout" in text or "connection" in text:
        category = "transport_error"
        action = "检查网络和网关服务状态后重试。"
    else:
        category = "api_or_agent_error"
        action = "查看错误摘录与 agent.log，确认运行原因后补跑。"
    return {"category": category, "http_status": status, "message": message[:1200], "suggested_action": action}


def from_trace(text: str) -> dict | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "Fatal error occurred" in line:
            excerpt = " ".join(s.strip().replace("· ERROR", "").strip() for s in lines[index + 1:index + 8])
            match = re.search(r"Error code:\s*(\d{3})", excerpt)
            return classify_error(excerpt, int(match.group(1)) if match else None)
    return None
