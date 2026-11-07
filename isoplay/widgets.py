"""Widgets the player is assembled from."""
import time

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
    QWidgetAction,
)

from isoplay.media import DISC_EXT, ago, origin, suffix
from isoplay.theme import BADGES, INK, OPTIONS_CSS, badge_art, svg_icon


class Overlay(QWidget):
    """Floating child window: the video is a native libVLC surface, so Qt widgets can't paint over it."""

    def __init__(self, owner, clickable):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint
        super().__init__(owner, flags if clickable else flags | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(owner.styleSheet())
        self.anim = QPropertyAnimation(self, b"windowOpacity", self, duration=180,
                                       easingCurve=QEasingCurve.OutCubic)
        self.anim.finished.connect(lambda: self.windowOpacity() < 0.01 and self.hide())
        self.shown = False

    def fade(self, show):
        if show == self.shown:
            return
        self.shown = show
        self.anim.stop()
        if show:
            if not self.isVisible():
                self.setWindowOpacity(0.0)
                self.show()
            self.anim.setDuration(140)
        else:
            self.anim.setDuration(320)
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(1.0 if show else 0.0)
        self.anim.start()


class Bars(QWidget):
    """Radial bars with a highlight travelling smoothly around them."""

    COUNT = 12
    TURNS = 1.1  # revolutions a second

    def __init__(self, size=76, weight=4):
        super().__init__(fixedSize=QSize(size, size))
        self.weight, self.percent = weight, None
        self.started = time.monotonic()
        self.spin = QTimer(self, interval=16, timeout=self.update)

    def showEvent(self, event):
        self.started = time.monotonic()
        self.spin.start()

    def hideEvent(self, event):
        self.spin.stop()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        outer = self.width() / 2 - self.weight / 2
        inner = outer * 0.58
        head = (time.monotonic() - self.started) * self.TURNS * 360
        painter.save()
        painter.translate(QRectF(self.rect()).center())
        for i in range(self.COUNT):
            angle = i * 360 / self.COUNT
            lit = (1 - ((angle - head) % 360) / 360) ** 2.4
            pen = QPen(QColor(245, 245, 247, round(30 + 225 * lit)), self.weight)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.save()
            painter.rotate(angle)
            painter.drawLine(QPointF(0, -inner - (outer - inner) * 0.12 * lit), QPointF(0, -outer))
            painter.restore()
        painter.restore()
        if self.percent is None:
            return
        font = painter.font()
        font.setPixelSize(round(self.height() * 0.24))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(INK))
        painter.drawText(self.rect(), Qt.AlignCenter, f"{self.percent}%")


class Loader(QWidget):
    """Centred wait indicator, painted straight onto the video."""

    def __init__(self, parent=None):
        super().__init__(parent, objectName="loader")
        self.bars = Bars()
        self.caption = QLabel(objectName="loaderCaption", alignment=Qt.AlignHCenter)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        column.addWidget(self.bars, alignment=Qt.AlignHCenter)
        column.addWidget(self.caption)

    def show_wait(self, text, percent=None):
        self.caption.setText(text)
        self.bars.percent = percent
        self.bars.update()
        self.adjustSize()


class ElidedLabel(QLabel):
    """Keep full text for accessibility; elide to the space actually allocated."""
    def __init__(self, text, mode=Qt.ElideRight, **kwargs):
        super().__init__(**kwargs)
        self.full_text, self.mode = text, mode
        self.setTextFormat(Qt.PlainText)
        self.setText(text)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self.full_text, self.mode, max(0, self.contentsRect().width())))


class PreferenceSwitch(QWidget):  # native menu checkmarks cannot be styled, so draw the toggle
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.setFixedSize(28, 16)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        action.toggled.connect(lambda _: self.update())

    def paintEvent(self, event):
        on = self.action.isChecked()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#0a84ff") if on else QColor(255, 255, 255, 42))
        painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        painter.setBrush(QColor(INK))
        painter.drawEllipse(QRectF(14 if on else 2, 2, 12, 12))


class PreferenceRow(QPushButton):
    def __init__(self, action):
        super().__init__(objectName="preferenceRow", cursor=Qt.PointingHandCursor)
        self.setAccessibleName(action.text())
        self.setAccessibleDescription("On" if action.isChecked() else "Off")
        self.setFixedHeight(38)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(18)
        label = QLabel(action.text(), objectName="preferenceLabel")
        label.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(PreferenceSwitch(action))
        self.clicked.connect(action.trigger)
        action.toggled.connect(lambda value: self.setAccessibleDescription("On" if value else "Off"))


class OptionsMenu(QMenu):
    def __init__(self, parent, disc_menus, automatic_updates, check_updates):
        super().__init__(parent)
        self.setObjectName("optionsMenu")
        self.setMinimumWidth(330)
        self.setStyleSheet(OPTIONS_CSS)
        for action in (disc_menus, automatic_updates):
            item = QWidgetAction(self)
            item.setText(action.text())
            item.setDefaultWidget(PreferenceRow(action))
            item.triggered.connect(action.trigger)
            self.addAction(item)
        self.addSeparator()
        self.addAction(check_updates)


class MediaBadge(QWidget):
    def __init__(self, kind, source):
        super().__init__(objectName="mediaBadge")
        if kind not in BADGES:
            kind = "disc" if suffix(source) in DISC_EXT else "video"
        label, art = BADGES[kind]
        self.art = badge_art(art)
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.setFixedSize(36, 36)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        target = QRect(QPoint(), self.art.size().scaled(self.size(), Qt.KeepAspectRatio))
        target.moveCenter(self.rect().center())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(target, self.art)


class RecentRow(QFrame):
    def __init__(self, item, on_open):
        super().__init__(objectName="row", cursor=Qt.PointingHandCursor)
        self.src, self.on_open = item["src"], on_open
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setHorizontalSpacing(12)
        lay.setVerticalSpacing(4)
        title = ElidedLabel(item["name"], mode=Qt.ElideMiddle, objectName="rowtitle")
        badge = MediaBadge(item.get("kind"), self.src)
        lay.addWidget(badge, 0, 0, 2, 1)
        lay.addWidget(title, 0, 1)
        lay.addWidget(ElidedLabel(origin(self.src), objectName="rowmeta"), 1, 1)
        lay.addWidget(QLabel(ago(item["ts"]), objectName="rowmeta"), 0, 2, 2, 1, Qt.AlignRight | Qt.AlignVCenter)
        lay.setColumnStretch(1, 1)
        self.setToolTip(f"{badge.accessibleName()}\n{self.src}")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_open(self.src)


def disc_label(root):
    return root.strip("/").replace("_", " ").title() or "Disc"


class DiscChooser(QWidget):
    """An opening menu within the player, only for images with multiple discs."""
    selected = Signal(str)
    cancelled = Signal()

    def __init__(self):
        super().__init__(objectName="welcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.addStretch()
        holder = QHBoxLayout()
        holder.addStretch()
        content = QWidget()
        content.setMaximumWidth(500)
        content.setMinimumWidth(360)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        column.addWidget(QLabel("Choose a Disc", objectName="appname"))
        column.addWidget(QLabel("This image contains more than one disc.", objectName="muted"))
        self.choices = QVBoxLayout()
        self.choices.setSpacing(10)
        column.addLayout(self.choices)
        cancel = QPushButton("Cancel", objectName="link", cursor=Qt.PointingHandCursor)
        cancel.clicked.connect(self.cancelled)
        column.addWidget(cancel, alignment=Qt.AlignLeft)
        holder.addWidget(content)
        holder.addStretch()
        outer.addLayout(holder)
        outer.addStretch()

    def set_discs(self, roots):
        while self.choices.count():
            item = self.choices.takeAt(0)
            item.widget().deleteLater()
        for root in roots:
            button = QPushButton(disc_label(root), objectName="action", cursor=Qt.PointingHandCursor,
                                 icon=svg_icon("disc", 32), iconSize=QSize(24, 24))
            button.setMinimumHeight(56)
            button.clicked.connect(lambda _=False, selected=root: self.selected.emit(selected))
            self.choices.addWidget(button)
