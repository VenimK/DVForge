"""Exercise the adapter through its real C ABI, the same way RustDesk does.

Mirrors src/server/printer_service.rs:
    init(tag) -> i32                     0 = success
    get_prn_data(dur, &data, &data_len)  fills the out-params
    free_prn_data(data)
    uninit()
"""
import ctypes
import os
import sys
import time

DLL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "target", "release", "printer_driver_adapter.dll")
TAG = sys.argv[2] if len(sys.argv) > 2 else "AdapterSelfTest"

lib = ctypes.CDLL(DLL)
lib.init.argtypes = [ctypes.c_char_p]
lib.init.restype = ctypes.c_int
lib.uninit.argtypes = []
lib.uninit.restype = None
lib.get_prn_data.argtypes = [ctypes.c_uint,
                             ctypes.POINTER(ctypes.POINTER(ctypes.c_char)),
                             ctypes.POINTER(ctypes.c_uint)]
lib.get_prn_data.restype = None
lib.free_prn_data.argtypes = [ctypes.POINTER(ctypes.c_char)]
lib.free_prn_data.restype = None

fails = []

def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not cond:
        fails.append(label)

def poll():
    data = ctypes.POINTER(ctypes.c_char)()
    dlen = ctypes.c_uint(0)
    lib.get_prn_data(1000, ctypes.byref(data), ctypes.byref(dlen))
    if not data or dlen.value == 0:
        return None
    out = ctypes.string_at(data, dlen.value)
    lib.free_prn_data(data)
    return out

print("DLL:", DLL)
print()

print("1. init")
rc = lib.init(TAG.encode())
check("init returns 0", rc == 0, "rc=%d" % rc)

spool = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     TAG, "printer-spool")
check("spool dir created", os.path.isdir(spool), spool)

print("\n2. empty spool yields nothing")
check("no data when idle", poll() is None)

print("\n3. a completed job is captured")
payload = b"%XPS-FAKE-JOB%" + bytes(range(256)) * 40
job = os.path.join(spool, "job.prn")
with open(job, "wb") as f:
    f.write(payload)
got = poll()
check("data returned", got is not None)
check("bytes match exactly", got == payload,
      "got %s bytes, expected %s" % (len(got) if got else 0, len(payload)))
check("job file removed after capture", not os.path.exists(job))

print("\n4. job is not returned twice")
check("second poll is empty", poll() is None)

print("\n5. a file still held open is skipped, then picked up on release")
held = open(os.path.join(spool, "locked.prn"), "wb")
held.write(b"partial data still being written by the spooler")
held.flush()
check("skipped while open", poll() is None)
held.close()
got = poll()
check("captured once closed", got is not None and got.startswith(b"partial data"))

print("\n6. ordering: oldest job first")
for name, body in (("a.prn", b"FIRST"), ("b.prn", b"SECOND")):
    with open(os.path.join(spool, name), "wb") as f:
        f.write(body)
    time.sleep(1.1)
check("oldest returned first", poll() == b"FIRST")
check("newest returned second", poll() == b"SECOND")

print("\n7. init clears stale jobs")
with open(os.path.join(spool, "stale.prn"), "wb") as f:
    f.write(b"left over from a previous run")
check("re-init returns 0", lib.init(TAG.encode()) == 0)
check("stale job discarded", poll() is None)

print("\n8. robustness")
lib.free_prn_data(ctypes.POINTER(ctypes.c_char)())
check("free(NULL) does not crash", True)
lib.get_prn_data(1000, None, None)
check("get_prn_data(NULL, NULL) does not crash", True)
check("init(NULL) is rejected", lib.init(None) != 0)

lib.uninit()
check("uninit does not crash", True)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL CHECKS PASSED")
