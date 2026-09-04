"""Shared Qt stylesheet and colour tokens.

Kept in one place so the unlock window and the main window stay visually
consistent, and so a future light theme means editing one file.
"""

# Dark palette, chosen for long sessions in a terminal-heavy workflow.
BG          = "#14161a"
BG_ELEVATED = "#1c1f26"
BG_INPUT    = "#22262f"
BORDER      = "#2e3440"
BORDER_FOCUS= "#4c8bf5"
TEXT        = "#e4e7eb"
TEXT_DIM    = "#8b93a1"
ACCENT      = "#4c8bf5"
ACCENT_HOVER= "#5d97f7"
DANGER      = "#e5534b"
SUCCESS     = "#3fb950"
WARNING     = "#d29922"

CATEGORY_COLORS = {
    "login":   "#4c8bf5",
    "ssh_key": "#3fb950",
    "api_key": "#a371f7",
    "network": "#d29922",
    "note":    "#8b93a1",
}

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Inter", "Segoe UI", "DejaVu Sans", sans-serif;
    font-size: 13px;
}}

QLabel#Heading {{
    font-size: 22px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#SubHeading {{
    font-size: 13px;
    color: {TEXT_DIM};
}}
QLabel#FieldLabel {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
QLabel#Error {{
    color: {DANGER};
    font-size: 12px;
}}
QLabel#Success {{
    color: {SUCCESS};
    font-size: 12px;
}}

QLineEdit, QTextEdit, QComboBox, QSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {BORDER_FOCUS};
}}
QLineEdit:read-only {{
    background-color: {BG_ELEVATED};
    color: {TEXT_DIM};
}}
QLineEdit[monospace="true"] {{
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
}}

QPushButton {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background-color: {BORDER};
}}
QPushButton:pressed {{
    background-color: {BG_ELEVATED};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    background-color: {BG_ELEVATED};
}}
QPushButton#Primary {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#Danger {{
    color: {DANGER};
}}
QPushButton#Danger:hover {{
    background-color: {DANGER};
    color: #ffffff;
}}
QPushButton#Flat {{
    background: transparent;
    border: none;
    color: {TEXT_DIM};
    padding: 4px 8px;
}}
QPushButton#Flat:hover {{
    color: {TEXT};
    background-color: {BG_INPUT};
}}

QListWidget, QTreeWidget, QTableWidget {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{
    padding: 9px 10px;
    border-radius: 6px;
    margin: 1px 4px;
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
}}
QListWidget::item:hover:!selected {{
    background-color: {BORDER};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_DIM};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {TEXT_DIM};
}}

QFrame#Card {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QMenuBar, QMenu {{
    background-color: {BG_ELEVATED};
}}
QMenu::item:selected {{
    background-color: {ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

QStatusBar {{
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
}}

QProgressBar {{
    background-color: {BG_INPUT};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    border-radius: 3px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 16px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom-color: {ACCENT};
}}

QToolTip {{
    background-color: {BG_ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px;
    border-radius: 4px;
}}
"""
