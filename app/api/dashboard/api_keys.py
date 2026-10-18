"""API 密钥管理端点：检测 / 进度 / 清理 / 导出 + 后台检测任务。

注意：run_api_key_test 之后必须紧跟带 @dashboard_router 装饰器的函数——
tests/test_round5_regressions.py 以源码文本方式锁定 run_api_key_test 函数
体（finally 块清理冷却状态）的边界。
"""
import asyncio

from fastapi import Depends, HTTPException

from app.api.dashboard import runtime
from app.api.dashboard.auth import require_body_password, require_dashboard_token
from app.config.persistence import save_settings
from app.services import GeminiClient
from app.utils.api_key import clear_all_cooldowns
from app.utils.logging import log
import app.config.settings as settings

# API密钥检测的进度信息（跨端点共享的可变状态）
api_key_test_progress = {
    "is_running": False,
    "completed": 0,
    "total": 0,
    "valid": 0,
    "invalid": 0,
    "is_completed": False,
}

# 后台任务引用持有：asyncio.create_task 只持弱引用，不持引用的
# 长耗时任务（密钥检测可能跑几分钟）可能被 GC 静默中断。
_dashboard_bg_tasks: set = set()


async def test_api_key(key):
    """测试单个API密钥是否有效（以能否列出模型为准）。"""
    try:
        all_models = await GeminiClient.list_available_models(key)
        return len(all_models) > 0
    except Exception as e:
        log("error", f"测试API密钥 key#{hash(key) & 0xFFFFFF:06x} 时出错: {str(e)}")
        return False


async def run_api_key_test(keys):
    """在主事件循环上串行检测全部 API 密钥。

    取代旧的线程 + 每 key 新建 event loop 方案（见下方 /test-api-keys
    端点注释），串行 + 探测间 2-6s 随机抖动，避免密钥枚举指纹。
    """
    # 重置进度信息
    api_key_test_progress.update(
        {
            "is_running": True,
            "completed": 0,
            "total": 0,  # 稍后会更新
            "valid": 0,
            "invalid": 0,
            "is_completed": False,
        }
    )

    try:
        # 获取所有需要检测的密钥（当前池 + INVALID_API_KEYS 中的历史无效密钥）
        current_keys = keys

        invalid_api_keys = (
            settings.INVALID_API_KEYS.split(",") if settings.INVALID_API_KEYS else []
        )
        invalid_api_keys = [key.strip() for key in invalid_api_keys if key.strip()]

        # 合并所有需要测试的密钥，去重
        all_keys_to_test = list(set(current_keys + invalid_api_keys))

        # 更新总数
        api_key_test_progress["total"] = len(all_keys_to_test)

        # 创建有效和无效密钥列表
        valid_keys = []
        invalid_keys = []

        # Hardening: probe bursts are a clear key-enumeration fingerprint.
        # If we fire 50 sequential `:models` list calls at Google from one
        # IP within seconds, Google's risk control sees that as a key
        # enumeration attack against the Gemini API.  We add a jittered
        # delay between probes (2-6s uniform jitter) so the probe
        # sequence looks like real human-driven testing of individual
        # keys, not a script iterating through a list.  Operators with
        # many keys should expect the batch to take a few minutes.
        import random as _random

        for idx, key in enumerate(all_keys_to_test):
            # 跳过首个探测前的等待，让首个结果尽快返回；只做探测间隔。
            if idx > 0:
                await asyncio.sleep(_random.uniform(2.0, 6.0))

            # 检查密钥（直接在主循环上 await，安全共享全局 httpx 客户端）
            is_valid = await test_api_key(key)
            if is_valid:
                log("info", f"API密钥 key#{hash(key) & 0xFFFFFF:06x} 有效")
            else:
                log("warning", f"API密钥 key#{hash(key) & 0xFFFFFF:06x} 无效")

            # 更新进度
            api_key_test_progress["completed"] += 1

            # 将密钥添加到相应列表
            if is_valid:
                valid_keys.append(key)
                api_key_test_progress["valid"] += 1
            else:
                invalid_keys.append(key)
                api_key_test_progress["invalid"] += 1

        # 更新全局密钥列表。
        # Correctness: 旧实现用检测开始时的快照整体覆写 key_manager.api_keys
        # —— 检测串行且每 key 间隔 2-6s 抖动，几十个 key 要跑几分钟，期间
        # 通过 update_config(gemini_api_keys) 新增的 key 会被静默丢弃。
        # 改为与当前列表合并：保留检测期间新增且未被本次检测覆盖的 key。
        tested = set(all_keys_to_test)
        newly_added = [k for k in runtime.key_manager.api_keys if k not in tested]
        final_valid_keys = valid_keys + newly_added

        runtime.key_manager.api_keys = final_valid_keys

        # 更新设置中的有效和无效密钥
        settings.GEMINI_API_KEYS = ",".join(final_valid_keys)
        settings.INVALID_API_KEYS = ",".join(invalid_keys)

        # 保存设置
        save_settings()

        # 重置密钥栈
        runtime.key_manager._reset_key_stack()

        log(
            "info",
            f"API密钥检测完成。有效密钥: {len(valid_keys)}，无效密钥: {len(invalid_keys)}",
        )
    except Exception as e:
        log("error", f"API密钥检测过程中发生错误: {str(e)}")
    finally:
        # Round 5（逻辑 bug）: 检测完成后清理全部冷却状态。
        # 场景：某 key 早前因 401/403 被永久拉黑（PERMANENT_BLOCK_TS），
        # 密钥持有者在 Google 侧修复后重新运行检测——检测判定其有效并
        # 保留在 key_manager.api_keys，但 _key_cooldowns 里的永久拉黑
        # 条目不会随之满除：get_available_key 会永远跳过该 key，
        # 面板显示"有效"而运行时从不用它。刚验证过的新池不应继承
        # 陈旧冷却状态；真实失败会在下次请求时重新触发冷却。
        try:
            await clear_all_cooldowns()
        except Exception as e:
            log("warning", f"清理密钥冷却状态时出错: {str(e)}")
        # Round 8（PVP）：同步清空失败类别记录。PVP 的"死 key 短路"
        # 依赖 error_handling._key_failure_kinds 里的 401/403 记录；
        # 若持有者已在 Google 侧修复并重测通过，陈旧的 dead 标记会让
        # PVP 拒绝一个刚被验证有效的 key（与上方冷却清理同一道理）。
        try:
            from app.utils.error_handling import reset_key_failure_kinds

            reset_key_failure_kinds()
        except Exception as e:
            log("warning", f"清理密钥失败类别记录时出错: {str(e)}")
        # 标记检测完成
        api_key_test_progress.update({"is_running": False, "is_completed": True})


@runtime.dashboard_router.post("/test-api-keys")
async def test_api_keys(password_data: dict = Depends(require_body_password)):
    """测试所有API密钥的有效性（后台串行执行）。"""
    try:
        # 检查是否已经有测试在运行。
        # Correctness: 旧实现在检查 is_running 与后台任务真正置位之间存在
        # 调度间隙（run_api_key_test 要等事件循环调度后才更新进度字典），
        # 两个并发 POST 都能通过检查并各自启动检测，并发改写
        # key_manager.api_keys。改为在端点内同步置位，检测任务开始时再
        # 重置计数器，彻底消除 TOCTOU 窗口。
        if api_key_test_progress["is_running"]:
            raise HTTPException(status_code=409, detail="已有API密钥检测正在进行中")
        api_key_test_progress["is_running"] = True

        # 获取有效密钥列表
        valid_keys = runtime.key_manager.api_keys.copy()

        # 启动后台检测任务
        # Fix: 旧实现在新线程里为每个 key 新建 event loop 再调
        # test_api_key → GeminiClient → get_async_client()，拿到的是
        # 绑定主事件循环的全局共享 httpx AsyncClient —— 跨线程跨循环
        # 使用同一客户端不是并发安全的（连接池原语与创建它的 loop
        # 绑定），检测期间并发流量可能报 "attached to a different
        # loop" 或污染连接池。改为在主循环上以后台任务串行检测
        # （与 main.py 的 check_remaining_keys_async 同一模式），
        # 同时消除每 key 新建/销毁 event loop 的开销。
        task = asyncio.create_task(run_api_key_test(valid_keys))
        _dashboard_bg_tasks.add(task)
        task.add_done_callback(_dashboard_bg_tasks.discard)

        return {
            "status": "success",
            "message": "API密钥检测已启动，将同时检测有效密钥和无效密钥",
        }
    except HTTPException:
        raise
    except Exception as e:
        # 同步置位的 is_running 在任务创建失败时必须回滚，
        # 否则该功能会永久处于"检测中"状态。
        api_key_test_progress["is_running"] = False
        raise HTTPException(status_code=500, detail=f"启动API密钥检测失败：{str(e)}")


@runtime.dashboard_router.get("/test-api-keys/progress")
async def get_test_api_keys_progress(_: None = Depends(require_dashboard_token)):
    """轮询 API 密钥检测进度（鉴权要求与 /dashboard-data 一致）。"""
    return api_key_test_progress


@runtime.dashboard_router.post("/clear-invalid-api-keys")
async def clear_invalid_api_keys(password_data: dict = Depends(require_body_password)):
    """清除所有失效的API密钥。"""
    try:
        # 获取当前无效密钥数量
        current_invalid_keys = (
            settings.INVALID_API_KEYS.split(",") if settings.INVALID_API_KEYS else []
        )
        current_invalid_keys = [
            key.strip() for key in current_invalid_keys if key.strip()
        ]
        invalid_count = len(current_invalid_keys)

        if invalid_count == 0:
            return {"status": "success", "message": "没有失效的API密钥需要清除"}

        # 清除无效密钥
        settings.INVALID_API_KEYS = ""
        save_settings()

        log("info", f"已清除 {invalid_count} 个失效的API密钥")

        return {
            "status": "success",
            "message": f"已成功清除 {invalid_count} 个失效的API密钥",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清除失效API密钥失败：{str(e)}")


@runtime.dashboard_router.post("/export-valid-api-keys")
async def export_valid_api_keys(password_data: dict = Depends(require_body_password)):
    """导出所有有效的API密钥（明文列表）。"""
    try:
        # 获取当前有效密钥
        valid_keys = runtime.key_manager.api_keys.copy()

        if not valid_keys:
            return {"status": "success", "message": "当前没有有效的API密钥", "keys": []}

        log("info", f"用户导出了 {len(valid_keys)} 个有效API密钥")

        return {
            "status": "success",
            "message": f"成功获取 {len(valid_keys)} 个有效API密钥",
            "keys": valid_keys,
            "count": len(valid_keys),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取有效API密钥失败：{str(e)}")
