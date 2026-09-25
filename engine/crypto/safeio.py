"""Crash-safe JSON state. A power cut or a Windows file lock must never turn an existing state file into an empty one.
load_json: main file, else .bak; if a file exists but none parses -> raise (never silently start empty). save_json: tmp + fsync + replace (retries) + .bak copy."""
import os, json, time, shutil


def load_json(path, default):
    seen = False
    for f in (path, path + ".bak"):
        if os.path.exists(f):
            seen = True
            try:
                with open(f, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                continue
    if seen:
        raise RuntimeError("state file unreadable, refusing to start empty: " + path)
    return default


def save_json(path, obj, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=indent)
        fh.flush()
        os.fsync(fh.fileno())
    for i in range(8):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            time.sleep(0.25 * (i + 1))
    else:
        raise PermissionError("could not replace " + path)
    try:
        shutil.copyfile(path, path + ".bak")
    except Exception:
        pass
