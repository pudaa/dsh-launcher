# -*- coding: utf-8 -*-
"""控制面板的实现：样式、控件、页面、窗口、自测。

对外只需要 `dsh_panel`（兼容层）——那里的 `import *` 会把这里的一切重新导出。
按子模块直接 import 也可以，但要自己理清依赖顺序：styles → widgets → host
→ workers → pages → window。
"""
