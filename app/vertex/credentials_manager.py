"""多凭证管理：文件凭证 + JSON 字符串凭证 + token 缓存（50min TTL）。"""
import os
import glob
import random
import json
import time
import threading
from collections import OrderedDict
from typing import List, Dict, Any
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account
import app.vertex.config as app_config  # Changed from relative
from app.utils.logging import vertex_log


# Tokens are identities, not project-wide capabilities. Cache by account/key,
# scopes and delegated subject; striped locks coalesce concurrent refreshes.
_TOKEN_CACHE = OrderedDict()
_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_REFRESH_LOCKS = [threading.Lock() for _ in range(32)]
_TOKEN_TTL_SECONDS = 50 * 60
_MAX_CACHED_TOKENS = 512


def credential_identity(credentials):
    email = getattr(credentials, "service_account_email", None)
    key_id = getattr(getattr(credentials, "signer", None), "key_id", None)
    return (
        getattr(credentials, "project_id", None), email, key_id,
        tuple(getattr(credentials, "scopes", None) or ()),
        getattr(credentials, "_subject", None),
        getattr(credentials, "quota_project_id", None),
        id(credentials) if not email and not key_id else None,
    )


def clear_token_cache():
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()


def get_credential_manager(fastapi_request) -> "CredentialManager":
    """优先取 app.state 中由启动流程注入的凭证管理器，缺失时兜底新建。

    chat_api 与 models_api 的共享逻辑（此前两处各自内联实现）。
    """
    try:
        credential_manager_instance = fastapi_request.app.state.credential_manager
        vertex_log("info", f"使用应用状态中的凭证管理器")
    except AttributeError:
        vertex_log(
            "warning",
            f"应用状态中没有凭证管理器，正在新建",
        )
        credential_manager_instance = CredentialManager()
    return credential_manager_instance


def parse_credentials_json(json_str: str) -> List[Dict[str, Any]]:
    """Parse complete objects, arrays, or comma/whitespace separated objects.

    JSONDecoder respects braces inside strings. Never accept a valid prefix of
    a malformed upload or silently discard an invalid entry.
    """
    if not isinstance(json_str, str) or not json_str.strip():
        raise ValueError("凭证 JSON 不能为空")
    def invalid_constant(value):
        raise ValueError("凭证 JSON 不允许非有限数字")
    decoder = json.JSONDecoder(parse_constant=invalid_constant)
    text = json_str.strip()
    objects = []
    position = 0
    try:
        while position < len(text):
            item, end = decoder.raw_decode(text, position)
            if isinstance(item, list):
                if position != 0 or text[end:].strip():
                    raise ValueError("凭证数组必须是完整 JSON")
                objects.extend(item)
            else:
                objects.append(item)
            position = end
            while position < len(text) and text[position].isspace():
                position += 1
            if position < len(text) and text[position] == ',':
                position += 1
                while position < len(text) and text[position].isspace():
                    position += 1
                if position == len(text):
                    raise ValueError("凭证 JSON 末尾不能有逗号")
    except json.JSONDecodeError as exc:
        raise ValueError(f"凭证 JSON 格式无效（位置 {exc.pos}）") from None
    if not objects or any(not isinstance(item, dict) for item in objects):
        raise ValueError("凭证必须是非空 JSON 对象或对象数组")
    return objects


def parse_multiple_json_credentials(json_str: str) -> List[Dict[str, Any]]:
    """Startup compatibility wrapper: invalid configuration yields no entries."""
    if not json_str or not json_str.strip():
        return []
    try:
        return parse_credentials_json(json_str)
    except ValueError as exc:
        vertex_log("warning", str(exc))
        return []


def _credential_entry(info: Dict[str, Any]) -> Dict[str, Any]:
    required = ("project_id", "private_key_id", "private_key", "client_email", "token_uri")
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("凭证类型必须为 service_account")
    if any(not isinstance(info.get(key), str) or not info[key].strip() for key in required):
        raise ValueError("凭证缺少有效的服务账号必填字段")
    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception:
        # SDK exceptions may include private key material; never echo them.
        raise ValueError("服务账号凭证或私钥无效") from None
    return {"credentials": credentials, "project_id": credentials.project_id, "source": "json_string"}


def prepare_json_credentials(json_str: str) -> List[Dict[str, Any]]:
    """Validate every entry locally before changing any active credentials."""
    prepared = []
    identities = set()
    for info in parse_credentials_json(json_str):
        entry = _credential_entry(info)
        identity = credential_identity(entry["credentials"])
        if identity not in identities:
            prepared.append(entry)
            identities.add(identity)
    return prepared


def _refresh_auth(credentials):
    """Reuse only a matching principal's token; refresh once per concurrent group."""
    if not credentials:
        return None
    key = credential_identity(credentials)
    with _TOKEN_REFRESH_LOCKS[hash(key) % len(_TOKEN_REFRESH_LOCKS)]:
        with _TOKEN_CACHE_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached is not None and time.time() < cached[1]:
                _TOKEN_CACHE.move_to_end(key)
                credentials.token = cached[0]
                if cached[2] is not None:
                    credentials.expiry = cached[2]
                return cached[0]
        try:
            credentials.refresh(AuthRequest())
            token = credentials.token
            if not token:
                return None
            expiry = getattr(credentials, "expiry", None)
            valid_until = time.time() + _TOKEN_TTL_SECONDS
            if expiry is not None:
                # google-auth uses naive UTC expiries in this pinned version.
                from datetime import timezone
                expiry_utc = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
                valid_until = min(valid_until, expiry_utc.timestamp() - 60)
            with _TOKEN_CACHE_LOCK:
                _TOKEN_CACHE[key] = (token, valid_until, expiry)
                _TOKEN_CACHE.move_to_end(key)
                while len(_TOKEN_CACHE) > _MAX_CACHED_TOKENS:
                    _TOKEN_CACHE.popitem(last=False)
            return token
        except Exception as exc:
            vertex_log("error", f"GCP 认证令牌刷新失败：{exc}")
            return None


# Credential Manager for handling multiple service accounts
class CredentialManager:
    def __init__(self):  # default_credentials_dir is now handled by config
        # Use CREDENTIALS_DIR from config
        self.credentials_dir = app_config.CREDENTIALS_DIR
        self.credentials_files = []
        # Cleanup: removed unused `self.current_index` — rotation is done by
        # random.shuffle in get_random_credentials(), this attribute was
        # written once at init and never read anywhere.
        self.credentials = None
        self.project_id = None
        # New: Store credentials loaded directly from JSON objects
        self.in_memory_credentials: List[Dict[str, Any]] = []
        self.load_credentials_list()  # Load file-based credentials initially

    def clear_json_string_credentials(self) -> int:
        """
        清除所有通过JSON字符串加载的内存凭证。
        返回清除的凭证数量。
        """
        count_before = len(self.in_memory_credentials)
        # 只保留不是从json字符串加载的凭证
        self.in_memory_credentials = [
            cred
            for cred in self.in_memory_credentials
            if cred.get("source") != "json_string"
        ]
        count_after = len(self.in_memory_credentials)
        removed_count = count_before - count_after
        vertex_log(
            "debug",
            f"从CredentialManager中清除了{removed_count}个由JSON字符串加载的凭证",
        )
        return removed_count

    def replace_json_string_credentials(self, prepared: List[Dict[str, Any]]) -> None:
        """Install an already validated batch in one assignment; preserve other sources."""
        retained = [item for item in self.in_memory_credentials if item.get("source") != "json_string"]
        self.in_memory_credentials = retained + prepared
        self.credentials = None
        self.project_id = None

    def add_credential_from_json(self, credentials_info: Dict[str, Any]) -> bool:
        try:
            entry = _credential_entry(credentials_info)
        except ValueError as exc:
            vertex_log("warning", str(exc))
            return False
        identity = credential_identity(entry["credentials"])
        if any(credential_identity(item["credentials"]) == identity for item in self.in_memory_credentials):
            return False
        self.in_memory_credentials.append(entry)
        return True

    def load_credentials_from_json_list(self, json_list: List[Dict[str, Any]]) -> int:
        # Project IDs do not identify a principal. Keep distinct accounts and
        # rotated signing keys even when they belong to the same project.
        count = sum(self.add_credential_from_json(info) for info in json_list)
        if count:
            vertex_log("info", f"已从 JSON 列表新增 {count} 个内存凭证")
        return count

    def load_credentials_list(self):
        """Load the list of available credential files"""
        # Look for all .json files in the credentials directory
        pattern = os.path.join(self.credentials_dir, "*.json")
        self.credentials_files = glob.glob(pattern)

        if not self.credentials_files:
            # vertex_log('info', f"No credential files found in {self.credentials_dir}")
            pass  # Don't return False yet, might have in-memory creds
        else:
            vertex_log(
                "info",
                f"发现 {len(self.credentials_files)} 个凭证文件：{[os.path.basename(f) for f in self.credentials_files]}",
            )

        # Check total credentials
        return self.get_total_credentials() > 0

    def refresh_credentials_list(self):
        """Refresh the list of credential files and return if any credentials exist"""
        old_file_count = len(self.credentials_files)
        self.load_credentials_list()  # Reloads file list
        new_file_count = len(self.credentials_files)

        if old_file_count != new_file_count:
            vertex_log(
                "info",
                f"凭证文件数量已更新：{old_file_count} → {new_file_count}",
            )

        # Total credentials = files + in-memory
        total_credentials = self.get_total_credentials()
        vertex_log(
            "debug", f"凭证刷新检查：当前共 {total_credentials} 个可用凭证"
        )
        return total_credentials > 0

    def get_total_credentials(self):
        """Returns the total number of credentials (file + in-memory)."""
        return len(self.credentials_files) + len(self.in_memory_credentials)

    def get_random_credentials(self):
        """
        Get a random credential (file or in-memory) and load it.
        Tries each available credential source at most once in a random order.
        """
        all_sources = []
        # Add file paths (as type 'file')
        for file_path in self.credentials_files:
            all_sources.append({"type": "file", "value": file_path})

        # Add in-memory credentials (as type 'memory_object')
        # Assuming self.in_memory_credentials stores dicts like {'credentials': cred_obj, 'project_id': pid, 'source': 'json_string'}
        for idx, mem_cred_info in enumerate(self.in_memory_credentials):
            all_sources.append(
                {"type": "memory_object", "value": mem_cred_info, "original_index": idx}
            )

        if not all_sources:
            vertex_log(
                "warning",
                f"无可供选择的凭证：文件和内存凭证均为空",
            )
            return None, None

        random.shuffle(all_sources)  # Shuffle to try in a random order

        for source_info in all_sources:
            source_type = source_info["type"]

            if source_type == "file":
                file_path = source_info["value"]
                vertex_log(
                    "debug",
                    f"正在加载凭证文件：{os.path.basename(file_path)}",
                )
                try:
                    credentials = service_account.Credentials.from_service_account_file(
                        file_path,
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    )
                    project_id = credentials.project_id
                    vertex_log(
                        "info",
                        f"凭证文件 {os.path.basename(file_path)} 加载成功，项目={project_id}",
                    )
                    self.credentials = credentials  # Cache last successfully loaded
                    self.project_id = project_id
                    return credentials, project_id
                except Exception as e:
                    vertex_log(
                        "error",
                        f"凭证文件 {os.path.basename(file_path)} 加载失败：{e}；尝试下一个来源",
                    )
                    continue  # Try next source

            elif source_type == "memory_object":
                mem_cred_detail = source_info["value"]
                # The 'credentials' object is already a service_account.Credentials instance
                credentials = mem_cred_detail.get("credentials")
                project_id = mem_cred_detail.get("project_id")

                if credentials and project_id:
                    vertex_log(
                        "info",
                        f"使用项目 {project_id} 的内存凭证，来源={mem_cred_detail.get('source', '未知')}",
                    )
                    # Here, we might want to ensure the credential object is still valid if it can expire
                    # For service_account.Credentials from_service_account_info, they typically don't self-refresh
                    # in the same way as ADC, but are long-lived based on the private key.
                    # If validation/refresh were needed, it would be complex here.
                    # For now, assume it's usable if present.
                    self.credentials = (
                        credentials  # Cache last successfully loaded/used
                    )
                    self.project_id = project_id
                    return credentials, project_id
                else:
                    vertex_log(
                        "warning",
                        f"第 {source_info.get('original_index', '未知')} 个内存凭证缺少凭证对象或项目编号，已跳过",
                    )
                    continue  # Try next source

        vertex_log("warning", f"所有凭证来源均加载失败")
        return None, None
