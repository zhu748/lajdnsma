import json
import os
import inspect
import pathlib
import threading
from app.config import settings
from app.utils.logging import log

# 序列化 settings.json 时的互斥锁。save_settings 会在事件循环线程和
# 后台任务中被并发触发（改配置 / 密钥检测完成 / 每日维护），虽然单个
# 调用本身不 await，但 threading.Lock 成本极低且能防御任何线程上下文
# （例如未来从 executor 里调用）造成的交错写。
_save_lock = threading.Lock()

# 定义不应该被保存或加载的配置项
# Hardened: previously only PASSWORD/WEB_PASSWORD were excluded.  Added
# the upstream credential fields (GEMINI_API_KEYS, GOOGLE_CREDENTIALS_JSON,
# VERTEX_EXPRESS_API_KEY) so they don't get written to disk in plaintext
# when an operator enables ENABLE_STORAGE.
EXCLUDED_SETTINGS = [
    "STORAGE_DIR",
    "ENABLE_STORAGE",
    "BASE_DIR",
    "PASSWORD",
    "WEB_PASSWORD",
    "GEMINI_API_KEYS",
    "GOOGLE_CREDENTIALS_JSON",
    "VERTEX_EXPRESS_API_KEY",
    # Hardening: INVALID_API_KEYS previously was persisted to disk
    # in plaintext.  The list of failed/invalid Gemini API keys is
    # just as sensitive as the working key list — anyone with read
    # access to settings.json would gain the full set of keys ever
    # tried.  Keep it in-memory only.
    "INVALID_API_KEYS",
    "WHITELIST_MODELS",
    "BLOCKED_MODELS",
    "DEFAULT_BLOCKED_MODELS",
    "PUBLIC_MODE",
    "DASHBOARD_URL",
    "version",
]


def save_settings():
    """
    将settings中所有的从os.environ.get获取的配置保存到JSON文件中，
    但排除特定的配置项
    """
    if settings.ENABLE_STORAGE:
        # 确保存储目录存在
        storage_dir = pathlib.Path(settings.STORAGE_DIR)
        storage_dir.mkdir(parents=True, exist_ok=True)

        # 设置JSON文件路径
        settings_file = storage_dir / "settings.json"

        # 获取settings模块中的所有变量
        settings_dict = {}
        for name, value in inspect.getmembers(settings):
            # 跳过内置和私有变量，以及函数/模块/类，以及排除列表中的配置项
            if (
                not name.startswith("_")
                and not inspect.isfunction(value)
                and not inspect.ismodule(value)
                and not inspect.isclass(value)
                and name not in EXCLUDED_SETTINGS
            ):
                # 尝试将可序列化的值添加到字典中
                try:
                    json.dumps({name: value})  # 测试是否可序列化
                    settings_dict[name] = value
                except (TypeError, OverflowError):
                    # 如果不可序列化，则跳过
                    continue
        log("info", f"保存设置到JSON文件: {settings_file}")

        # 原子写入：先写临时文件再 os.replace。
        # 旧实现直接以 "w" 模式覆写 settings.json —— 进程在写入中途崩溃
        # （或磁盘满）会留下截断的 JSON，下次启动 load_settings 失败导致
        # 全部持久化配置静默丢失；并发写则可能交错。os.replace 在同一
        # 文件系统上是原子的，读方永远只能看到完整的旧版或新版。
        with _save_lock:
            tmp_file = storage_dir / "settings.json.tmp"
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(settings_dict, f, ensure_ascii=False, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_file, settings_file)
            finally:
                # 正常路径下 os.replace 已把临时文件移走；这里兜底清理
                # 写入失败时残留的 .tmp 文件。
                if tmp_file.exists():
                    try:
                        tmp_file.unlink()
                    except OSError:
                        pass

        return settings_file


def load_settings():
    """
    从JSON文件中加载设置并更新settings模块，
    排除特定的配置项，并合并GEMINI_API_KEYS
    """
    if settings.ENABLE_STORAGE:
        # 设置JSON文件路径
        storage_dir = pathlib.Path(settings.STORAGE_DIR)
        settings_file = storage_dir / "settings.json"

        # 如果文件不存在，则返回
        if not settings_file.exists():
            return False

        # 从JSON文件中加载设置
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                loaded_settings = json.load(f)

            # 更新settings模块中的变量，但排除特定配置项。
            # Cleanup: GEMINI_API_KEYS / GOOGLE_CREDENTIALS_JSON /
            # VERTEX_EXPRESS_API_KEY 均在 EXCLUDED_SETTINGS 中（安全加固
            # 决定凭据不落盘），旧代码里对这三个字段的"合并/环境变量优先"
            # 特殊分支因 `name not in EXCLUDED_SETTINGS` 先行短路而永远
            # 不可达 —— 约 50 行死代码却暗示凭据可持久化，全部删除。
            # 旧版本 settings.json 若真含有这些字段，也会被同一条件正确
            # 跳过，行为不变。
            for name, value in loaded_settings.items():
                if hasattr(settings, name) and name not in EXCLUDED_SETTINGS:
                    setattr(settings, name, value)

            # 在加载完设置后，检查是否需要刷新模型配置
            try:
                # 如果加载了Google Credentials JSON或Vertex Express API Key，需要刷新模型配置
                if (
                    hasattr(settings, "GOOGLE_CREDENTIALS_JSON")
                    and settings.GOOGLE_CREDENTIALS_JSON
                ) or (
                    hasattr(settings, "VERTEX_EXPRESS_API_KEY")
                    and settings.VERTEX_EXPRESS_API_KEY
                ):
                    log(
                        "info",
                        "检测到Google Credentials JSON或Vertex Express API Key，准备更新配置",
                    )

                    # 更新配置
                    import app.vertex.config as app_config

                    # 重新加载vertex配置
                    app_config.reload_config()

                    # 更新app_config中的GOOGLE_CREDENTIALS_JSON
                    if (
                        hasattr(settings, "GOOGLE_CREDENTIALS_JSON")
                        and settings.GOOGLE_CREDENTIALS_JSON
                    ):
                        app_config.GOOGLE_CREDENTIALS_JSON = (
                            settings.GOOGLE_CREDENTIALS_JSON
                        )
                        # 同时更新环境变量，确保其他模块能够访问到
                        os.environ["GOOGLE_CREDENTIALS_JSON"] = (
                            settings.GOOGLE_CREDENTIALS_JSON
                        )
                        log(
                            "info",
                            "已更新app_config和环境变量中的GOOGLE_CREDENTIALS_JSON",
                        )

                    # 更新app_config中的VERTEX_EXPRESS_API_KEY_VAL
                    if (
                        hasattr(settings, "VERTEX_EXPRESS_API_KEY")
                        and settings.VERTEX_EXPRESS_API_KEY
                    ):
                        app_config.VERTEX_EXPRESS_API_KEY_VAL = [
                            key.strip()
                            for key in settings.VERTEX_EXPRESS_API_KEY.split(",")
                            if key.strip()
                        ]
                        # 同时更新环境变量
                        os.environ["VERTEX_EXPRESS_API_KEY"] = (
                            settings.VERTEX_EXPRESS_API_KEY
                        )
                        log(
                            "info",
                            f"已更新app_config和环境变量中的VERTEX_EXPRESS_API_KEY_VAL，共{len(app_config.VERTEX_EXPRESS_API_KEY_VAL)}个有效密钥",
                        )

                    log("info", "配置更新完成，Vertex AI将在下次请求时重新初始化")

            except Exception as e:
                log("error", f"更新配置时出错: {str(e)}")

            log("info", "加载设置成功")
            return True
        except Exception as e:
            log("error", f"加载设置时出错: {e}")
            return False
