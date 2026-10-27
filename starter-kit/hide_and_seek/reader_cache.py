"""Archive of the reader's LLM outputs by fingerprint, replayed offline.

Taken from the team's oblivion project (`entraide/modele.py`: `Enregistreur` fingerprints a request,
`ModeleRejeu` replays it without network and refuses a stale archive; `entraide/commun.py`: atomic
write). The key includes the prompt version and the model: changing the prompt invalidates the
archive. In the final the arena serves an identical supplier e-mail for an identical message, so a
repeated reply is read in 0 s instead of up to 20 s on a 60-second budget - and every reading can be
replayed in front of the jury.
HIDE_AND_SEEK_READER_CACHE = auto (archive, then network) | replay (archive only) | off
"""

import hashlib
import json
import os
import threading
from pathlib import Path

MODE = os.environ.get("HIDE_AND_SEEK_READER_CACHE", "auto")
FILE = Path(os.environ.get("HIDE_AND_SEEK_READER_ARCHIVE", "data/reader_archive.json"))
_lock, _archive = threading.Lock(), {}


def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def complete(lines, call, version):
    """(output, replayed): `call(lines)` only when the fingerprint is unknown; in replay mode an
    unknown fingerprint raises LookupError."""
    key = fingerprint([version, os.environ.get("LLM_MODEL", ""), lines])
    with _lock:
        if not _archive and MODE != "off" and FILE.exists():
            try:
                _archive.update(json.loads(FILE.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        if MODE != "off" and key in _archive:
            return _archive[key]["output"], True
    if MODE == "replay":
        raise LookupError(f"replay: unknown fingerprint {key[:12]}")
    out = call(lines)
    if MODE != "off":
        with _lock:
            _archive[key] = {"output": out, "lines": lines}
            try:
                write_atomic(FILE, _archive)
            except OSError:
                pass
    return out, False
