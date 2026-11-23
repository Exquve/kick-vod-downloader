"""Sistem tepsisi ikonu (pystray)."""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


def run_tray(icon_path: str, on_open: Callable[[], None], on_folder: Callable[[], None],
             on_quit: Callable[[], None]) -> None:
    import pystray
    from PIL import Image

    image = Image.open(icon_path)

    def _quit(icon, _item):
        icon.visible = False
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Telefondan aç (QR)", lambda i, _: on_open(), default=True),
        pystray.MenuItem("İndirme klasörünü aç", lambda i, _: on_folder()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Çıkış", _quit),
    )
    icon = pystray.Icon("KickIndirici", image, "Kick VOD İndirici", menu)
    icon.run()
