# -*- coding: utf-8 -*-
"""Shared JSON file helpers.

The security core, the vault core and the ToDo store all keep their data in a
plain JSON file, and all three need the same guarantee: a crash (or a full
disk) while writing must never leave a truncated or half-written file behind,
because a truncated config is the file that makes the tab refuse to open.

Rather than keep three copies of that routine -- which drift apart the moment
one of them is fixed -- it lives here once and every module imports it.

    atomic_write_json(path, payload)
"""

import json
import os
import tempfile


def atomic_write_json(path, payload, prefix=".scriptdb-"):
    """Write ``payload`` as JSON to ``path`` so a crash cannot truncate it.

    The data is written to a temporary file in the same directory, flushed and
    fsynced to disk, and only then moved onto the target with ``os.replace``,
    which is atomic on every platform this project supports. A failure leaves
    the previous file untouched and the temporary file is removed.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
