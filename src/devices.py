"""Find e-readers plugged in over USB and copy books onto them. No GTK.

Most e-readers (Kobo, PocketBook, Tolino, older Kindles, many Android-free
readers) show up as an ordinary USB drive, so "sending" a book is just copying
the file into the folder the reader scans. We recognise a reader by the marker
files it keeps on its drive, and fall back to treating any removable drive with
a Books folder as a generic reader.

Newer Kindles (2022 Scribe/Paperwhite onward) connect over MTP instead and
won't appear here; and Kindles can't read EPUB over USB at all, so a Kindle is
detected but flagged as unable to take EPUB files.
"""
import getpass
import os
import shutil
from dataclasses import dataclass

from . import ebook


@dataclass
class Device:
    name: str          # what to call it in the UI, e.g. "Kobo Clara 2E"
    kind: str          # kobo | kindle | pocketbook | generic
    mount: str         # mount point
    books_dir: str     # where books get copied
    formats: tuple     # suffixes the reader can open

    def accepts(self, path):
        return os.path.splitext(path)[1].lower() in self.formats

    def free_bytes(self):
        try:
            return shutil.disk_usage(self.mount).free
        except OSError:
            return 0


def _mount_roots():
    user = getpass.getuser()
    roots = [f"/run/media/{user}", f"/media/{user}", "/media"]
    seen, mounts = set(), []
    for root in roots:
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and os.path.ismount(entry.path) and entry.path not in seen:
                seen.add(entry.path)
                mounts.append(entry.path)
    return mounts


def _exists(mount, *parts):
    return os.path.exists(os.path.join(mount, *parts))


def _identify(mount):
    label = os.path.basename(mount)
    if _exists(mount, ".kobo"):
        return Device("Kobo", "kobo", mount, mount,
                      (".epub", ".kepub", ".pdf", ".cbz", ".txt"))
    if _exists(mount, "system", "version.txt") or (
            _exists(mount, "documents") and label.lower().startswith("kindle")):
        return Device("Kindle", "kindle", mount, os.path.join(mount, "documents"),
                      (".azw3", ".azw", ".mobi", ".pdf", ".txt", ".kfx"))
    if _exists(mount, "system", "config", "books.db") or \
            _exists(mount, "system", "explorer-3"):
        return Device("PocketBook", "pocketbook", mount, os.path.join(mount, "Books"),
                      (".epub", ".pdf", ".fb2", ".mobi", ".djvu", ".cbz", ".txt"))
    for folder in ("Books", "books", "eBooks", "Ebooks"):
        if _exists(mount, folder):
            return Device(label, "generic", mount, os.path.join(mount, folder),
                          (".epub", ".pdf"))
    return None


def find_devices():
    """Every mounted e-reader we can recognise, in mount order."""
    found = []
    for mount in _mount_roots():
        try:
            dev = _identify(mount)
        except OSError:
            dev = None
        if dev is not None:
            found.append(dev)
    return found


def device_by_mount(mount):
    for dev in find_devices():
        if dev.mount == mount:
            return dev
    return None


def send(device, path, *, title, author, series="", series_index=0.0):
    """Copy one ebook onto the device under a readable name. Returns the
    destination path. Overwrites an earlier copy of the same book so re-sending
    after editing metadata replaces it rather than duplicating it."""
    if not device.accepts(path):
        raise ebook.EbookError(f"{device.name} can't open {os.path.splitext(path)[1]} files")
    ext = os.path.splitext(path)[1].lower()
    name = ebook.device_filename(title, author, series, series_index, ext)
    os.makedirs(device.books_dir, exist_ok=True)
    dest = os.path.join(device.books_dir, name)
    if os.path.getsize(path) > device.free_bytes() and not os.path.exists(dest):
        raise ebook.EbookError(f"Not enough space on {device.name}")
    tmp = dest + ".part"
    shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    try:
        os.sync()
    except AttributeError:
        pass
    return dest
