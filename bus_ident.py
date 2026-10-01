"""Agent identity shared by the service hook adapter, the CLI and the MCP server, so ids always match."""
import hashlib
import os
import re

ACCOUNT = re.compile(r"^\.claude(?:-([A-Za-z0-9_]+))?$", re.I)
TOOLS = ("claude-code", "codex", "claude-desktop", "claude-desktop-cloud", "other")


def segments(path):
    return [part for part in re.split(r"[\\/]+", str(path or "")) if part]


def account_from_path(path):
    """`~/.claude` -> main, `~/.claude-pecunia` -> pecunia; None when no config-dir segment is present."""
    for part in segments(path):
        match = ACCOUNT.match(part)
        if match:
            return (match.group(1) or "main").lower()
    return None


def folder_name(cwd):
    parts = segments(cwd)
    return parts[-1].rstrip(":").lower() if parts else ""


def short_id(session_id):
    hexchars = re.sub(r"[^0-9a-f]", "", str(session_id or "").lower())
    return hexchars[:8] if len(hexchars) >= 8 else hashlib.sha1(str(session_id).encode()).hexdigest()[:8]


def claude_identity(session_id, cwd=None, transcript_path=None, config_dir=None):
    """Bus identity of a Claude Code session: id claude-<8 hex of session_id>, name/folder from cwd, account from paths."""
    folder = folder_name(cwd)
    account = account_from_path(transcript_path) or account_from_path(config_dir) or "unknown"
    return {"id": "claude-" + short_id(session_id), "name": folder or None, "folder": folder or None, "account": account,
            "tool": "claude-code", "session_id": str(session_id) if session_id else None,
            "cwd": str(cwd) if cwd else None}


def env_identity(environ=None, cwd=None):
    """Identity of the Claude Code session running this process (Bash tool / MCP server), or None outside Claude Code."""
    env = os.environ if environ is None else environ
    session_id = env.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    config_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return claude_identity(session_id, cwd or env.get("CLAUDE_PROJECT_DIR") or os.getcwd(), None, config_dir)


def guess_tool(agent_id):
    low = str(agent_id).lower()
    for prefix, tool in (("study-claude", "claude-desktop-cloud"), ("claude-desktop-cloud", "claude-desktop-cloud"),
                         ("claude-desktop", "claude-desktop"), ("desktop-", "claude-desktop"), ("claude-", "claude-code"),
                         ("codex", "codex")):
        if low.startswith(prefix):
            return tool
    return "other"
