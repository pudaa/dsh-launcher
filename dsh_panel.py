# -*- coding: utf-8 -*-
"""控制面板（兼容入口）。

2026-11-12 拆分
--------------
这个文件曾经有 3100 行：样式、控件、四个 worker、六个页面、窗口本体、自测
全挤在一起。现在实现搬进了 `dsh_ui/panel/`，这里只做两件事：

1. 把公共名字**重新导出**——`dsh_theme` 要 `FONT`/`palette`，主窗口要
   `ControlPanel`/`PanelHost`，自测脚本要一堆页面类，全都不必改 import；
2. 保留面板自测（`selftest_checks`）——它验证的是这批控件的构造与状态联动，
   跟着产品代码走比放到 tools/ 更不容易失联。
"""
from __future__ import annotations

from dsh_ui.panel.styles import *      # noqa: F401,F403
from dsh_ui.panel.styles import _css    # noqa: F401  (import * 不带下划线名)
from dsh_ui.panel.widgets import *     # noqa: F401,F403
from dsh_ui.panel.host import *        # noqa: F401,F403
from dsh_ui.panel.workers import *     # noqa: F401,F403
from dsh_ui.panel.pages import *       # noqa: F401,F403
from dsh_ui.panel.window import *      # noqa: F401,F403

from PySide6.QtWidgets import QApplication   # noqa: F401  自测用

def selftest_checks() -> list:
    """给 tools/gui_selftest.py 用的纯逻辑断言，返回失败项列表。

    只验不依赖真实 DSH 的部分。其中几条是**像素级**的，因为那类问题
    （按钮底色退回系统默认、圆角没生效、切主题没重绘）用属性断言根本看不出来。
    """
    from PySide6.QtWidgets import QApplication
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    def loop_events(application, ms: int) -> None:
        """把事件循环推进 ms 毫秒——动画要真的跑，断言才有意义。"""
        import time as _time
        end = _time.monotonic() + ms / 1000.0
        while _time.monotonic() < end:
            application.processEvents()
            _time.sleep(0.01)

    app = QApplication.instance() or QApplication([])              # noqa: F841

    # --- 配色必须逐条等于官方令牌 ---
    # 这是"防割裂"的硬约束：面板配色是照抄官方语义令牌来的，一旦有人手改成
    # "看起来差不多"的颜色，两者放在一起就会露怯。所以把官方原值钉在这里。
    official = {
        "light": {"bg": "#ffffff", "side": "#f9fafb", "text": "#0f1115",
                  "dim": "#61666b", "accent": "#4176e6", "primary_fill": "#0f1115"},
        "dark": {"bg": "#151517", "side": "#1b1b1c", "text": "#f9fafb",
                 "dim": "#cfd3d6", "accent": "#679efe", "primary_fill": "#f9fafb"},
    }
    for theme, expect in official.items():
        pal = palette(theme)
        bad = ["%s=%s(应 %s)" % (k, pal[k], v) for k, v in expect.items()
               if pal[k].lower() != _css(v).lower()]
        check("%s 主题关键色等于官方令牌" % theme, not bad, "、".join(bad))

    check("CSS #RRGGBBAA 正确转成 Qt #AARRGGBB",
          _css("#0000001a") == "#1a000000", _css("#0000001a"))
    check("六位色值不被误改", _css("#ffffff") == "#ffffff")

    # 图标集完整性
    missing = icons.missing([i for i, _t, _c in ControlPanel.PAGES]
                            + list(STATE_ICON.values()))
    check("页面与状态图标齐全", not missing,
          "缺：" + "、".join(missing) if missing else "")

    bad_icons = []
    for name in icons.SVG:
        r = icons.ink_ratio(name, 48, "#000000")
        if not (0.02 < r < 0.60):
            bad_icons.append("%s=%.3f" % (name, r))
    check("全部图标渲染比例正常", not bad_icons, "、".join(bad_icons[:4]))

    # --- 数据目录统计：粗粒度文案 + 预算保护（2026-10-20 第二轮反馈）---
    # 用户："文件数量太大的话统计耗时又占用内存，不需要那么精细"。
    # 所以：文案上万按"万"报，并且统计有硬预算（超了标"≥"而不是硬算到底）。
    counts = [(0, "0"), (999, "999"), (9999, "9999"), (10000, "1.0 万"),
              (66447, "6.6 万"), (1200000, "120 万")]
    bad_counts = ["%d→%s(应 %s)" % (n, storage.str_of_count(n), want)
                  for n, want in counts if storage.str_of_count(n) != want]
    check("文件数文案是粗粒度的", not bad_counts, "、".join(bad_counts))

    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as _tmp:
        for _i in range(5):
            with open(os.path.join(_tmp, "f%d" % _i), "wb") as _fh:
                _fh.write(b"abc")
        _f1, _b1, _p1, _partial1 = storage._scan(_tmp)
        _f2, _b2, _p2, _partial2 = storage._scan(_tmp, budget_s=-1)
        _f3, _b3, _p3, _partial3 = storage._scan(_tmp, max_entries=2)
    check("统计正常时数字准确且不打不完整标记",
          (_f1, _b1, _partial1) == (5, 15, False),
          "%s 个文件 / %s 字节 / partial=%s" % (_f1, _b1, _partial1))
    check("超时间预算时立刻停并标记不完整",
          _partial2 and _f2 <= 5, "partial=%s files=%s" % (_partial2, _f2))
    check("超条目上限时标记不完整", _partial3 and _f3 <= 2,
          "partial=%s files=%s" % (_partial3, _f3))

    # 假的数据根统计：真去数本机那块盘要好几秒（几百兆 / 几万文件），
    # 自测不该为了验渲染去付这个代价。sleep 是用来**假装**那次全树遍历的，
    # 好让下面能断言"切页不等它"。
    fake_usage = {
        "root": r"D:\fake\DSH-Web", "files": 12, "bytes": 3 * 1024 * 1024,
        "groups": [{"key": "profile", "label": "浏览器数据",
                    "hint": "登录态与缓存", "files": 10,
                    "bytes": 2 * 1024 * 1024}],
        "other": 1024 * 1024, "is_default": False,
    }

    def fake_usage_scan():
        time.sleep(1.2)
        return fake_usage

    # 远程主机用假数据：自测不该去读用户真实的 remote-hosts.json，
    # 更不该真去连 SSH。这里只验"面板能不能把主机列出来、状态对不对"。
    fake_hosts = [
        remote.SshHost(name="家里的服务器", host="home-box", user="cx",
                       keep_service=remote.KEEP_KEEP),
        remote.SshHost(name="临时机器", host="10.0.0.5", port=2222),
    ]
    fake_remote_state = {"host_id": fake_hosts[0].id, "name": fake_hosts[0].name,
                         "stage": "正在建立隧道…", "connected": False, "error": ""}

    host = PanelHost(install=None, handle=None, theme=lambda: "dark",
                     usage=fake_usage_scan,
                     remote_hosts=lambda: list(fake_hosts),
                     remote_state=lambda: dict(fake_remote_state))
    panel = ControlPanel(host)
    panel.resize(PANEL_W, PANEL_H)
    panel.show()
    loop_events(app, 260)

    check("固定尺寸为 880x600",
          (panel.width(), panel.height()) == (PANEL_W, PANEL_H),
          "%sx%s" % (panel.width(), panel.height()))
    check("无边框", bool(panel.windowFlags() & Qt.WindowType.FramelessWindowHint))
    check("尺寸被锁定（不可缩放）",
          panel.minimumSize() == panel.maximumSize() == panel.size())
    check("开了半透明底（自绘圆角的前提）",
          panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
    check("页签数量与定义一致", len(panel.navs) == len(ControlPanel.PAGES),
          "%d 个" % len(panel.navs))
    titles = [n.text().strip() for n in panel.navs]
    check("页签名正确",
          titles == ["概览", "运行方式", "本机部署", "常规", "状态检测", "关于"],
          "、".join(titles))

    # 部署页必须有「重新安装 DSH」入口：半升级（主包新、子依赖旧）的唯一
    # 自助修复路径，2026-10-20 加。没有它用户只能回命令行敲 npm。
    import inspect as _inspect
    _deploy_src = _inspect.getsource(DeployPage)
    check("部署页提供「重新安装 DSH」入口",
          "reinstall_dsh" in _deploy_src and "重新安装" in _deploy_src)

    goto_cost = None
    try:
        for i in range(len(ControlPanel.PAGES)):
            _t0 = time.monotonic()
            panel.goto(i)
            if i == 3:
                goto_cost = time.monotonic() - _t0
        check("五个页面都能切换并完成构建",
              len(panel.pages) == len(ControlPanel.PAGES),
              "已构建 %d 个" % len(panel.pages))
    except Exception as e:                                       # noqa: BLE001
        check("五个页面都能切换并完成构建", False,
              "%s: %s" % (type(e).__name__, e))

    # --- 概览页的内存栏（2026-11-12）---
    # 数字靠**注入一份假快照**来验，不依赖本机真有哪些进程——否则这条断言
    # 在 CI（只跑静态分支）和开发机上会得出不同结论。
    check("内存格式化：MB 与 GB 两种量级",
          human_mem(509) == "509 MB" and human_mem(1645) == "1.6 GB"
          and human_mem(0) == "0 MB" and human_mem(None) == "—",
          "%s / %s" % (human_mem(509), human_mem(1645)))

    op = panel.pages.get(0)
    check("概览页有内存栏（合计 + 壳/网页/服务三块）",
          op is not None and hasattr(op, "kv_mem_total")
          and set(getattr(op, "mem_rows", {})) == {"host", "webengine", "node"})

    if op is not None and hasattr(op, "kv_mem_total"):
        fake = {
            "total_ws": 755.0, "total_private": 1645.0,
            "groups": {"host": {"ws": 85, "private": 509, "count": 1},
                       "webengine": {"ws": 364, "private": 521, "count": 2},
                       "node": {"ws": 306, "private": 615, "count": 2},
                       "other": {"ws": 0, "private": 0, "count": 0}},
            "rows": [{}, {}, {}, {}, {}],
        }
        op._apply_mem(fake)
        total_txt = op.kv_mem_total.v.text()
        check("合计取私有内存并带进程数",
              "1.6 GB" in total_txt and "6 个进程" in total_txt, total_txt)
        web_txt = op.mem_rows["webengine"].v.text()
        check("三块分开显示，多进程的行标注数量",
              op.mem_rows["host"].v.text() == "509 MB"
              and "521 MB" in web_txt and "2 个进程" in web_txt,
              "%s ｜ %s" % (op.mem_rows["host"].v.text(), web_txt))
        check("「其他」为空时不占位", op.mem_row_other.isHidden())

        fake["groups"]["other"] = {"ws": 2, "private": 3, "count": 1}
        op._apply_mem(fake)
        check("「其他」有内容时才出现",
              not op.mem_row_other.isHidden()
              and op.mem_row_other.v.text().startswith("3 MB"),
              op.mem_row_other.v.text())

    # 采样线程必须跟着页面可见性走。这条同时钉住 QThread 的**重启**能力：
    # 停止后再 `start_sampling()` 必须能真的跑起来（halt 事件忘了 clear 就是"重启无效"）。
    panel.goto(0)
    loop_events(app, 150)
    mw = getattr(op, "_mem_worker", None) if op is not None else None
    check("回到概览页会启动内存采样",
          mw is not None and mw.isRunning(),
          "worker=%s" % ("None" if mw is None else
                         ("运行中" if mw.isRunning() else "已停")))
    panel.goto(5)
    loop_events(app, 150)
    check("切走概览页后内存采样已停",
          mw is not None and not mw.isRunning(),
          "worker=%s" % ("None" if mw is None else
                         ("运行中" if mw.isRunning() else "已停")))

    sp = panel.pages.get(4)
    check("状态检测页覆盖全部预期项",
          sp is not None and set(sp.rows.keys()) == {k for k, _n in CHECK_ROWS},
          "、".join(sp.rows.keys()) if sp is not None else "状态页未构建")

    # --- 常规页的占用统计必须走后台线程（2026-10-20 卡顿回归）---
    # 用户反馈"每次切进常规 tab 都明显卡顿"：根因是 `storage.breakdown()`
    # 在 GUI 线程里同步遍历整个数据根（实测 10.2 秒 / 6.6 万文件 / 727 MB）。
    # 上面 fake_usage_scan 会睡 1.2 秒，所以这三条断言能分别钉住：
    #   ① 切页不等它（耗时远小于 1.2 秒）
    #   ② 它确实在别的线程里跑
    #   ③ 结果回来后会回填到界面上，且 30 秒内复用缓存
    gp = panel.pages.get(3)
    check("切进常规页不等占用统计（不再卡顿）",
          goto_cost is not None and goto_cost < 0.5,
          "%.3f 秒" % goto_cost if goto_cost is not None else "页面未构建")
    check("占用统计跑在后台线程（不占界面线程）",
          gp is not None and gp._usage_worker is not None
          and gp._usage_worker.isRunning())
    for _ in range(200):
        if gp is None or (gp._usage_worker is not None
                          and not gp._usage_worker.isRunning()):
            break
        loop_events(app, 20)
    loop_events(app, 60)
    got_text = gp.kv_usage.v.text() if gp is not None else "常规页未构建"
    check("统计结果回填到「占用」行",
          "12 个文件" in got_text and "3.0 MB" in got_text, got_text)
    check("数据根来自统计结果",
          gp is not None and gp.kv_root.v.text() == fake_usage["root"],
          gp.kv_root.v.text() if gp is not None else "")
    worker_before = gp._usage_worker if gp is not None else None
    if gp is not None:
        gp.on_show()
    check("30 秒内重复进入常规页复用缓存（不重算）",
          gp is not None and gp._usage_worker is worker_before
          and "12 个文件" in gp.kv_usage.v.text())

    # --- 圆角：像素级 ---
    # grab() 渲染的是控件自己的绘制（含透明通道），所以圆角外应当是**透明**的。
    # 这条能一次抓住三件事：忘了开半透明底、paintEvent 没画圆角、
    # 以及某个子控件拿不透明底把角盖成直角。
    panel.goto(0)
    loop_events(app, 300)
    shot = panel.grab().toImage()
    W, H = shot.width(), shot.height()
    corners = {"左上": (1, 1), "右上": (W - 2, 1),
               "左下": (1, H - 2), "右下": (W - 2, H - 2)}
    solid = []
    for name, (x, y) in corners.items():
        c = shot.pixelColor(x, y)
        if c.alpha() > 24:
            solid.append("%s a=%d %s" % (name, c.alpha(), c.name()))
    check("窗口四角是透明的（圆角生效）", not solid, "、".join(solid))
    check("窗口正中是不透明的（圆角没画过头）",
          shot.pixelColor(W // 2, H // 2).alpha() > 240)

    # --- 标题栏按钮底色必须是深色，不能是系统默认浅色 ---
    bad_light = []
    for btn in (panel.titlebar.btn_min, panel.titlebar.btn_close):
        g = btn.geometry()
        x, y = max(0, g.x() + 6), g.y() + g.height() // 2
        if x < W and y < H:
            c = shot.pixelColor(x, y)
            if c.lightness() > 120:
                bad_light.append("%s=%s" % (btn.toolTip(), c.name()))
    check("标题栏按钮底色是深色（不是系统默认浅色）", not bad_light,
          "、".join(bad_light))

    # --- 关闭键的红色悬停底不能溢出窗口圆角 ---
    # 子控件不会被父控件的 paintEvent 裁剪，红底是矩形的就会盖在圆角上。
    # 反向验证过：去掉 border-top-right-radius 后，这个角会变成 #ec1313 实心。
    from PySide6.QtTest import QTest
    btn_close = panel.titlebar.btn_close
    QTest.mouseMove(btn_close, btn_close.rect().center())
    loop_events(app, 140)
    hot = panel.grab().toImage()
    corner_c = hot.pixelColor(W - 2, 1)
    check("关闭键悬停底色不溢出圆角（角上仍是透明）",
          corner_c.alpha() <= 40, "%s a=%d" % (corner_c.name(), corner_c.alpha()))
    QTest.mouseMove(panel.titlebar, panel.titlebar.rect().center())
    loop_events(app, 80)

    # --- 主题切换 ---
    panel.apply_theme("light")
    loop_events(app, 220)
    shot2 = panel.grab().toImage()
    mid = shot2.pixelColor(W // 2, H // 2)
    check("切到浅色主题后中央底色变浅", mid.lightness() > 200, mid.name())
    check("切主题后四角仍然是透明的",
          all(shot2.pixelColor(x, y).alpha() <= 24 for x, y in corners.values()))
    panel.apply_theme("dark")
    loop_events(app, 220)
    check("切回深色主题",
          panel.theme() == "dark"
          and panel.grab().toImage().pixelColor(W // 2, H // 2).lightness() < 80)

    # --- 快速连点导航后当前页必须真的可见 ---
    for i in (0, 1, 2, 3, 4, 0, 1):
        panel.goto(i)
    waited = 0
    cur = panel.pages.get(panel.current)
    while cur is not None and cur.graphicsEffect() is not None and waited < 2000:
        loop_events(app, 50)
        waited += 50
    eff = cur.graphicsEffect() if cur is not None else None
    check("快速连点导航后当前页恢复可见（淡入能收尾）",
          cur is not None and eff is None,
          "等待 %dms，effect=%s" % (waited, type(eff).__name__ if eff else "已撤掉"))

    # --- 真机反馈过的两个排版问题，逐页扫 ---
    #   ① 按钮被压扁、文字被内边距裁掉
    #   ② 卡片里的纯文本贴到卡片边框上
    # 这两条只能等布局真的跑完再量，所以逐页 goto + 推进事件循环。
    squeezed, touching = [], []
    for idx in range(len(ControlPanel.PAGES)):
        panel.goto(idx)
        loop_events(app, 280)
        page = panel.pages[idx]

        for b in page.findChildren(QPushButton):
            if b.objectName() not in ("action", "primary"):
                continue
            if b.height() < b.sizeHint().height():
                squeezed.append("第%d页 %r %d<%d"
                                % (idx, b.text()[:10], b.height(),
                                   b.sizeHint().height()))

        for card in page.findChildren(Card):
            for lbl in card.findChildren(QLabel):
                # 只看**直接挂在卡片上**的文本：行控件内部的 label 自己有边距
                if lbl.parentWidget() is not card:
                    continue
                cm = lbl.contentsMargins()
                if cm.left() < 12 and "padding" not in lbl.styleSheet():
                    touching.append("第%d页 %r 左边距=%d"
                                    % (idx, lbl.text()[:14], cm.left()))

    check("没有按钮被压到低于自身最小高度", not squeezed, "、".join(squeezed[:4]))
    check("卡片里的纯文本没有贴边框（左右 >= 12px）", not touching,
          "、".join(touching[:4]))

    # --- 内容超出固定窗口高度时必须可滚动，而不是挤压控件 ---
    #  这是上面第一条的**根因**：QVBoxLayout 在空间不足时会把子项压到低于最小值。
    scrolled = []
    for idx in range(len(ControlPanel.PAGES)):
        page = panel.pages[idx]
        if not isinstance(page.scroll, QScrollArea):
            scrolled.append("第%d页没有滚动区" % idx)
        elif not page.scroll.widgetResizable():
            scrolled.append("第%d页滚动区不可随窗口调整" % idx)
        elif page.scroll.verticalScrollBarPolicy() != \
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            # 原生条常驻可见还占内容宽度，老大明确不要
            scrolled.append("第%d页原生滚动条没关掉" % idx)
    check("每页都有滚动区，且原生滚动条已关闭", not scrolled, "、".join(scrolled))

    # --- 浮动指示条：该出现时出现、该消失时消失、别越界 ---
    hint_bad = []
    for idx in range(len(ControlPanel.PAGES)):
        panel.goto(idx)
        loop_events(app, 300)
        page, bar = panel.pages[idx], panel.pages[idx].scroll.verticalScrollBar()
        vp = page.scroll.viewport()
        if bar.maximum() <= 0:
            # 内容不超高：条子必须完全不出现（也不占位）
            if page.hint.isVisible():
                hint_bad.append("第%d页内容不超高却显示了指示条" % idx)
            continue
        # 滚到底：条子应可见，且**完全落在视口内**（滑到底不能越过下沿）
        bar.setValue(bar.maximum())
        loop_events(app, 120)
        g = page.hint.geometry()
        if not page.hint.isVisible():
            hint_bad.append("第%d页滚动后指示条没出现" % idx)
        if not (0 <= g.top() and g.bottom() <= vp.height()
                and g.right() <= vp.width()):
            hint_bad.append("第%d页指示条越界 %s（视口 %dx%d）"
                            % (idx, g, vp.width(), vp.height()))
        # 浮层绝不能吃鼠标事件，否则会挡住它下面的按钮
        if not page.hint.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents):
            hint_bad.append("第%d页指示条会挡鼠标" % idx)
    check("浮动滚动指示条行为正确（不越界、不挡鼠标、不需要时不出现）",
          not hint_bad, "、".join(hint_bad[:4]))

    # --- 界面里不许出现 Emoji ---
    import re
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    hits = [n.text() for n in panel.navs if emoji.search(n.text())]
    for page in panel.pages.values():
        for lbl in page.findChildren(QLabel):
            if emoji.search(lbl.text()):
                hits.append(lbl.text()[:30])
    check("面板不含 Emoji", not hits, "、".join(hits[:3]))

    # --- 设置项从概览迁到「常规」页（2026-10-20 重排）---
    gp = panel.pages.get(3)
    toggles = getattr(gp, "toggles", {}) if gp is not None else {}
    check("「常规」页承载了更新与外观、开发者开关",
          {"prerelease_opt_in", "dev_mode"} <= set(toggles),
          "、".join(toggles) or "无")
    # 开发者模式下才给入口：默认关着的时候按钮必须是灰的，否则用户点了
    # 会被"还没开启"顶回来，属于无意义交互。
    check("开发者按钮在模式关闭时不可点",
          gp is not None and hasattr(gp, "btn_dev")
          and not gp.btn_dev.isEnabled() and not gp.btn_log.isEnabled())
    # 概览只留状态与主操作：不该再出现更新按钮（那是常规页的事）
    ov = panel.pages.get(0)
    check("概览已瘦身（更新按钮不在概览）",
          ov is not None and not hasattr(ov, "btn_update"))
    # 运行方式页三档齐全
    rp = panel.pages.get(1)
    check("运行方式页给出私有 / 公共两档入口",
          rp is not None and hasattr(rp, "btn_private") and hasattr(rp, "btn_public"))

    # --- 远程主机列表（2026-11-12）---
    rows = getattr(rp, "_remote_rows", {}) if rp is not None else {}
    check("远程主机按配置逐台列出", len(rows) == len(fake_hosts),
          "%d 台" % len(rows))
    first = rows.get(fake_hosts[0].id)
    check("主机行显示名称与地址",
          first is not None and first.name_lbl.text() == "家里的服务器"
          and first.addr_lbl.text() == "cx@home-box")
    check("非默认端口写进地址显示",
          any(r.addr_lbl.text().endswith(":2222") for r in rows.values()))
    check("连接中的那台按钮禁用并显示进度",
          first is not None and not first.btn.isEnabled()
          and "连接中" in first.btn.text(),
          first.btn.text() if first is not None else "")
    check("没连上的主机按钮可点",
          any(r.btn.isEnabled() and r.btn.text() == "连接" for r in rows.values()))
    check("连接中时「断开」不可点（半开的隧道没法取消）",
          rp is not None and not rp.btn_disconnect.isEnabled())

    fake_remote_state.update({"connected": True, "stage": "", "name": "家里的服务器"})
    if rp is not None:
        rp.refresh_remote()
    check("连上后状态行与按钮同步",
          rp is not None and "已连接" in rp.kv_remote.v.text()
          and rp.btn_disconnect.isEnabled()
          and (rows.get(fake_hosts[0].id) is not None
               and rows[fake_hosts[0].id].btn.text() == "断开"),
          rp.kv_remote.v.text() if rp is not None else "")

    check("后台检测线程能干净收尾", panel.shutdown(15000))

    panel.close()
    panel.deleteLater()
    return failures
