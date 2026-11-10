"""生成项目图标：多尺寸 PNG 打包成 .ico。

图案：蓝紫渐变圆角方块 + 白色对话气泡 + 两条"弹幕"横线，
小尺寸也能认出是「弹幕」而不是一团色块。
"""
import os
import struct
import sys

sys.path.insert(0, ".")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    r = float(size)

    # 底：圆角方块，左上蓝 → 右下紫
    grad = QLinearGradient(0, 0, r * 0.85, r)
    grad.setColorAt(0.0, QColor("#4f8cff"))
    grad.setColorAt(1.0, QColor("#8b5cf6"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    radius = r * 0.23
    p.drawRoundedRect(QRectF(0, 0, r, r), radius, radius)

    # 气泡主体
    bx, by = r * 0.185, r * 0.245
    bw, bh = r * 0.63, r * 0.375
    p.setBrush(QColor(255, 255, 255, 245))
    p.drawRoundedRect(QRectF(bx, by, bw, bh), r * 0.10, r * 0.10)

    # 气泡尾巴（左下角小三角）
    tail = QPainterPath()
    tail.moveTo(bx + bw * 0.22, by + bh - r * 0.012)
    tail.lineTo(bx + bw * 0.16, by + bh + r * 0.135)
    tail.lineTo(bx + bw * 0.50, by + bh - r * 0.012)
    tail.closeSubpath()
    p.drawPath(tail)

    # 两条弹幕线：长短不一，才像一行行滚过去的字
    p.setBrush(QColor("#5b7cf6"))
    line_h = max(r * 0.085, 1.6)
    for cy, width_ratio in ((by + bh * 0.33, 0.74), (by + bh * 0.66, 0.46)):
        p.drawRoundedRect(
            QRectF(bx + bw * 0.13, cy - line_h / 2, bw * width_ratio, line_h),
            line_h / 2,
            line_h / 2,
        )
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: str, blobs: list[tuple[int, bytes]]) -> None:
    """ICO 容器：每个条目内嵌一张 PNG（Vista 起支持）。"""
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    payload = b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size  # 256 记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    with open(path, "wb") as fh:
        fh.write(header + entries + payload)


def render_arrow(color: str) -> QImage:
    """下拉框的小三角，画成 4 倍再让 QSS 缩到 8×5，边缘才不糊。"""
    w, h = 32, 20
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    path = QPainterPath()
    path.moveTo(w * 0.18, h * 0.30)
    path.lineTo(w * 0.82, h * 0.30)
    path.lineTo(w * 0.50, h * 0.72)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return img


def main() -> None:
    app = QApplication.instance() or QApplication([])
    os.makedirs(OUT_DIR, exist_ok=True)

    images = {size: render(size) for size in SIZES}
    images[256].save(os.path.join(OUT_DIR, "icon.png"), "PNG")
    write_ico(
        os.path.join(OUT_DIR, "icon.ico"),
        [(size, png_bytes(images[size])) for size in SIZES],
    )
    # 下拉框箭头：暗色主题用浅灰，浅色主题用深灰
    render_arrow("#c6ccd8").save(os.path.join(OUT_DIR, "arrow-dark.png"), "PNG")
    render_arrow("#5d6675").save(os.path.join(OUT_DIR, "arrow-light.png"), "PNG")
    print("wrote", os.path.normpath(os.path.join(OUT_DIR, "icon.ico")))
    print("sizes", SIZES)


if __name__ == "__main__":
    main()
