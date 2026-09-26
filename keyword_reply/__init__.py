# -*- coding: utf-8 -*-
"""关键字回复插件的内部实现包。

- rules.py  规则归一化与匹配（纯函数，可脱离 astrbot 单测）
- store.py  规则存盘与查询（纯本地 IO，可脱离 astrbot 单测）
- groups.py umo 解析与群名缓存（纯本地 IO，可脱离 astrbot 单测）
- page.py   Web 面板的后端接口（需要 astrbot 或 quart）
"""

__all__ = ["rules", "store", "groups", "page"]
