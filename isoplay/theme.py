"""Colours, stylesheets and vector artwork."""
import sys
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from isoplay.runtime import BASE

MAC = sys.platform == "darwin"
MOD = "⌘" if MAC else "Ctrl+"

INK, INK2, INK3 = "#f5f5f7", "rgba(235,235,245,0.62)", "rgba(235,235,245,0.38)"
ICON = "#9e9ea6"  # SVG has no rgba(), so icons need a flat colour, not INK2/INK3
CSS = f"""
* {{ outline: none; }}
QMainWindow {{ background: #000; }}
#welcome {{ background: #161618; }}
#welcome[dropping="true"] {{ background: #1b2330; }}
#appname {{ color: {INK}; font-size: 20px; font-weight: 600; }}
#version, #muted {{ color: {INK3}; font-size: 12px; }}
#section {{ color: {INK2}; font-size: 12px; font-weight: 600; }}
#action {{ color: {INK}; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px; padding: 0 14px; min-height: 38px; font-size: 13px; text-align: left; }}
#action:hover {{ background: rgba(255,255,255,0.10); }}
#action:pressed {{ background: rgba(255,255,255,0.14); }}
#link {{ color: {INK3}; background: transparent; border: none; font-size: 12px; padding: 0; }}
#link:hover {{ color: {INK}; }}
QToolButton#link {{ padding: 4px 6px; }}
QToolButton#link::menu-indicator {{ image: none; width: 0; height: 0; }}
#row {{ background: transparent; border-radius: 7px; }}
#row:hover {{ background: rgba(255,255,255,0.06); }}
#rowtitle {{ color: {INK}; font-size: 13px; }}
#rowmeta {{ color: {INK3}; font-size: 12px; }}
#recentScroll, #recentContent {{ background: transparent; border: none; }}
#recentScroll QScrollBar:vertical {{ background: transparent; width: 6px; margin: 4px 0; }}
#recentScroll QScrollBar::handle:vertical {{ background: {INK3}; border-radius: 3px; min-height: 24px; }}
#recentScroll QScrollBar::add-line:vertical, #recentScroll QScrollBar::sub-line:vertical {{ height: 0; }}
#recentScroll QScrollBar::add-page:vertical, #recentScroll QScrollBar::sub-page:vertical {{ background: transparent; }}
#empty {{ color: {INK3}; font-size: 13px; }}
#panel {{ background: rgba(28,28,30,0.90); border: 1px solid rgba(255,255,255,0.10); border-radius: 12px; }}
#forget {{ background: rgba(255,255,255,0.08); border: none; border-radius: 13px; }}
#forget:hover {{ background: rgba(255,69,58,0.80); }}
#forget:pressed {{ background: rgba(255,69,58,0.95); }}
#problem {{ color: #ffb4ab; background: rgba(255,69,58,0.12); border: 1px solid #742925;
    border-radius: 8px; padding: 10px 12px; font-size: 12.5px; margin-top: 14px; }}
#panel QPushButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px; }}
#panel QPushButton:hover {{ background: rgba(255,255,255,0.10); }}
#panel QPushButton:pressed {{ background: rgba(255,255,255,0.16); }}
#time {{ color: {INK2}; font-size: 11px; }}
#panel QPushButton#time {{ padding: 0 2px; }}
#panel QPushButton#time:hover {{ background: transparent; color: {INK}; }}
QSlider::groove:horizontal {{ height: 3px; background: rgba(255,255,255,0.20); border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: rgba(255,255,255,0.90); border-radius: 1px; }}
QSlider::handle:horizontal {{ width: 11px; height: 11px; margin: -4px 0; border-radius: 5px; background: #fff; }}
#back {{ background: rgba(28,28,30,0.90); border: 1px solid rgba(255,255,255,0.10); border-radius: 9px; }}
#back:hover {{ background: rgba(52,52,56,0.95); }}
#back:pressed {{ background: rgba(68,68,72,0.95); }}
#osd {{ background: rgba(28,28,30,0.90); border: 1px solid rgba(255,255,255,0.10); border-radius: 9px; }}
#osd QLabel {{ color: {INK}; font-size: 13px; font-weight: 500; }}
#loaderCaption {{ color: {INK}; font-size: 13px; font-weight: 500; background: transparent; }}
QToolTip {{ color: {INK}; background: #2c2c2e; border: 1px solid rgba(255,255,255,0.12); padding: 3px 6px; }}
"""
MENU_CSS = f"""
QMenu {{ background: #2c2c2e; border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 4px; }}
QMenu::item {{ color: {INK}; padding: 5px 22px 5px 22px; border-radius: 5px; font-size: 13px; }}
QMenu::item:selected {{ background: #0a84ff; }}
QMenu::item:disabled {{ color: {INK3}; }}
QMenu::separator {{ height: 1px; background: rgba(255,255,255,0.10); margin: 4px 6px; }}
QMenu::indicator {{ width: 12px; left: 6px; }}
"""
OPTIONS_CSS = f"""
QMenu#optionsMenu {{ background: #242426; color: {INK}; border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 6px; }}
QMenu#optionsMenu::item {{ color: {INK}; padding: 10px 12px; border-radius: 6px; font-size: 13px; }}
QMenu#optionsMenu::item:selected {{ background: rgba(255,255,255,0.08); }}
QMenu#optionsMenu::item:disabled {{ color: {INK3}; }}
QMenu#optionsMenu::indicator {{ width: 0; height: 0; }}
QMenu#optionsMenu::separator {{ height: 1px; background: rgba(255,255,255,0.08); margin: 5px 8px; }}
QPushButton#preferenceRow {{ background: transparent; border: none; border-radius: 6px; padding: 0; }}
QPushButton#preferenceRow:hover, QPushButton#preferenceRow:focus {{ background: rgba(255,255,255,0.06); }}
QLabel#preferenceLabel {{ color: {INK}; font-size: 13px; background: transparent; }}
"""

ICONS = {  # 24x24 path data (Material Symbols, Apache-2.0)
    "play": "M8 5.14v13.72a1 1 0 0 0 1.52.85l10.9-6.86a1 1 0 0 0 0-1.7L9.52 4.29A1 1 0 0 0 8 5.14z",
    "pause": "M7 5h3a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm7 0h3a1 1 0 0 1 1 1v12"
             "a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
    "prev": "M6 6h2v12H6zm3.5 6 8.5 6V6z",
    "next": "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z",
    "disc": "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 14.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9zm0-5.5"
            "a1 1 0 1 0 0 2 1 1 0 0 0 0-2z",
    "tracks": "M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM4 12h4v2H4zm10 6H4v-2h10z"
              "m6 0h-4v-2h4zm0-4H10v-2h10z",
    "volume": "M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05A4.48 4.48 0 0 0 16.5 12zM14 3.23v2.06"
              "a7 7 0 0 1 0 13.42v2.06A9 9 0 0 0 14 3.23z",
    "muted": "M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM19 12c0 .94-.2 1.82-.54 2.64"
             "l1.51 1.51A8.8 8.8 0 0 0 21 12a9 9 0 0 0-7-8.77v2.06A7 7 0 0 1 19 12zM4.27 3 3 4.27 7.73 9H3v6h4"
             "l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a9 9 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9z"
             "M12 4 9.91 6.09 12 8.18V4z",
    "full": "M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z",
    "unfull": "M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z",
    "folder": "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z",
    "link": "M3.9 12A3.1 3.1 0 0 1 7 8.9h4V7H7a5 5 0 0 0 0 10h4v-1.9H7A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2z"
            "m9-6h-4v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10z",
    "settings": "M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32"
                "a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84"
                "c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.74 8.87"
                "a.49.49 0 0 0 .12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61"
                "l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84"
                "c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32"
                "c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z",
    "close": "M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59"
             "L13.41 12 19 6.41z",
    "back": "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z",
}


@lru_cache
def svg_icon(svg, size=48, color=INK):
    """Render SVG markup or an ICONS key to a QIcon (QSvgRenderer, so no image-format plugin needed)."""
    if svg in ICONS:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<path fill="{color}" d="{ICONS[svg]}"/></svg>')
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    QSvgRenderer(svg.encode() if isinstance(svg, str) else svg).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def tabular(widget):
    f = widget.font()
    f.setFeature(QFont.Tag("tnum"), 1)
    widget.setFont(f)
    return widget


BADGES = {  # kind -> (accessible label, artwork; see assets/media-badges/README.md for sources)
    "bluray": ("Blu-ray disc", "blu-ray.svg"),
    "dvd": ("DVD disc", "dvd.svg"),
    "video": ("Media file", "media-file.svg"),
    "disc": ("Disc image", "disc-image.png"),  # PNG: QtSvg ignores the clip-path this artwork needs
}


@lru_cache
def badge_art(art):
    path = BASE / "assets/media-badges" / art
    if path.suffix == ".png":
        return QPixmap(str(path))
    renderer = QSvgRenderer(str(path))
    box = renderer.viewBoxF()
    scale = 144 / max(box.width(), box.height())
    img = QImage(round(box.width() * scale), round(box.height() * scale), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(img)
