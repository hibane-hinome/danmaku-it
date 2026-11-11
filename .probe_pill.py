import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QTextDocument, QFont
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

def render(html, w=420, h=60):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    doc = QTextDocument()
    doc.setDefaultFont(QFont("Sans Serif", 10))
    doc.setTextWidth(w)
    doc.setHtml(html)
    p = QPainter(img)
    doc.drawContents(p, QRectF(0, 0, w, h))
    p.end()
    return img, doc

# 1) 带 alpha 的十六进制背景，inline span
img, doc = render('<span style="background-color:#8C000000">&nbsp;小明: 你好呀&nbsp;</span>')
print("doc height:", round(doc.size().height(), 1))
def scan(img, y):
    hits = []
    for x in range(img.width()):
        c = img.pixelColor(x, y)
        if c.alpha() > 0:
            hits.append((x, c.alpha()))
    return hits
line = scan(img, 8)
print("row8 first/last:", line[:3], line[-3:] if line else None)
if line:
    print("alpha at span:", line[len(line)//2])

# 2) rgba() 写法能不能解析
img2, _ = render('<span style="background-color:rgba(0,0,0,140)">&nbsp;小明: 你好呀&nbsp;</span>')
l2 = scan(img2, 8)
print("rgba parse ->", (l2[0][1] if l2 else "nothing"))
