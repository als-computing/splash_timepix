"""Centralized color theme based on ALS Style Guide."""

# =============================================================================
# ALS Brand Colors
# =============================================================================

# Primary Blues (use in ~50% of UI chrome)
BLUE_PRIMARY = "#006ba6"  # 50% - Main brand color
BLUE_DARK = "#003859"  # 20% - Headers, emphasis
BLUE_LIGHT_1 = "#00b5ec"  # 10% - Accents
BLUE_LIGHT_2 = "#0085ca"  # 10% - Accents
BLUE_LIGHT_3 = "#00587c"  # 10% - Accents

# Greys (unlimited use)
GREY_DARK = "#636669"
GREY_LIGHT = "#b1b3b3"

# Tertiary Colors (use sparingly for status/accents)
TERTIARY_MUD = "#672e46"
TERTIARY_PURPLE = "#5d4777"
TERTIARY_BLUE = "#4298b5"
TERTIARY_TEAL = "#007681"
TERTIARY_GREEN = "#74aa50"
TERTIARY_YELLOW = "#eaaa00"
TERTIARY_ORANGE = "#d57800"
TERTIARY_RED = "#e04e38"

# =============================================================================
# Semantic Colors (mapped from ALS palette)
# =============================================================================

# Status indicators
STATUS_OK = TERTIARY_GREEN
STATUS_STREAMING = TERTIARY_BLUE
STATUS_WARNING = TERTIARY_YELLOW
STATUS_ERROR = TERTIARY_RED
STATUS_INACTIVE = GREY_LIGHT

# Buttons
BUTTON_START = TERTIARY_GREEN
BUTTON_PREVIEW = BLUE_PRIMARY
BUTTON_SIMULATOR = TERTIARY_PURPLE
BUTTON_REPLAY = TERTIARY_ORANGE
BUTTON_STOP = TERTIARY_RED
BUTTON_SECONDARY = GREY_DARK

# Backgrounds
BG_DARK = "#1a1a2e"  # Darkest - terminal/heatmap backgrounds
BG_PANEL = "#252536"  # Panel backgrounds
BG_WIDGET = "#2d2d3d"  # Widget/card backgrounds
BG_BUTTON_GROUP = "#3a3a4a"  # Grouped button container

# Borders
BORDER_DEFAULT = GREY_DARK
BORDER_SUBTLE = "#404050"

# Text
TEXT_PRIMARY = "#e0e0e0"
TEXT_SECONDARY = GREY_LIGHT
TEXT_MUTED = "#888888"

# =============================================================================
# Style Snippets
# =============================================================================


def button_style(bg_color: str, text_color: str = "white") -> str:
    """Generate consistent button stylesheet."""
    return f"""
        QPushButton {{
            background-color: {bg_color};
            color: {text_color};
            font-weight: bold;
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background-color: {bg_color};
            opacity: 0.9;
        }}
        QPushButton:pressed {{
            background-color: {bg_color};
        }}
        QPushButton:disabled {{
            background-color: {GREY_DARK};
            color: {GREY_LIGHT};
        }}
    """


def secondary_button_style() -> str:
    """Style for secondary/less prominent buttons."""
    return f"""
        QPushButton {{
            background-color: {BG_WIDGET};
            color: {TEXT_PRIMARY};
            padding: 10px 20px;
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 4px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background-color: {BG_BUTTON_GROUP};
        }}
        QPushButton:pressed {{
            background-color: {BG_PANEL};
        }}
    """


def group_box_style() -> str:
    """Style for QGroupBox containers."""
    return f"""
        QGroupBox {{
            font-weight: bold;
            border: 1px solid {BORDER_SUBTLE};
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 8px;
            background-color: {BG_PANEL};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
            color: {TEXT_PRIMARY};
        }}
    """


def terminal_style() -> str:
    """Style for terminal/log output widgets."""
    return f"""
        QPlainTextEdit {{
            background-color: {BG_DARK};
            color: {TEXT_PRIMARY};
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 11px;
            border: 1px solid {BORDER_SUBTLE};
            border-top: none;
            border-radius: 0 0 4px 4px;
        }}
    """


def input_style() -> str:
    """Style for input widgets (QLineEdit, QSpinBox, QComboBox)."""
    return f"""
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
            background-color: {BG_DARK};
            color: {TEXT_PRIMARY};
            border: 1px solid {BORDER_SUBTLE};
            border-radius: 4px;
            padding: 4px 8px;
        }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
            border-color: {BLUE_PRIMARY};
        }}
        QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
            background-color: {BG_BUTTON_GROUP};
            color: {TEXT_MUTED};
            border-color: {BORDER_SUBTLE};
        }}
    """


def heatmap_background_style() -> str:
    """Style for heatmap display area."""
    return f"background-color: {BG_DARK}; border: 1px solid {BORDER_SUBTLE};"


def checkable_button_style() -> str:
    """Style for mutually-exclusive checkable tool buttons (e.g. zoom modes)."""
    return f"""
        QPushButton {{
            background-color: {BG_WIDGET};
            color: {TEXT_PRIMARY};
            padding: 6px 12px;
            border: 1px solid {BORDER_DEFAULT};
            border-radius: 4px;
        }}
        QPushButton:checked {{
            background-color: {BLUE_LIGHT_2};
            color: white;
            border: 1px solid {BLUE_LIGHT_2};
        }}
        QPushButton:hover:!checked {{
            background-color: {BG_BUTTON_GROUP};
        }}
    """
