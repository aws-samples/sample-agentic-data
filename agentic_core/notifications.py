"""
Notification Channels — 真实推送集成
支持: 企业微信群机器人 / 钉钉群机器人 / 飞书群机器人 / Amazon SES 邮件
"""

import json
import hashlib
import hmac
import base64
import time
import urllib.request
import urllib.parse
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    """Send JSON POST request."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid URL scheme: {url[:30]}")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"POST {url[:60]}... failed: {e}")
        return {"errcode": -1, "errmsg": str(e)}


def _markdown_to_plain(md: str, max_len: int = 2000) -> str:
    """Strip markdown to plain text for channels that need it."""
    import re
    text = re.sub(r'```[\s\S]*?```', '', md)
    text = re.sub(r'[#*`>|]', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:max_len]


def _markdown_for_webhook(md: str, max_len: int = 4000) -> str:
    """Trim markdown for webhook (they have size limits)."""
    if len(md) <= max_len:
        return md
    return md[:max_len - 20] + "\n\n... (报告已截断)"



def _tables_to_bullets(md: str) -> str:
    """Convert markdown tables to bullet lists for platforms that don't render tables (feishu)."""
    import re
    lines = md.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect table header row
        if '|' in line and i + 1 < len(lines) and re.match(r'^\s*\|[-:|\s]+\|', lines[i + 1]):
            # Parse header
            headers = [h.strip() for h in line.split('|') if h.strip()]
            i += 2  # skip header + separator
            # Parse data rows
            while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].split('|') if c.strip()]
                if len(cells) >= 2:
                    # Format: **header1**: value1 | header2: value2
                    parts = []
                    for hi, cell in enumerate(cells):
                        if hi < len(headers):
                            parts.append(f"{headers[hi]}: {cell}")
                        else:
                            parts.append(cell)
                    result.append(f"- {' | '.join(parts)}")
                i += 1
        else:
            result.append(line)
            i += 1
    return '\n'.join(result)



def _clean_report_content(md: str) -> str:
    """Clean report content for external push: remove chart JSON, suggestions, and action blocks."""
    import re
    lines = md.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Skip chart JSON blocks
        if stripped.startswith('{"type":"') and ('"items"' in stripped or '"indicators"' in stripped):
            continue
        # Skip suggestion JSON arrays
        if stripped.startswith('[{"title":') and '"query"' in stripped:
            continue
        # Skip suggestion code blocks
        if stripped.startswith('```suggestions') or stripped.startswith('```'):
            continue
        # Skip EMAIL/TICKET action blocks
        if stripped.startswith('[EMAIL]') or stripped.startswith('[TICKET]'):
            continue
        cleaned.append(line)

    # Remove trailing suggestion-like lines (short questions without markdown formatting)
    # These are follow-up suggestions that appear after the main report
    while cleaned:
        last = cleaned[-1].strip()
        if not last:
            cleaned.pop()
            continue
        # Heuristic: trailing lines that look like follow-up questions
        # (no markdown heading, contain question marks or analysis keywords, < 80 chars)
        if (not last.startswith('#') and not last.startswith('**') and not last.startswith('-') and not last.startswith('>') 
            and len(last) < 100
            and any(kw in last for kw in ['？', '分析', '问题', '是否', '哪些', '什么', '如何', '对比', '排查', '深挖', '专项'])):
            cleaned.pop()
            continue
        break

    return '\n'.join(cleaned).rstrip()


# ═══════ 企业微信 (WeCom) ═══════

def send_wecom(webhook_url: str, title: str, content: str, **kwargs) -> dict:
    """
    企业微信群机器人 Webhook 推送 (Markdown)
    webhook_url: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    """
    clean = _clean_report_content(content)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n\n{_markdown_for_webhook(clean, 3800)}"
        }
    }
    result = _post_json(webhook_url, payload)
    success = result.get("errcode", -1) == 0
    return {
        "success": success,
        "channel": "wecom",
        "message": result.get("errmsg", "unknown"),
        "raw": result,
    }


# ═══════ 钉钉 (DingTalk) ═══════

def send_dingtalk(webhook_url: str, title: str, content: str, secret: str = "", **kwargs) -> dict:
    """
    钉钉群机器人 Webhook 推送 (Markdown)
    webhook_url: https://oapi.dingtalk.com/robot/send?access_token=xxx
    secret: 可选，加签密钥
    """
    url = webhook_url
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

    clean = _clean_report_content(content)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{_markdown_for_webhook(clean, 3800)}"
        }
    }
    result = _post_json(url, payload)
    success = result.get("errcode", -1) == 0
    return {
        "success": success,
        "channel": "dingtalk",
        "message": result.get("errmsg", "unknown"),
        "raw": result,
    }


# ═══════ 飞书 (Feishu/Lark) ═══════

def send_feishu(webhook_url: str, title: str, content: str, secret: str = "", **kwargs) -> dict:
    """
    飞书群机器人 Webhook 推送 (Rich Text)
    webhook_url: https://open.feishu.cn/open-apis/bot/v2/hook/xxx
    secret: 可选，签名校验密钥
    """
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": _tables_to_bullets(_markdown_for_webhook(_clean_report_content(content), 3800)),
                },
                {
                    "tag": "hr",
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"Agentic Data · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
                        }
                    ],
                },
            ],
        },
    }

    # Feishu signature
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    result = _post_json(webhook_url, payload)
    success = result.get("code", -1) == 0 or result.get("StatusCode", -1) == 0
    return {
        "success": success,
        "channel": "feishu",
        "message": result.get("msg", result.get("errmsg", "unknown")),
        "raw": result,
    }


# ═══════ Amazon SES 邮件 ═══════

def send_ses_email(recipients: list, title: str, content: str,
                   sender: str = "noreply@agentic-data.aws", region: str = "us-east-1", **kwargs) -> dict:
    """
    Amazon SES 邮件推送 (HTML)
    recipients: ["user@example.com"]
    """
    try:
        import boto3
        ses = boto3.client("ses", region_name=region)

        # Convert markdown to basic HTML
        clean = _clean_report_content(content)
        html_body = _md_to_html(clean, title)

        response = ses.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": f"📊 {title}", "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": _markdown_to_plain(content), "Charset": "UTF-8"},
                },
            },
        )
        return {
            "success": True,
            "channel": "ses",
            "message": f"MessageId: {response['MessageId']}",
            "raw": {"MessageId": response["MessageId"]},
        }
    except Exception as e:
        return {
            "success": False,
            "channel": "ses",
            "message": str(e),
            "raw": {},
        }


def _md_to_html(md: str, title: str) -> str:
    """Basic markdown to HTML for email."""
    import re
    html = md
    html = re.sub(r'^### (.+)$', r'<h3 style="color:#1a1a2e;margin:16px 0 8px">\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h2 style="color:#1a1a2e;margin:20px 0 10px">\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^# (.+)$', r'<h1 style="color:#1a1a2e">\1</h1>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'^- (.+)$', r'<li style="margin:2px 0">\1</li>', html, flags=re.MULTILINE)
    html = re.sub(r'(<li.*</li>)', r'<ul style="margin:8px 0;padding-left:20px">\1</ul>', html, flags=re.DOTALL)
    html = html.replace('\n\n', '</p><p style="margin:8px 0;line-height:1.6">')
    html = html.replace('\n', '<br>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Arial,sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#333">
<div style="border-bottom:3px solid #4361ee;padding-bottom:12px;margin-bottom:20px">
<h1 style="margin:0;font-size:20px;color:#1a1a2e">{title}</h1>
<p style="margin:4px 0 0;font-size:12px;color:#888">Agentic Data · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</p>
</div>
<div style="font-size:14px;line-height:1.7"><p style="margin:8px 0;line-height:1.6">{html}</p></div>
<div style="margin-top:24px;padding-top:12px;border-top:1px solid #eee;font-size:11px;color:#999">
此报告由 Agentic Data 智能分析平台自动生成</div>
</body></html>"""


# ═══════ 统一发送入口 ═══════

CHANNEL_SENDERS = {
    "wecom": send_wecom,
    "dingtalk": send_dingtalk,
    "feishu": send_feishu,
    "ses": send_ses_email,
}

def send_notification(channel_config: dict, title: str, content: str) -> dict:
    """
    统一推送入口。
    channel_config: {"type": "wecom", "webhook_url": "...", ...}
    """
    ch_type = channel_config.get("type", "")
    sender = CHANNEL_SENDERS.get(ch_type)
    if not sender:
        return {"success": False, "channel": ch_type, "message": f"Unknown channel type: {ch_type}"}

    kwargs = dict(channel_config)
    kwargs.pop("type", None)
    kwargs.pop("id", None)
    kwargs.pop("name", None)
    kwargs.pop("enabled", None)
    kwargs.pop("created_at", None)

    # SES needs recipients list
    if ch_type == "ses":
        recipients = kwargs.pop("recipients", [])
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        kwargs["recipients"] = recipients

    try:
        return sender(title=title, content=content, **kwargs)
    except Exception as e:
        return {"success": False, "channel": ch_type, "message": str(e)}


def send_to_channels(channels: list, title: str, content: str) -> list:
    """Send to multiple channels, return results."""
    results = []
    for ch in channels:
        if ch.get("enabled", True):
            result = send_notification(ch, title, content)
            results.append(result)
    return results
