"""Version check — hardened.

Previously this module did a blocking `requests.get(...)` against a
hardcoded GitHub raw URL containing the upstream project name.  Two
problems:

1. The URL path leaked the project identity to GitHub's raw CDN.
2. `requests` defaults to `python-requests/x.x.x` UA, bypassing the
   shared httpx client used everywhere else in the project.

Hardening:
* The remote URL is now configurable via the `VERSION_CHECK_URL` env
  var.  If unset (default), the check is fully skipped — no outbound
  request, no fingerprint, no startup delay.
* When enabled, the call goes through the shared async httpx client
  with a realistic SDK-style UA picked by `pick_user_agent(None)`.
* The remote URL is not echoed back to the client; only the version
  comparison result is.
"""

from __future__ import annotations

import os
from pathlib import Path

import app.config.settings as settings
from app.utils.logging import log
from app.utils.stealth import pick_user_agent

# version.txt 位于仓库根目录；用 __file__ 相对定位而非 "./version.txt"，
# 后者依赖进程 CWD（uvicorn 从其他目录启动时读不到，dashboard 会永远
# 显示 0.0.0 —— 与 main.py 里 StaticFiles 的 BASE_DIR 修复同理）。
_VERSION_FILE = Path(__file__).resolve().parents[2] / "version.txt"


async def check_version() -> bool:
    """Check whether a newer version is available.

    Returns True if the remote version is strictly greater than the
    local version (or if no local version is parsed and the remote is
    reachable).  Returns False otherwise or on any error.
    """
    # Read local version first — this always happens regardless of
    # whether remote check is enabled, so the dashboard can still
    # display the local version.
    try:
        with open(_VERSION_FILE, "r", encoding="utf-8") as f:
            version_line = f.read().strip()
            settings.version["local_version"] = (
                version_line.split("=")[1] if "=" in version_line else "0.0.0"
            )
    except Exception as e:
        log("warning", f"读取本地版本失败: {e}")
        settings.version["local_version"] = settings.version.get(
            "local_version", "0.0.0"
        )

    # Remote check is opt-in to avoid the hardcoded-URL fingerprint.
    remote_url = os.environ.get("VERSION_CHECK_URL", "").strip()
    if not remote_url:
        # Skipped entirely — no outbound request.
        log(
            "info",
            f"版本检查: 本地版本 {settings.version['local_version']}, 远程检查已通过 VERSION_CHECK_URL 关闭",
        )
        return False

    try:
        # Lazy import to keep the hot path clean when remote check is off.
        from app.utils.http_client import get_async_client

        client = await get_async_client()
        headers = {
            "User-Agent": pick_user_agent(None),
            "Accept": "text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        response = await client.get(remote_url, headers=headers, timeout=5.0)
        if response.status_code == 200:
            version_line = response.text.strip()
            settings.version["remote_version"] = (
                version_line.split("=")[1] if "=" in version_line else "0.0.0"
            )
            # Compare version numbers.
            local_parts = [
                int(x) if x.isdigit() else 0
                for x in settings.version["local_version"].split(".")
            ]
            remote_parts = [
                int(x) if x.isdigit() else 0
                for x in settings.version["remote_version"].split(".")
            ]
            while len(local_parts) < len(remote_parts):
                local_parts.append(0)
            while len(remote_parts) < len(local_parts):
                remote_parts.append(0)
            settings.version["has_update"] = False
            for i in range(len(local_parts)):
                if remote_parts[i] > local_parts[i]:
                    settings.version["has_update"] = True
                    break
                elif remote_parts[i] < local_parts[i]:
                    break
            log(
                "info",
                f"版本检查: 本地版本 {settings.version['local_version']}, 远程版本 {settings.version['remote_version']}, 有更新: {settings.version['has_update']}",
            )
        else:
            log(
                "warning",
                f"无法获取远程版本信息，HTTP状态码: {response.status_code}",
            )
    except Exception as e:
        log("error", f"版本检查失败: {e}")

    return bool(settings.version.get("has_update", False))
