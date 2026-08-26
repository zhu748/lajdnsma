"""pytest 全局隔离夹具。

背景（round-3 修复）：14 个测试文件的加载器用 sys.modules.update(...)
注入假桩后从不恢复，假 fastapi / app.utils / app.config.settings 泄漏到
同进程后续测试——任何之后才首次 import 真实模块的测试都会拿到缺属性的
假桩（ImportError / AttributeError），或因假包无 __path__ 而根本无法
导入子模块。此前全量跑全绿纯属各文件注入顺序的巧合。

此夹具在每个测试前后对 sys.modules 做「条目 + 属性」双重快照-恢复：
1. 删除测试期间新注册的模块条目；
2. 还原被替换条目的原对象；
3. 清除测试期间加在「既有模块对象」上的新模块型属性（import 绑定）。

第 3 点不可省：`import a.b.c as x` 的绑定会先走「父模块对象属性链」
再回退 sys.modules——若测试期间真实 app.config 曾被导入，共享的假
`app` 包对象上会留下 `fake_app.config = <真实模块>` 属性，之后其它
测试即便把假桩放回 sys.modules，也会被这条陈旧属性链绕过（拿到真实
settings 而非假桩，产生难以追踪的数值断言失败）。

模块级（collection 阶段）的注入不受影响——快照发生在每个测试开始时，
模块级状态被原样保留。
"""
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _isolate_sys_modules():
    before = dict(sys.modules)
    before_attrs = {
        key: set(vars(mod).keys())
        for key, mod in before.items()
        if isinstance(mod, types.ModuleType)
    }
    yield
    after = sys.modules
    # 1) 移除测试期间新注册的键
    for key in [k for k in after if k not in before]:
        after.pop(key, None)
    # 2) 还原被替换的值（对象身份不同才写回，避免无谓抖动）
    for key, value in before.items():
        if after.get(key) is not value:
            after[key] = value
    # 3) 清除测试期间加在既有模块对象上的新模块型属性（import 绑定）
    for key, attrs in before_attrs.items():
        mod = after.get(key)
        if not isinstance(mod, types.ModuleType):
            continue
        current = vars(mod)
        for attr in [a for a in current if a not in attrs]:
            # 仅清模块型属性（import 绑定）；测试合法写入的函数/数据
            # 状态不属于 import 绑定，保持原样。
            if isinstance(current[attr], types.ModuleType):
                try:
                    delattr(mod, attr)
                except AttributeError:
                    pass
