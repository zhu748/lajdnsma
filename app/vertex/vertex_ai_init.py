"""Vertex 启动初始化与 genai.Client 池（按 project_id 键控，TTL 50min）。"""
import json
import time
from google import genai
from app.vertex.credentials_manager import (
    CredentialManager,
    parse_multiple_json_credentials,
)
import app.vertex.config as app_config
from app.vertex.model_loader import (
    refresh_models_config_cache,
)  # Import new model loader function
from app.utils.logging import vertex_log

# VERTEX_EXPRESS_MODELS list is now dynamically loaded via model_loader
# The constant VERTEX_EXPRESS_MODELS previously defined here is removed.
# Consumers should use get_vertex_express_models() from model_loader.

# 全局客户端池：按 project_id 缓存 genai.Client（原 global_fallback_client
# 单例随死代码 get_vertex_ai_client 一并移除，统一走池）

# Hardened: client pool keyed by project_id, so we don't re-create the
# genai.Client (and re-negotiate TLS) on every request.  TTL is 50
# minutes — tokens last 1h, so we refresh well before expiry.
_client_pool: dict[str, tuple] = {}  # {project_id: (client, created_at)}
_CLIENT_TTL_SECONDS = 50 * 60


def _get_client_from_pool(project_id: str, creds, location: str = "global"):
    """Get or create a cached genai.Client for a given project_id.

    Returns the cached client if it's still fresh (< TTL old), otherwise
    creates and caches a new one.  This dramatically reduces both CPU
    (RSA key parsing) and the number of distinct TLS sessions we open
    against Vertex — both of which are anti-fingerprint improvements.
    """
    now = time.time()
    cached = _client_pool.get(project_id)
    if cached is not None:
        client, created_at = cached
        if now - created_at < _CLIENT_TTL_SECONDS:
            return client
    try:
        client = genai.Client(
            vertexai=True,
            credentials=creds,
            project=project_id,
            location=location,
        )
        _client_pool[project_id] = (client, now)
        return client
    except Exception as e:
        vertex_log("error", f"为项目 {project_id} 创建 Vertex 客户端失败：{e}")
        return None


def reset_client_pool():
    """清空全局客户端池（凭证更新后由 dashboard 调用）。"""
    _client_pool.clear()
    vertex_log("info", "Vertex 客户端池已重置")


async def init_vertex_ai(credential_manager=None) -> bool:  # Made async
    """
    Initializes the credential manager with credentials from GOOGLE_CREDENTIALS_JSON (if provided)
    and verifies if any credentials (environment or file-based through the manager) are available.
    The CredentialManager itself handles loading file-based credentials upon its instantiation.
    This function primarily focuses on augmenting the manager with env var credentials.

    Returns True if any credentials seem available in the manager, False otherwise.
    """
    # 如果未传入credential_manager，则创建一个新的实例
    if credential_manager is None:
        vertex_log("info", f"正在创建凭证管理器")
        credential_manager = CredentialManager()
    else:
        vertex_log("info", f"使用传入的凭证管理器")

    try:
        credentials_json_str = app_config.GOOGLE_CREDENTIALS_JSON
        env_creds_loaded_into_manager = False

        # 更严格的检查：确保不为None、不为空字符串、且去除空白后不为空
        if credentials_json_str and credentials_json_str.strip():
            vertex_log(
                "info",
                f"检测到 GOOGLE_CREDENTIALS_JSON，正在加载到凭证管理器",
            )
            try:
                # Attempt 1: Parse as multiple JSON objects
                json_objects = parse_multiple_json_credentials(credentials_json_str)
                if json_objects:
                    vertex_log(
                        "debug",
                        f"已从 GOOGLE_CREDENTIALS_JSON 解析 {len(json_objects)} 个候选凭证",
                    )
                    success_count = credential_manager.load_credentials_from_json_list(
                        json_objects
                    )
                    if success_count > 0:
                        vertex_log(
                            "info",
                            f"已从 GOOGLE_CREDENTIALS_JSON 加载 {success_count} 个凭证",
                        )
                        env_creds_loaded_into_manager = True

                # Attempt 2: If multiple parsing/loading didn't add any, try parsing/loading as a single JSON object
                if not env_creds_loaded_into_manager:
                    vertex_log(
                        "debug",
                        f"多个 JSON 凭证加载为空或未成功，尝试按单个 JSON 凭证加载",
                    )
                    try:
                        credentials_info = json.loads(credentials_json_str)
                        # Basic validation (CredentialManager's add_credential_from_json does more thorough validation)

                        if isinstance(credentials_info, dict) and all(
                            field in credentials_info
                            for field in [
                                "type",
                                "project_id",
                                "private_key_id",
                                "private_key",
                                "client_email",
                            ]
                        ):
                            if credential_manager.add_credential_from_json(
                                credentials_info
                            ):
                                vertex_log(
                                    "info",
                                    f"已将单个 JSON 凭证加载到管理器",
                                )
                                # env_creds_loaded_into_manager = True # Redundant, as this block is conditional on it being False
                            else:
                                vertex_log(
                                    "warning",
                                    f"通过管理器添加单个 JSON 凭证失败",
                                )
                        else:
                            vertex_log(
                                "warning",
                                f"单个 JSON 凭证不是有效对象，或缺少必填字段",
                            )
                    except json.JSONDecodeError as single_json_err:
                        vertex_log(
                            "warning",
                            f"GOOGLE_CREDENTIALS_JSON 无法解析为单个 JSON 对象：{single_json_err}",
                        )
                    except Exception as single_load_err:
                        vertex_log(
                            "warning",
                            f"向管理器加载单个 JSON 凭证失败：{single_load_err}",
                        )
            except Exception as e_json_env:
                # This catches errors from parse_multiple_json_credentials or load_credentials_from_json_list
                vertex_log(
                    "warning",
                    f"处理 GOOGLE_CREDENTIALS_JSON 配置失败：{e_json_env}",
                )
        else:
            vertex_log(
                "info",
                f"GOOGLE_CREDENTIALS_JSON 未配置或为空",
            )

        # Attempt to pre-warm the model configuration cache
        vertex_log(
            "info", f"启动时预热模型配置缓存"
        )
        models_loaded_successfully = await refresh_models_config_cache()
        if models_loaded_successfully:
            vertex_log("info", f"模型配置缓存预热成功")
        else:
            vertex_log(
                "warning",
                f"启动预热模型配置失败，将在首次请求时延迟加载",
            )
            # We don't necessarily fail the entire init_vertex_ai if model list fetching fails,
            # as credential validation might still be important, and model list can be fetched later.

        # CredentialManager's __init__ calls load_credentials_list() for files.
        # refresh_credentials_list() re-scans files and combines with in-memory (already includes env creds if loaded above).
        # The return value of refresh_credentials_list indicates if total > 0
        if credential_manager.refresh_credentials_list():
            total_creds = credential_manager.get_total_credentials()
            vertex_log(
                "info",
                f"凭证管理器当前有 {total_creds} 个可用凭证",
            )

            # Optional: Attempt to validate one of the credentials by creating a temporary client.
            # This adds a check that at least one credential is functional.
            vertex_log(
                "info",
                f"正在通过临时客户端验证一个凭证",
            )
            temp_creds_val, temp_project_id_val = (
                credential_manager.get_random_credentials()
            )
            if temp_creds_val and temp_project_id_val:
                try:
                    _ = genai.Client(
                        vertexai=True,
                        credentials=temp_creds_val,
                        project=temp_project_id_val,
                        location="global",
                    )
                    vertex_log(
                        "info",
                        f"项目 {temp_project_id_val} 的凭证验证成功，初始化检查通过",
                    )
                    return True
                except Exception as e_val:
                    vertex_log(
                        "warning",
                        f"通过临时客户端验证凭证失败：{e_val}；请检查凭证有效性",
                    )
                    # Still return True if credentials exist, as the app might still function with other valid credentials.
                    # The per-request client creation will be the ultimate test for a specific credential.
                    return (
                        True  # Credentials exist, even if one failed validation here.
                    )
            elif (
                total_creds > 0
            ):  # Credentials listed but get_random_credentials returned None
                vertex_log(
                    "warning",
                    f"管理器报告有 {total_creds} 个凭证，但无法取出供验证的凭证，请检查配置",
                )
                return True  # Still, credentials are listed.
            else:  # No creds from get_random_credentials and total_creds is 0
                vertex_log(
                    "error",
                    f"已尝试全部凭证来源，仍无可用凭证",
                )
                return False  # No credentials reported by manager and get_random_credentials gave none.
        else:
            vertex_log(
                "error",
                f"处理完全部来源后仍无可用凭证",
            )
            return False

    except Exception as e:
        vertex_log("error", f"Vertex 凭证初始化发生严重错误：{e}")
        return False
