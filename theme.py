"""Mono Industrial theme.

Colours lifted from the Notepad++ theme of the same name (monoindustrial, Fabio Zendhi
Nagao, 2008) -- PowerEditor/installer/themes/Mono Industrial.xml. The GlobalStyles block
supplies the chrome colours and the `json` LexerType supplies the syntax colours used in
the feature value editor.
"""

from __future__ import annotations

import re
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --- GlobalStyles ---------------------------------------------------------
EDITOR_BG = "#222C28"  # Default Style / Global override bgColor
CHROME_BG = "#2E3436"  # Line number margin bgColor
CURRENT_LINE = "#2C3833"  # Current line background colour
FG = "#FFFFFF"  # Default Style fgColor
SELECT_BG = "#919994"  # Selected text colour
SELECT_FG = "#000000"
CARET = "#FFFFFF"  # Caret colour
BORDER = "#555753"  # Fold margin fgColor
MUTED = "#888A85"  # Indent guideline style
WARNING = "#FCE94F"  # Brace highlight style
ERROR = "#EF2929"  # Bad brace colour

# --- json LexerType -------------------------------------------------------
JSON_DEFAULT = "#C3BE98"
JSON_NUMBER = "#FF8000"
JSON_STRING = "#DCDCCC"
JSON_PROPERTY = "#8CD0D3"
JSON_OPERATOR = "#E3CEAB"
JSON_KEYWORD = "#18AF8A"

ACCENT = JSON_KEYWORD  # teal, used for section titles, focus rings and success text

# The theme names DejaVu Sans Mono; fall back through what Windows/macOS actually ship.
MONO_PREFERENCES = (
    "DejaVu Sans Mono",
    "Consolas",
    "Menlo",
    "Cascadia Mono",
    "Courier New",
)
UI_PREFERENCES = ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial")


def _resolve(preferences: tuple[str, ...], fallback: str) -> str:
    available = set(tkfont.families())
    for name in preferences:
        if name in available:
            return name
    return fallback


def apply_theme(root: tk.Misc) -> dict[str, tuple]:
    """Restyle ttk and return the fonts the GUI should use."""
    mono = (_resolve(MONO_PREFERENCES, "Courier"), 11)
    ui = (_resolve(UI_PREFERENCES, "TkDefaultFont"), 9)
    ui_bold = (*ui, "bold")

    root.configure(background=CHROME_BG)

    style = ttk.Style(root)
    style.theme_use("clam")  # the most themable built-in theme

    style.configure(
        ".",
        background=CHROME_BG,
        foreground=FG,
        fieldbackground=EDITOR_BG,
        bordercolor=BORDER,
        lightcolor=CHROME_BG,
        darkcolor=CHROME_BG,
        troughcolor=EDITOR_BG,
        focuscolor=ACCENT,
        font=ui,
    )

    style.configure("TFrame", background=CHROME_BG)
    style.configure("TLabel", background=CHROME_BG, foreground=FG, font=ui)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Ok.TLabel", foreground=ACCENT)
    style.configure("Error.TLabel", foreground=ERROR)
    style.configure("Warning.TLabel", foreground=WARNING)

    style.configure(
        "TLabelframe",
        background=CHROME_BG,
        bordercolor=BORDER,
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label", background=CHROME_BG, foreground=ACCENT, font=ui_bold
    )

    style.configure(
        "TButton",
        background=EDITOR_BG,
        foreground=FG,
        bordercolor=BORDER,
        relief="flat",
        padding=(10, 4),
        font=ui,
    )
    style.map(
        "TButton",
        background=[
            ("disabled", CHROME_BG),
            ("pressed", ACCENT),
            ("active", CURRENT_LINE),
        ],
        foreground=[("disabled", BORDER), ("pressed", SELECT_FG)],
        bordercolor=[("active", ACCENT), ("focus", ACCENT)],
    )

    # Stop is destructive-ish and only live while a command runs.
    style.configure("Stop.TButton", foreground=ERROR)
    style.map(
        "Stop.TButton",
        background=[
            ("disabled", CHROME_BG),
            ("pressed", ERROR),
            ("active", CURRENT_LINE),
        ],
        foreground=[("disabled", BORDER), ("pressed", SELECT_FG), ("active", ERROR)],
    )

    # clam's indicator uses indicatorbackground/indicatorforeground -- the
    # indicatorcolor option belongs to other themes and is silently ignored here.
    style.configure(
        "TCheckbutton",
        background=CHROME_BG,
        foreground=FG,
        indicatorbackground=EDITOR_BG,
        indicatorforeground=FG,
        indicatorsize=16,  # clam defaults to ~10
        indicatormargin=(2, 2, 8, 2),
        upperbordercolor=BORDER,
        lowerbordercolor=BORDER,
        bordercolor=BORDER,
        padding=(0, 4),
        font=ui,
    )
    style.map(
        "TCheckbutton",
        background=[("active", CHROME_BG)],
        foreground=[("disabled", BORDER)],
        # The box keeps the editor background in every state; the white tick alone
        # signals checked.
        indicatorbackground=[
            ("disabled", CHROME_BG),
            ("active", CURRENT_LINE),
        ],
        indicatorforeground=[("disabled", BORDER), ("selected", FG)],
        upperbordercolor=[("selected", ACCENT), ("active", ACCENT)],
        lowerbordercolor=[("selected", ACCENT), ("active", ACCENT)],
    )

    style.configure(
        "TEntry",
        fieldbackground=EDITOR_BG,
        foreground=FG,
        insertcolor=CARET,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=4,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
        foreground=[("readonly", JSON_DEFAULT), ("disabled", BORDER)],
    )

    style.configure(
        "TCombobox",
        fieldbackground=EDITOR_BG,
        background=EDITOR_BG,
        foreground=FG,
        arrowcolor=JSON_DEFAULT,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", EDITOR_BG), ("disabled", CHROME_BG)],
        foreground=[("disabled", BORDER)],
        arrowcolor=[("disabled", BORDER), ("active", ACCENT)],
        bordercolor=[("focus", ACCENT), ("active", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
    )

    # The combobox dropdown is a classic Tk listbox and only listens to options.
    root.option_add("*TCombobox*Listbox.background", EDITOR_BG)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", SELECT_FG)
    root.option_add("*TCombobox*Listbox.font", mono)

    # Classic tk.Scrollbar is drawn by the OS on Windows and ignores colour options,
    # so text widgets get a ttk scrollbar instead -- clam draws it entirely itself.
    for orientation in ("Vertical", "Horizontal"):
        name = f"Editor.{orientation}.TScrollbar"
        style.configure(
            name,
            background=BORDER,  # the thumb
            troughcolor=EDITOR_BG,
            bordercolor=EDITOR_BG,
            arrowcolor=JSON_DEFAULT,
            lightcolor=BORDER,
            darkcolor=BORDER,
            gripcount=0,
            arrowsize=12,
            width=12,
        )
        style.map(
            name,
            background=[
                ("disabled", EDITOR_BG),
                ("pressed", ACCENT),
                ("active", MUTED),
            ],
            arrowcolor=[("disabled", EDITOR_BG), ("active", ACCENT)],
        )

    return {"mono": mono, "ui": ui, "ui_bold": ui_bold}


def style_text(widget: tk.Text, font: tuple, *, foreground: str = JSON_DEFAULT) -> None:
    widget.configure(
        background=EDITOR_BG,
        foreground=foreground,
        insertbackground=CARET,
        selectbackground=SELECT_BG,
        selectforeground=SELECT_FG,
        relief="flat",
        borderwidth=0,
        highlightthickness=0,
        font=font,
        padx=6,
        pady=4,
    )


class ScrollableText(tk.Frame):
    """A tk.Text with a themed ttk scrollbar, wrapped in a bordered frame.

    Replaces tkinter.scrolledtext.ScrolledText, whose classic tk.Scrollbar renders as a
    native (white) Windows scrollbar no matter what colours are set on it.

    The inner widget is exposed as `.text`; the border tracks focus.
    """

    def __init__(
        self,
        master: tk.Misc,
        font: tuple,
        *,
        foreground: str = JSON_DEFAULT,
        **text_options,
    ) -> None:
        super().__init__(
            master,
            background=EDITOR_BG,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            borderwidth=0,
        )
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.text = tk.Text(self, **text_options)
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.text.yview,
            style="Editor.Vertical.TScrollbar",
        )
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        style_text(self.text, font, foreground=foreground)
        self.text.bind("<FocusIn>", lambda _e: self.configure(highlightbackground=ACCENT))
        self.text.bind("<FocusOut>", lambda _e: self.configure(highlightbackground=BORDER))


# --- Windows title bar ----------------------------------------------------

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19  # Windows 10 builds before 20H1
_DWMWA_BORDER_COLOR = 34  # Windows 11 only
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def _colorref(hex_colour: str) -> int:
    """#RRGGBB -> Win32 COLORREF (0x00BBGGRR)."""
    red = int(hex_colour[1:3], 16)
    green = int(hex_colour[3:5], 16)
    blue = int(hex_colour[5:7], 16)
    return (blue << 16) | (green << 8) | red


def apply_window_chrome(window: tk.Tk) -> bool:
    """Darken the native title bar. Windows only; a no-op elsewhere.

    Tk has no control over the title bar, so this goes through DWM directly:
    immersive dark mode first, then the Windows 11 caption colours so the bar matches
    the theme rather than settling for the generic system dark grey.
    """
    if sys.platform != "win32":
        return False

    import ctypes

    window.update_idletasks()
    try:
        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
        # Declare signatures explicitly: a 64-bit HWND truncates through the default
        # int marshalling.
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = user32.GetParent(window.winfo_id())
    except (AttributeError, OSError):
        return False
    if not hwnd:
        return False

    def set_attribute(attribute: int, value: int) -> bool:
        data = ctypes.c_int(value)
        result = dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            attribute,
            ctypes.byref(data),
            ctypes.sizeof(data),
        )
        return result == 0

    dark = set_attribute(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or set_attribute(
        _DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY, 1
    )

    # Best-effort; these silently fail on Windows 10 and leave the dark mode above.
    set_attribute(_DWMWA_CAPTION_COLOR, _colorref(CHROME_BG))
    set_attribute(_DWMWA_TEXT_COLOR, _colorref(FG))
    set_attribute(_DWMWA_BORDER_COLOR, _colorref(BORDER))

    # Some builds only repaint the caption once the window is re-mapped.
    if dark and window.state() == "normal":
        window.withdraw()
        window.deiconify()
    return dark


# --- JSON syntax highlighting ---------------------------------------------

_JSON_TOKEN_RE = re.compile(
    r"""
      (?P<property>"(?:[^"\\]|\\.)*")\s*(?=:)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<keyword>\b(?:true|false|null)\b)
    | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<operator>[{}\[\],:])
    """,
    re.VERBOSE,
)

_JSON_TAGS = {
    "property": JSON_PROPERTY,
    "string": JSON_STRING,
    "keyword": JSON_KEYWORD,
    "number": JSON_NUMBER,
    "operator": JSON_OPERATOR,
}


def configure_json_tags(widget: tk.Text, font: tuple) -> None:
    for tag, colour in _JSON_TAGS.items():
        widget.tag_configure(tag, foreground=colour)
    # KEYWORD carries fontStyle="1" (bold) in the theme.
    widget.tag_configure("keyword", foreground=JSON_KEYWORD, font=(*font, "bold"))


def highlight_json(widget: tk.Text) -> None:
    """Retokenise the whole widget. Feature blobs are small; this is cheap."""
    content = widget.get("1.0", "end-1c")
    for tag in _JSON_TAGS:
        widget.tag_remove(tag, "1.0", "end")

    line_starts = [0]
    for index, character in enumerate(content):
        if character == "\n":
            line_starts.append(index + 1)

    def position(offset: int) -> str:
        low, high = 0, len(line_starts) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if line_starts[middle] <= offset:
                low = middle
            else:
                high = middle - 1
        return f"{low + 1}.{offset - line_starts[low]}"

    for match in _JSON_TOKEN_RE.finditer(content):
        tag = match.lastgroup
        if tag is None:
            continue
        start, end = match.span(tag)
        widget.tag_add(tag, position(start), position(end))
