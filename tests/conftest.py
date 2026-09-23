"""pytest 引导：把插件仓库目录注册为 agnes_ai_generation 包（与框架加载方式一致）"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

if "agnes_ai_generation" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "agnes_ai_generation", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agnes_ai_generation"] = mod
    spec.loader.exec_module(mod)
