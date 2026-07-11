import logging
from enum import Enum
import os
import sys
from io import BytesIO
import PySimpleGUI as sg
from PIL import Image
from psgtray import SystemTray
import typing
from typing import Sequence, Final, final

import sys, importlib
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
#    try:
#        sys.path.remove(str(parent))
#    except ValueError: # already removed
#        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that


if __name__ == '__main__' and __package__ is None:
    import_parents()

from . logging_lib import setup_logging

# Create a pure color icon
icon = Image.new('RGB', (64, 64), "red")
# Save icon to an in-memory file object
buffer = BytesIO()
icon.save(buffer, format='PNG')


class RightClickMenu:
    class Action(Enum):
        COPY = "Copy"
        PASTE = "Paste"
        SELECT_ALL = "Select All"
        CUT = "Cut"

    def __init__(self, logger: MyLogger):
        self.logger = logger
        self.menu_contents = ['', [self.Action.COPY.value, self.Action.PASTE.value, self.Action.SELECT_ALL.value, self.Action.CUT.value]]
#
    @property
    def events(self) -> Sequence[str]:
        return self.menu_contents[1]

    def do_clipboard_operation(self, event: str, window: sg.Window, element: sg.Element) -> None:
        if event == self.Action.SELECT_ALL.value:
            element.Widget.selection_clear()
            element.Widget.tag_add('sel', '1.0', 'end')
        elif event == self.Action.COPY.value:
            try:
                text = element.Widget.selection_get()
                window.TKroot.clipboard_clear()
                window.TKroot.clipboard_append(text)
            except:
                self.logger.debug('Nothing selected')
        elif event == self.Action.PASTE.value:
            element.Widget.insert(sg.tk.INSERT, window.TKroot.clipboard_get())
        elif event == self.Action.CUT.value:
            try:
                text = element.Widget.selection_get()
                window.TKroot.clipboard_clear()
                window.TKroot.clipboard_append(text)
                element.update('')
            except:
                self.logger.debug('Nothing selected')


class TrayMenu:
    class Action(Enum):
        SHOW_WINDOW = "Show Window"
        HIDE_WINDOW = "Hide Window"
        EXIT = "Exit"

    def __init__(self, window: sg.Window, logger: MyLogger):
        self.logger = logger
        self.window = window
        self.tray_contents = ['',
                              [self.Action.SHOW_WINDOW.value, self.Action.HIDE_WINDOW.value, '---',
                               self.Action.EXIT.value]]
        self.tooltip = "PowerTools"
        self.__tray = SystemTray(self.tray_contents, single_click_events=False, window=self.window,
                                 tooltip=self.tooltip,
                                 icon=sg.DEFAULT_BASE64_ICON)

    def close(self) -> None:
        self.__tray.close()

    def show_message(self, title: str, message: str) -> None:
        self.__tray.show_message(title, message)

    @property
    def key(self) -> str:
        return typing.cast(str, self.__tray.key)

    @property
    def events(self) -> Sequence[str]:
        return [self.Action.SHOW_WINDOW.value, self.Action.HIDE_WINDOW.value, sg.EVENT_SYSTEM_TRAY_ICON_DOUBLE_CLICKED,
                sg.WIN_CLOSE_ATTEMPTED_EVENT]

    def do_tray_operation(self, event: str) -> None:
        if event in (self.Action.SHOW_WINDOW.value, sg.EVENT_SYSTEM_TRAY_ICON_DOUBLE_CLICKED):
            self.window.un_hide()
            self.window.bring_to_front()
        elif event in (self.Action.HIDE_WINDOW.value, sg.WIN_CLOSE_ATTEMPTED_EVENT):
            self.window.hide()
            self.__tray.show_icon()  # if hiding window, better make sure the icon is visible


class SimpleTrayApp:
    def __init__(self, logger: MyLogger):
        self.logger = logger
        self.setup_hidpi()

        self.right_click_menu = RightClickMenu(logger)

        self.main_tab_layout = [
            [sg.Multiline(size=(80, 20), reroute_stderr=False, reroute_stdout=False, reroute_cprint=True, write_only=True,
                          key=self.Action.MLINE_KEY.value, right_click_menu=self.right_click_menu.menu_contents)],
            [sg.Button(self.Action.EXIT_BUTTON_KEY.value)]]

        self.window_layout = [
            [sg.Text(self.Names.program_name, expand_x=True, justification='center')],
            [sg.TabGroup([[sg.Tab(self.Names.main_tab_name.value, self.main_tab_layout),
                           ]],
                         tab_location="left"),
             ]
        ]
        self.window: sg.Window | None = None
        self.tray: TrayMenu | None = None

    def set_window_layout(self, layout: list[list[sg.Element]]) -> None:
        self.window_layout = layout

    def run(self) -> None:

        self.window = sg.Window(self.Names.program_name.value, self.window_layout, finalize=True,
                                enable_close_attempted_event=True,
                                size=(800, 600), )
        self.tray = TrayMenu(self.window, self.logger)
        sg.cprint(sg.get_versions())
        self.__event_loop()

    def event_proc(self, event: str, values: dict[str, str]) -> bool:
        if self.window and self.tray:
            return True
        else:
            return False

    class Action(Enum):
        MLINE_KEY = '-OUT-'
        EXIT_BUTTON_KEY = 'Exit'

    class Names(Enum):
        program_name = 'PowerTools'
        main_tab_name = 'Main'

    @staticmethod
    def setup_hidpi() -> None:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)

    def add_logger_handler(self) -> None:
        console_handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(console_handler)

    @final
    def __event_loop(self) -> None:
        if self.window and self.tray:
            mline: sg.Multiline = self.window[self.Action.MLINE_KEY.value]
            while True:
                event, values = self.window.read()

                # the event is converted to '__DOUBLE_CLICKED__' if double-clicked
                if event == self.tray.key:
                    event = values[event]

                if event in (sg.WIN_CLOSED, self.Action.EXIT_BUTTON_KEY.value):
                    break

                self.logger.debug(f"Processed event: {event}, values: {values}")

                if event in self.tray.events:
                    self.tray.do_tray_operation(event)
                elif event in self.right_click_menu.events:
                    self.right_click_menu.do_clipboard_operation(event, self.window, mline)

                if not self.event_proc(event, values):
                    break

            self.tray.close()  # optional but without a close, the icon may "linger" until moused over
            self.window.close()


if __name__ == '__main__':
    _logger = setup_logging()
    app = SimpleTrayApp(_logger)
    app.run()
