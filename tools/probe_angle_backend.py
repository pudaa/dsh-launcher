"""探针：验证 --use-angle 换后端是否真的生效（2026-10-25）。

背景：QTBUG-142165 的崩溃点（PackedAttributeLayout / InputLayoutCache /
StateManager11）全在 `rx::` 命名空间，也就是 **ANGLE 的 D3D11 renderer**。
换 ANGLE 后端 = 不构造 PackedAttributeLayout = 不进那段代码。
本探针对每种 flags 起一个独立 QWebEngineView，读 WebGL renderer 字符串，
并同时存两张图：`shot.png`（Qt 内部 grab）与 `screen.png`（屏幕 BitBlt，
抓"用户实际看到的像素"），用来区分"后端切过去了"和"画面真的出来了"。

**已知结论（本机 2026-10-25，Qt 6.11.2 / Windows / AMD 780M + RTX 4060）**：
只有默认 D3D11 渲染正常（非白像素 30%）；
`--use-angle=gl` 与 `--use-angle=vulkan` 的 renderer 字符串都正确、context 也建成，
但合成输出**全白**（`RasterDecoderImpl: Context lost` / `CreateSharedImage failed`），
延长到 5s 重抓仍全白；`--use-angle=d3d9` 把 WebGL 降到 1.0 且 EGL 报错。
⇒ **换后端不能用来规避这个崩溃**。详见 docs/webengine-angle-crash.md。

用法：
  python tools/probe_angle_backend.py                     # 默认（D3D11）
  python tools/probe_angle_backend.py --use-angle=gl
  python tools/probe_angle_backend.py --use-angle=vulkan
  python tools/probe_angle_backend.py --use-angle=d3d9
"""
import os
import sys
import tempfile

flags = sys.argv[1] if len(sys.argv) > 1 else ""
# 必须在导入 PySide6 之前设置
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags

from PySide6.QtCore import QTimer, QUrl                               # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView                 # noqa: E402
from PySide6.QtWidgets import QApplication                            # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;background:#fff">
<canvas id="c" width="200" height="120"></canvas>
<div style="font:16px sans-serif;color:#c00;padding:4px">ANGLE 后端渲染验证</div>
<div id="box" style="width:180px;height:60px;background:linear-gradient(90deg,#22c1a3,#0b7285)"></div>
<script>
// 画点不容易巧合的内容：彩色三角形 + 条纹
(function () {
  var c = document.getElementById('c');
  var gl = c.getContext('webgl2') || c.getContext('webgl');
  if (!gl) return;
  gl.clearColor(0.13, 0.76, 0.64, 1.0);
  gl.clear(gl.COLOR_BUFFER_BIT);
})();
window.__r = (function () {
  var c = document.getElementById('c');
  var gl = c.getContext('webgl2') || c.getContext('webgl');
  if (!gl) return JSON.stringify({ ok: false });
  var d = gl.getExtension('WEBGL_debug_renderer_info');
  return JSON.stringify({
    ok: true,
    renderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    vendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
    version: gl.getParameter(gl.VERSION)
  });
})();
</script></body></html>"""


def main() -> int:
    root = tempfile.mkdtemp(prefix="probe-angle-")
    page_path = os.path.join(root, "page.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(PAGE)

    app = QApplication(sys.argv)
    profile = QWebEngineProfile("probe", app)
    profile.setCachePath(os.path.join(root, "cache"))
    profile.setPersistentStoragePath(os.path.join(root, "storage"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)

    view = QWebEngineView()
    view.setPage(QWebEnginePage(profile, view))
    view.resize(400, 300)
    view.show()

    def finish(code: int, text: str) -> None:
        print("FLAGS : %s" % (flags or "(无)"))
        print("RESULT: %s" % text, flush=True)
        app.exit(code)

    def on_load(ok: bool) -> None:
        if not ok:
            finish(2, "页面加载失败")
            return
        QTimer.singleShot(
            600, lambda: view.page().runJavaScript("window.__r", on_js))

    def on_js(res) -> None:
        # 再等一拍让 compositor 上屏，然后截图存盘用于人工核对像素
        def shoot() -> None:
            shot = os.path.join(root, "shot.png")
            try:
                view.grab().save(shot)
                print("SHOT  : %s" % shot)
            except Exception as e:                          # noqa: BLE001
                print("SHOT  : 失败 %s" % e)
            # 屏幕 BitBlt：抓"用户实际看到的像素"。
            # 不用 PrintWindow——它会触发重绘，把问题伪装成正常。
            try:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.primaryScreen()
                surf = scr.grabWindow(int(view.winId()))
                sp = os.path.join(root, "screen.png")
                surf.save(sp)
                print("SCREEN: %s" % sp)
            except Exception as e:                          # noqa: BLE001
                print("SCREEN: 失败 %s" % e)
            finish(0, str(res))

        QTimer.singleShot(5000, shoot)

    view.loadFinished.connect(on_load)
    view.load(QUrl.fromLocalFile(page_path))
    QTimer.singleShot(25000, lambda: finish(3, "超时（25s 无结果）"))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
