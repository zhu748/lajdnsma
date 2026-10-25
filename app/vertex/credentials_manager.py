"""多凭证管理：文件凭证 + JSON 字符串凭证 + token 缓存（50min TTL）。"""
import os
import glob
import random
import json
import time
from typing import List, Dict, Any
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account
import app.vertex.config as app_config  # Changed from relative
from app.utils.logging import vertex_log


# Hardened token cache: per-project_id (token, expiry_epoch).
# service_account tokens last 1h; we cache for 50min to refresh before
# expiry.  Avoiding token refresh on every request is important because
# every refresh is recorded by Cloud Audit Log as a separate
# `service_account.token.refresh` event — high refresh rate is itself an
# anomalous-account fingerprint.
_TOKEN_CACHE: dict[str, tuple] = {}  # {project_id: (token, expiry_epoch)}
_TOKEN_TTL_SECONDS = 50 * 60  # 50 minutes


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


# Helper function to parse multiple JSONs from a string
def parse_multiple_json_credentials(json_str: str) -> List[Dict[str, Any]]:
    """
    Parse multiple JSON objects from a string separated by commas.
    Format expected: {json_object1},{json_object2},...
    Returns a list of parsed JSON objects.
    """
    # 检查输入是否为None或空字符串
    if not json_str or not json_str.strip():
        vertex_log(
            "debug", f"凭证 JSON 为空，无需解析"
        )
        return []

    credentials_list = []
    nesting_level = 0
    current_object_start = -1

    for i, char in enumerate(json_str):
        if char == "{":
            if nesting_level == 0:
                current_object_start = i
            nesting_level += 1
        elif char == "}":
            if nesting_level > 0:
                nesting_level -= 1
                if nesting_level == 0 and current_object_start != -1:
                    # Found a complete top-level JSON object
                    json_object_str = json_str[current_object_start : i + 1]
                    try:
                        credentials_info = json.loads(json_object_str)
                        # Basic validation for service account structure
                        required_fields = [
                            "type",
                            "project_id",
                            "private_key_id",
                            "private_key",
                            "client_email",
                        ]
                        if all(field in credentials_info for field in required_fields):
                            credentials_list.append(credentials_info)
                            vertex_log(
                                "debug", f"凭证 JSON 对象解析成功"
                            )
                        else:
                            vertex_log(
                                "warning",
                                f"凭证 JSON 缺少必填字段，已跳过该对象",
                            )
                    except json.JSONDecodeError as e:
                        vertex_log(
                            "error",
                            f"凭证 JSON 解析失败：{e}",
                        )
                    current_object_start = -1  # Reset for the next object
            else:
                # Found a closing brace without a matching open brace in scope, might indicate malformed input
                vertex_log(
                    "warning",
                    f"位置 {i} 出现多余的右花括号，JSON 格式可能不正确",
                )

    if nesting_level != 0:
        vertex_log(
            "warning",
            f"凭证 JSON 的花括号未配对，剩余嵌套层数 {nesting_level}",
        )

    vertex_log(
        "debug",
        f"已解析 {len(credentials_list)} 个凭证对象",
    )
    return credentials_list


def _refresh_auth(credentials):
    """Helper function to refresh GCP token.

    Hardened: caches the refreshed token in `_TOKEN_CACHE` keyed by
    project_id so we don't refresh on every request (every refresh is
    a Cloud Audit Log event; high refresh rate is an anomalous-account
    fingerprint).
    """
    if not credentials:
        vertex_log("error", f"无法刷新认证令牌：未提供凭证")
        return None
    project_id_for_log = getattr(credentials, "project_id", "Unknown")

    # Check cache first
    cached = _TOKEN_CACHE.get(project_id_for_log)
    if cached is not None:
        token, expiry = cached
        if time.time() < expiry:
            # Apply cached token back to the credentials object so the
            # downstream google.genai client picks it up.
            try:
                credentials.token = token
            except Exception:
                pass
            return token

    try:
        credentials.refresh(AuthRequest())
        token = credentials.token
        # Cache for 50 minutes (real tokens last 1h)
        _TOKEN_CACHE[project_id_for_log] = (token, time.time() + _TOKEN_TTL_SECONDS)
        vertex_log(
            "info", f"项目 {project_id_for_log} 的认证令牌已刷新并缓存"
        )
        return token
    except Exception as e:
        # Cleanup: the except block used to re-assign project_id_for_log to
        # the same value it already holds — dead store, removed.
        vertex_log(
            "error", f"项目 {project_id_for_log} 的 GCP 认证令牌刷新失败：{e}"
        )
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

    def add_credential_from_json(self, credentials_info: Dict[str, Any]) -> bool:
        """
        Add a credential from a JSON object to the manager's in-memory list.

        Args:
            credentials_info: Dict containing service account credentials

        Returns:
            bool: True if credential was added successfully, False otherwise
        """
        try:
            # Validate structure again before creating credentials object
            required_fields = [
                "type",
                "project_id",
                "private_key_id",
                "private_key",
                "client_email",
            ]
            if not all(field in credentials_info for field in required_fields):
                vertex_log(
                    "warning",
                    f"凭证 JSON 缺少必填字段，已跳过",
                )
                return False

            credentials = service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            project_id = credentials.project_id
            vertex_log(
                "debug",
                f"已根据 JSON 创建凭证对象，项目={project_id}",
            )

            # Store the credentials object and project ID
            self.in_memory_credentials.append(
                {
                    "credentials": credentials,
                    "project_id": project_id,
                    "source": "json_string",  # Add source for clarity
                }
            )
            vertex_log(
                "info",
                f"已将项目 {project_id} 的 JSON 凭证加入管理器",
            )
            return True
        except Exception as e:
            vertex_log(
                "error", f"根据 JSON 创建凭证失败：{e}"
            )
            return False

    def load_credentials_from_json_list(self, json_list: List[Dict[str, Any]]) -> int:
        """
        Load multiple credentials from a list of JSON objects into memory.

        Args:
            json_list: List of dicts containing service account credentials

        Returns:
            int: Number of credentials successfully loaded
        """
        # Avoid duplicates if called multiple times
        existing_projects = {cred["project_id"] for cred in self.in_memory_credentials}
        success_count = 0
        newly_added_projects = set()

        for credentials_info in json_list:
            project_id = credentials_info.get("project_id")
            # Check if this project_id from JSON exists in files OR already added from JSON
            is_duplicate_file = any(
                os.path.basename(f) == f"{project_id}.json"
                for f in self.credentials_files
            )  # Basic check
            is_duplicate_mem = (
                project_id in existing_projects or project_id in newly_added_projects
            )

            if project_id and not is_duplicate_file and not is_duplicate_mem:
                if self.add_credential_from_json(credentials_info):
                    success_count += 1
                    newly_added_projects.add(project_id)
            elif project_id:
                vertex_log(
                    "debug",
                    f"项目 {project_id} 的凭证已存在，跳过重复项",
                )

        if success_count > 0:
            vertex_log(
                "info",
                f"已从 JSON 列表新增 {success_count} 个内存凭证",
            )
        return success_count

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
