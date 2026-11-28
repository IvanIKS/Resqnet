"""Galileo OSNMA check of recorded navigation data.

OSNMAlib (KU Leuven, EUPL-1.2) replays a recorded Galileo I/NAV stream and
verifies the OSNMA tags against the GSC public key. A good result proves the
navigation data (ephemeris, clock) really came from Galileo. It does not prove
where the reporter stood: OSNMA authenticates data, not the position.

A run takes seconds to minutes and OSNMAlib keeps a global Config, so runs are
serialised with a lock and every verdict is cached in memory and in a JSON
file. ``warm_up`` computes all samples in the background at startup.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("resqnet.osnma")

CHECKED_BY = "OSNMAlib v3.2.5"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "osnma" / "ublox_run"
RECORDING = DATA_DIR / "current_config.ubx"
PUBLIC_KEY = "OSNMA_PublicKey_2.xml"
MERKLE_TREE = "OSNMA_MerkleTree.xml"
CACHE_DIR = Path(os.getenv("OSNMA_CACHE_DIR") or Path(tempfile.gettempdir()) / "resqnet-osnma")

FORGED_SVIDS = (3, 7, 8)

SAMPLES = {
    "ublox_authentic": "Real u-blox recording of Galileo E1-B, 9 March 2026, unchanged.",
    "forged": "Same recording with one ephemeris bit (M0, word type 1) flipped on E03, E07, E08.",
}

NOTE = ("Recorded sample, not a live receiver. OSNMA authenticates Galileo navigation data "
        "(ephemeris, clock); it does not prove the reporter's position.")

_UBX_SYNC = b"\xb5\x62"
_RXM_SFRBX = (0x02, 0x13)
_GALILEO = 2
_GST_EPOCH = datetime(1999, 8, 22, tzinfo=timezone.utc)
_LEAP_SECONDS = 18

_TAG_RE = re.compile(r"Tag (AUTHENTICATED|FAILED)\s+\((\d+), (\d+)\) PRN_A: (\d+) GST_SF: (\d+) (\d+)")
_MACSEQ_RE = re.compile(r"MACSEQ (AUTHENTICATED|FAILED)\s+PRN_A: (\d+) GST_SF: (\d+) (\d+)")

_lock = threading.Lock()
_memo: dict[str, dict] = {}


def _ubx_checksum(body: bytes) -> bytes:
    ck_a = ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes((ck_a, ck_b))


def forge_ubx(src: Path, dst: Path, svids=FORGED_SVIDS) -> int:
    """Copy a UBX recording, flipping the lowest M0 bit of every word type 1 page of ``svids``.

    This imitates a spoofer that broadcasts altered ephemeris with valid page
    CRCs. Returns the number of pages changed.
    """
    data = bytearray(src.read_bytes())
    changed = 0
    i = 0
    while (i := data.find(_UBX_SYNC, i)) != -1 and i + 6 <= len(data):
        msg_class, msg_id, length = struct.unpack_from("<BBH", data, i + 2)
        end = i + 6 + length + 2
        body = bytes(data[i + 2:end - 2])
        if end > len(data) or _ubx_checksum(body) != data[end - 2:end]:
            i += 1
            continue
        payload = i + 6
        if ((msg_class, msg_id) == _RXM_SFRBX and length >= 8 + 32
                and data[payload] == _GALILEO and data[payload + 1] in svids):
            word0 = struct.unpack_from("<I", data, payload + 8)[0]
            if (word0 >> 24) & 0x3F == 1:
                data[payload + 12] ^= 0x01
                data[end - 2:end] = _ubx_checksum(bytes(data[i + 2:end - 2]))
                changed += 1
        i = end
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bytes(data))
    return changed


def sample_path(name: str) -> Path:
    if name == "ublox_authentic":
        return RECORDING
    if name == "forged":
        path = CACHE_DIR / "forged.ubx"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            forge_ubx(RECORDING, tmp)
            tmp.replace(path)
        return path
    raise KeyError(name)


def _gst(wn: int, tow: int) -> dict:
    utc = _GST_EPOCH + timedelta(weeks=wn, seconds=tow - _LEAP_SECONDS)
    return {"wn": wn, "tow": tow, "utc": utc.isoformat().replace("+00:00", "Z")}


class _TagCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.tags: list[tuple[bool, int, int, int, int, int]] = []
        self.macseq_failed = 0

    def emit(self, record):
        message = record.getMessage()
        if m := _TAG_RE.search(message):
            ok, prn_d, adkd, prn_a, wn, tow = m.group(1) == "AUTHENTICATED", *map(int, m.groups()[1:])
            self.tags.append((ok, prn_d, adkd, prn_a, wn, tow))
        elif (m := _MACSEQ_RE.search(message)) and m.group(1) == "FAILED":
            self.macseq_failed += 1


def _run_osnmalib(path: Path) -> _TagCollector:
    from osnma.input_formats.input_ubx import UBX
    from osnma.receiver.receiver import OSNMAReceiver

    collector = _TagCollector()
    tag_logger = logging.getLogger("osnma.osnma_core.tag_verification")
    with tempfile.TemporaryDirectory() as work:
        for name in (PUBLIC_KEY, MERKLE_TREE):
            shutil.copy(DATA_DIR / name, work)
        config = {
            "exec_path": work,
            "pubk_name": PUBLIC_KEY,
            "merkle_name": MERKLE_TREE,
            "log_file": False,
            "log_console": False,
            "do_status_log": False,
        }
        source = UBX(str(path))
        try:
            receiver = OSNMAReceiver(source, config)
            logging.getLogger("osnma").propagate = False
            tag_logger.addHandler(collector)
            receiver.start()
        finally:
            tag_logger.removeHandler(collector)
            source.file.close()
    return collector


def _verdict(sample: str, collector: _TagCollector, seconds: float) -> dict:
    data_tags = [t for t in collector.tags if t[2] in (0, 12)]
    failed = [t for t in collector.tags if not t[0]]
    failed_svids = sorted({t[1] for t in failed})
    authenticated_svids = sorted({t[1] for t in data_tags if t[0]} - set(failed_svids))
    times = sorted((t[4], t[5]) for t in collector.tags)

    if failed or collector.macseq_failed:
        verdict, reason = "rejected", "OSNMA tag verification failed: navigation data does not match the signed tags"
    elif authenticated_svids:
        verdict, reason = "authenticated", "all OSNMA tags verified with the GSC public key"
    else:
        verdict, reason = "rejected", "no navigation data could be authenticated"

    return {
        "verdict": verdict,
        "detail": {
            "sample": sample,
            "source": "recorded sample",
            "description": SAMPLES[sample],
            "reason": reason,
            "satellites_authenticated": [f"E{s:02d}" for s in authenticated_svids],
            "satellites_failed": [f"E{s:02d}" for s in failed_svids],
            "tags_authenticated": sum(1 for t in collector.tags if t[0]),
            "tags_failed": len(failed),
            "macseq_failed": collector.macseq_failed,
            "failed_tags": [
                {"prn_d": f"E{t[1]:02d}", "adkd": t[2], "prn_a": f"E{t[3]:02d}", "gst": _gst(t[4], t[5])}
                for t in failed[:10]
            ],
            "gst_first": _gst(*times[0]) if times else None,
            "gst_last": _gst(*times[-1]) if times else None,
            "checked_by": CHECKED_BY,
            "checked_in_seconds": round(seconds, 1),
            "note": NOTE,
        },
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_cache() -> dict:
    try:
        return json.loads((CACHE_DIR / "verdicts.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / "verdicts.json.tmp"
    tmp.write_text(json.dumps(cache, indent=1))
    tmp.replace(CACHE_DIR / "verdicts.json")


def check_sample(sample: str) -> dict:
    """Return ``{"verdict", "detail"}`` for a sample, running OSNMAlib only on a cache miss."""
    if sample not in SAMPLES:
        raise KeyError(sample)
    with _lock:
        if sample in _memo:
            return _memo[sample]
        path = sample_path(sample)
        key = f"{CHECKED_BY}:{_sha256(path)}"
        cache = _load_cache()
        cached = cache.get(sample)
        if cached and cached.get("key") == key:
            result = cached["result"]
        else:
            started = time.monotonic()
            collector = _run_osnmalib(path)
            result = _verdict(sample, collector, time.monotonic() - started)
            cache[sample] = {"key": key, "result": result}
            _save_cache(cache)
            logger.info("OSNMA %s: %s in %.1f s", sample, result["verdict"],
                        result["detail"]["checked_in_seconds"])
        _memo[sample] = result
        return result


def warm_up() -> threading.Thread:
    """Compute every sample's verdict in a background thread so API calls stay fast."""
    def run():
        for sample in SAMPLES:
            try:
                check_sample(sample)
            except Exception:
                logger.exception("OSNMA check of %s failed", sample)

    thread = threading.Thread(target=run, name="osnma-warm-up", daemon=True)
    thread.start()
    return thread
