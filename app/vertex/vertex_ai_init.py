"""Vertex 启动初始化与按项目、位置、凭证隔离的 SDK 客户端池。"""
from google import genai
from app.vertex.credentials_manager import (
    CredentialManager,
    credential_identity,
    clear_token_cache,
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

from app.vertex import client_pool


def _get_client_from_pool(project_id: str, creds, location: str = "global"):
    # Projects may have multiple service accounts with different permissions.
    identity = credential_identity(creds)
    try:
        return client_pool.get_client(("sa", project_id, location, identity), lambda: genai.Client(
            vertexai=True, credentials=creds, project=project_id, location=location,
        ))
    except Exception as exc:
        vertex_log("error", f"为项目 {project_id} 创建 Vertex 客户端失败：{exc}")
        return None


def reset_client_pool():
    """Retire old credentials; ongoing responses retain their client until closed."""
    clear_token_cache()
    client_pool.reset()
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
        if credentials_json_str and credentials_json_str.strip():
            json_objects = parse_multiple_json_credentials(credentials_json_str)
            success_count = credential_manager.load_credentials_from_json_list(json_objects)
            vertex_log("info", f"从 GOOGLE_CREDENTIALS_JSON 新增 {success_count} 个凭证")
        else:
            vertex_log("info", "GOOGLE_CREDENTIALS_JSON 未配置或为空")

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
