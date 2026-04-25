"""시스템 트레이 + 메인 GUI 컨트롤러."""

from __future__ import annotations

import sys
import platform
import webbrowser

try:
    import tkinter as tk
    _HAS_TKINTER = True
except Exception:
    _HAS_TKINTER = False

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_PYSTRAY = True
except Exception:
    _HAS_PYSTRAY = False


def _create_icon_image() -> "Image.Image":
    img = Image.new("RGB", (16, 16), color=(26, 26, 46))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, 13, 11], outline=(74, 144, 217), width=1)
    draw.rectangle([4, 4, 11, 9], fill=(0, 204, 102))
    draw.line([6, 12, 9, 14], fill=(224, 224, 224))
    return img


_session_ref: dict = {}
_status_window = None
_error_history_window = None
_tray_icon = None
_tk_root: tk.Tk | None = None
_status_window_close_target = None
_IS_MACOS = platform.system() == "Darwin"


def _get_status_window():
    global _status_window
    if _status_window is None:
        from gui_windows import StatusWindow
        _status_window = StatusWindow(_session_ref)
    _bind_status_window_close_once()
    return _status_window


def _get_error_history_window():
    global _error_history_window
    if _error_history_window is None:
        from gui_windows import ErrorHistoryWindow
        _error_history_window = ErrorHistoryWindow(_session_ref)
    return _error_history_window


def _open_dashboard(_=None) -> None:
    if _tk_root:
        if _IS_MACOS:
            _tk_root.deiconify()
        _tk_root.after(0, _show_dashboard)


def _open_error_history(_=None) -> None:
    if _tk_root:
        _tk_root.after(0, lambda: _get_error_history_window().show())


def _open_settings(_=None) -> None:
    def _run():
        try:
            from first_run import show_setup_window
            show_setup_window()
        except Exception as e:
            print(f"[tray_app] 설정 창 열기 실패: {e}")
    if _tk_root:
        _tk_root.after(0, _run)


def _quit_app(_=None) -> None:
    global _tray_icon, _tk_root
    if _tray_icon:
        _tray_icon.stop()
        _tray_icon = None
    if _tk_root:
        try:
            _tk_root.quit()
            _tk_root.destroy()
        except Exception:
            pass
        _tk_root = None
    import os
    os._exit(0)


def _on_error_callback(result: dict) -> None:
    if _tk_root:
        _tk_root.after(0, lambda: _show_dashboard_from_error(result))


def _bind_status_window_close_once() -> None:
    global _status_window_close_target
    if not _IS_MACOS or not _HAS_TKINTER or _status_window is None:
        return

    window = getattr(_status_window, "_win", None)
    if window is None or window is _status_window_close_target:
        return

    try:
        window.protocol("WM_DELETE_WINDOW", _hide_dashboard)
        _status_window_close_target = window
    except tk.TclError:
        pass


def _show_dashboard() -> None:
    status_window = _get_status_window()
    status_window.show()
    _bind_status_window_close_once()
    _restore_dashboard_window()


def _show_dashboard_from_error(result: dict) -> None:
    status_window = _get_status_window()
    status_window.on_error_detected(result)
    _bind_status_window_close_once()
    _restore_dashboard_window()


def _restore_dashboard_window() -> None:
    status_window = _get_status_window()
    window = getattr(status_window, "_win", None)
    if window is None:
        return

    try:
        window.deiconify()
        window.lift()
        window.focus_force()
    except tk.TclError:
        pass


def _hide_dashboard() -> None:
    status_window = _get_status_window()
    window = getattr(status_window, "_win", None)
    if window is None:
        return

    try:
        window.withdraw()
    except tk.TclError:
        pass


def _register_macos_app_hooks() -> None:
    if not _IS_MACOS or not _tk_root:
        return

    def _reopen_app(*_args):
        _show_dashboard()
        return None

    def _quit_from_app_menu(*_args):
        _quit_app()
        return None

    for command_name, handler in (
        ("::tk::mac::ReopenApplication", _reopen_app),
        ("tk::mac::ReopenApplication", _reopen_app),
        ("::tk::mac::Quit", _quit_from_app_menu),
        ("tk::mac::Quit", _quit_from_app_menu),
    ):
        try:
            _tk_root.createcommand(command_name, handler)
        except tk.TclError:
            pass


def create_tray(session_index_ref: dict) -> None:
    global _session_ref, _tray_icon, _tk_root

    if not _HAS_TKINTER:
        print("[tray_app] tkinter가 설치되지 않아 GUI를 시작할 수 없습니다.")
        return

    _session_ref = session_index_ref

    try:
        from monitor import set_error_callback
        set_error_callback(_on_error_callback)
    except ImportError:
        pass

    if _IS_MACOS:
        _tk_root = tk.Tk()
        _tk_root.protocol('WM_DELETE_WINDOW', _tk_root.withdraw)
        _tk_root.withdraw()
        _register_macos_app_hooks()
        _show_dashboard()

        print("[tray_app] macOS GUI가 시작되었습니다.")

        try:
            _tk_root.mainloop()
        except KeyboardInterrupt:
            _quit_app()
        return

    if not _HAS_PYSTRAY:
        print("[tray_app] pystray가 설치되지 않아 시스템 트레이를 사용할 수 없습니다.")
        return

    menu = pystray.Menu(
        pystray.MenuItem("대시보드 열기", _open_dashboard),
        pystray.MenuItem("에러 이력", _open_error_history),
        pystray.MenuItem("설정", _open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", _quit_app),
    )

    icon_image = _create_icon_image()
    _tray_icon = pystray.Icon("ai_assistant", icon_image, "AI 업무 어시스턴트", menu)

    # Windows/Linux: tkinter 메인 스레드, pystray 데몬 스레드
    import threading

    _tk_root = tk.Tk()
    _tk_root.withdraw()

    tray_thread = threading.Thread(target=_tray_icon.run, daemon=True)
    tray_thread.start()

    print("[tray_app] 시스템 트레이가 시작되었습니다.")

    try:
        _tk_root.mainloop()
    except KeyboardInterrupt:
        _quit_app()


def create_web_tray() -> None:
    if platform.system() != 'Windows':
        return

    if not _HAS_PYSTRAY:
        print("[tray_app] pystray가 설치되지 않아 웹 트레이를 사용할 수 없습니다.")
        return

    from local_server import get_dashboard_url

    def _open_web_dashboard(_=None) -> None:
        webbrowser.open(get_dashboard_url())

    def _quit_web_app(_=None) -> None:
        if _tray_icon:
            _tray_icon.stop()
        import os

        os._exit(0)

    global _tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("대시보드 열기", _open_web_dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", _quit_web_app),
    )

    icon_image = _create_icon_image()
    _tray_icon = pystray.Icon("ai_assistant_web", icon_image, "AI 업무 어시스턴트", menu)
    print("[tray_app] 웹 대시보드용 시스템 트레이가 시작되었습니다.")
    try:
        _tray_icon.run()
    except KeyboardInterrupt:
        _quit_web_app()
