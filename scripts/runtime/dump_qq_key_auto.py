"""
Auto-dump NTQQ SQLCipher database keys from a running QQ process.

Zero prior knowledge required - no hardcoded PIDs, keys, addresses, or salts.
Finds the QQ main process (wrapper.node), scans memory for x'key_salt' patterns.

Usage (run as Administrator):
    python dump_qq_key_auto.py --qq 10001
    python dump_qq_key_auto.py --qq 10001 --pid 12345
"""

import sys
if sys.platform != "win32":
    raise SystemExit("Live NTQQ key extraction requires Windows. Use an existing decrypted snapshot with --no-fresh on other systems.")

import ctypes
import ctypes.wintypes as wt
from ctypes import windll, byref, sizeof, create_string_buffer
import struct, hashlib, hmac as hmac_mod, os, sys, re, time, json, argparse, sqlite3
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ── Windows API ──────────────────────────────────────────────────────────────

PROCESS_VM_READ           = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS        = 0x00000002
TH32CS_SNAPMODULE         = 0x00000008
TH32CS_SNAPMODULE32       = 0x00000010
MEM_COMMIT                = 0x1000
READABLE_PROTS            = {0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x60, 0x80}
MAX_REGION                = 256 * 1024 * 1024

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress',       ctypes.c_void_p),
        ('AllocationBase',    ctypes.c_void_p),
        ('AllocationProtect', wt.DWORD),
        ('RegionSize',        ctypes.c_size_t),
        ('State',             wt.DWORD),
        ('Protect',           wt.DWORD),
        ('Type',              wt.DWORD),
    ]

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize',              wt.DWORD),
        ('cntUsage',            wt.DWORD),
        ('th32ProcessID',       wt.DWORD),
        ('th32DefaultHeapID',   ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID',        wt.DWORD),
        ('cntThreads',          wt.DWORD),
        ('th32ParentProcessID', wt.DWORD),
        ('pcPriClassBase',      ctypes.c_long),
        ('dwFlags',             wt.DWORD),
        ('szExeFile',           ctypes.c_char * 260),
    ]

class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize',        wt.DWORD),
        ('th32ModuleID',  wt.DWORD),
        ('th32ProcessID', wt.DWORD),
        ('GlblcntUsage',  wt.DWORD),
        ('ProccntUsage',  wt.DWORD),
        ('modBaseAddr',   ctypes.POINTER(ctypes.c_byte)),
        ('modBaseSize',   wt.DWORD),
        ('hModule',       wt.HMODULE),
        ('szModule',      ctypes.c_char * 256),
        ('szExePath',     ctypes.c_char * 260),
    ]

# ── SQLCipher constants ──────────────────────────────────────────────────────

EXT_HEADER  = 1024
PAGE_SIZE   = 4096
SALT_SIZE   = 16
KEY_SIZE    = 32
IV_SIZE     = 16
HMAC_SIZE   = 20
RESERVE     = 48
HMAC_MASK   = 0x3a
KDF_ITER    = 4000
FAST_ITER   = 2

PRE_LOGIN_KEY = b"BD156D6710D54D8782F4"  # hardcoded in wrapper.node

# ── Process helpers ──────────────────────────────────────────────────────────

def find_qq_pids():
    snapshot = windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (ctypes.c_void_p(-1).value, -1):
        return []
    pe = PROCESSENTRY32()
    pe.dwSize = sizeof(PROCESSENTRY32)
    pids = []
    if windll.kernel32.Process32First(snapshot, byref(pe)):
        while True:
            name = pe.szExeFile.decode('utf-8', errors='replace').lower()
            if name == 'qq.exe':
                pids.append(pe.th32ProcessID)
            if not windll.kernel32.Process32Next(snapshot, byref(pe)):
                break
    windll.kernel32.CloseHandle(snapshot)
    return pids


def pid_has_module(pid, module_name):
    snapshot = windll.kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot in (ctypes.c_void_p(-1).value, -1, 0):
        return False
    me = MODULEENTRY32()
    me.dwSize = sizeof(MODULEENTRY32)
    found = False
    if windll.kernel32.Module32First(snapshot, byref(me)):
        while True:
            mod = me.szModule.decode('utf-8', errors='replace').lower()
            if mod == module_name.lower():
                found = True
                break
            if not windll.kernel32.Module32Next(snapshot, byref(me)):
                break
    windll.kernel32.CloseHandle(snapshot)
    return found


def find_qq_main_pid():
    pids = find_qq_pids()
    if not pids:
        return None
    print(f"QQ PIDs: {pids}")
    for pid in pids:
        if pid_has_module(pid, 'wrapper.node'):
            print(f"  PID {pid} has wrapper.node -> main process")
            return pid
    print(f"  WARNING: wrapper.node not found, trying first PID {pids[0]}")
    return pids[0]


def read_mem(handle, addr, size):
    buf = create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if windll.kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, byref(n)):
        return buf.raw[:n.value]
    return None


import qpaths
NT_DB_BASE = qpaths.source_base()

# ── DB discovery ─────────────────────────────────────────────────────────────

def find_db_dirs(qq_number):
    candidates = []
    g = os.path.join(NT_DB_BASE, 'nt_qq', 'global', 'nt_db')
    if os.path.isdir(g):
        candidates.append(('global', g))
    p = os.path.join(NT_DB_BASE, qq_number, 'nt_qq', 'nt_db')
    if os.path.isdir(p):
        candidates.append((qq_number, p))
    else:
        print(f"ERROR: DB dir not found: {p}")
    return candidates


def collect_db_info(db_dirs):
    global_dbs = []
    pa_map = {}
    for account, d in db_dirs:
        for fn in os.listdir(d):
            if not fn.lower().endswith('.db'):
                continue
            fp = os.path.join(d, fn)
            try:
                with open(fp, 'rb') as f:
                    hdr = f.read(EXT_HEADER + PAGE_SIZE)
                if len(hdr) < EXT_HEADER + PAGE_SIZE:
                    continue
                if hdr[:16] != b'SQLite header 3\x00':
                    continue
                page1 = hdr[EXT_HEADER: EXT_HEADER + PAGE_SIZE]
                salt_hex = page1[:SALT_SIZE].hex()
                if account == 'global':
                    global_dbs.append((account, fp, page1))
                else:
                    pa_map.setdefault(salt_hex, []).append((account, fp, page1))
            except (OSError, PermissionError):
                continue
    return global_dbs, pa_map


# ── Crypto helpers ───────────────────────────────────────────────────────────

def verify_key_hmac(page1_data, enc_key_bytes):
    """Validate enc_key against page 1 HMAC-SHA1."""
    salt = page1_data[:SALT_SIZE]
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    hmac_key = hashlib.pbkdf2_hmac('sha512', enc_key_bytes, hmac_salt, FAST_ITER, KEY_SIZE)
    data_end = PAGE_SIZE - RESERVE            # 4048
    # Page 1 HMAC skips the 16-byte salt prefix
    content  = page1_data[SALT_SIZE:data_end]
    iv       = page1_data[data_end:data_end + IV_SIZE]
    stored   = page1_data[data_end + IV_SIZE:data_end + IV_SIZE + HMAC_SIZE]
    # Page number is little-endian (not big-endian as RE doc states)
    computed = hmac_mod.new(hmac_key, content + iv + struct.pack('<I', 1), hashlib.sha1).digest()
    return hmac_mod.compare_digest(computed, stored)


def derive_enc_key(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha512', passphrase, salt, KDF_ITER, KEY_SIZE)


def derive_hmac_key(enc_key_bytes, salt):
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    return hashlib.pbkdf2_hmac('sha512', enc_key_bytes, hmac_salt, FAST_ITER, KEY_SIZE)


def decrypt_page(page_data, enc_key, skip_salt=0):
    """Decrypt a single SQLCipher page (AES-256-CBC)."""
    data = page_data[skip_salt:]
    encrypted = data[:len(data) - RESERVE]
    iv = data[len(data) - RESERVE:len(data) - RESERVE + IV_SIZE]
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    dec = cipher.decryptor()
    return dec.update(encrypted) + dec.finalize()


def decrypt_db(input_path, output_path, enc_key_bytes):
    """Decrypt a full SQLCipher DB to a plain SQLite file."""
    with open(input_path, 'rb') as f:
        file_data = f.read()

    sc_data = file_data[EXT_HEADER:]
    if len(sc_data) < PAGE_SIZE:
        return False

    total_pages = len(sc_data) // PAGE_SIZE
    output = bytearray()

    for pgno in range(1, total_pages + 1):
        page_start = (pgno - 1) * PAGE_SIZE
        page_raw = sc_data[page_start:page_start + PAGE_SIZE]
        skip = SALT_SIZE if pgno == 1 else 0
        decrypted = decrypt_page(page_raw, enc_key_bytes, skip_salt=skip)

        if pgno == 1:
            hdr = bytearray(b'SQLite format 3\x00')
            full = bytearray(hdr + decrypted)
            full[16:18] = struct.pack('>H', PAGE_SIZE)
            full[20] = 0  # no reserved space in output
            full = bytes(full[:PAGE_SIZE])
            if len(full) < PAGE_SIZE:
                full += b'\x00' * (PAGE_SIZE - len(full))
            output.extend(full)
        else:
            output.extend(decrypted)
            pad = PAGE_SIZE - len(decrypted)
            if pad > 0:
                output.extend(b'\x00' * pad)

    with open(output_path, 'wb') as f:
        f.write(output)

    # Quick validation
    try:
        conn = sqlite3.connect(output_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        conn.close()
        return len(tables) > 0
    except Exception:
        return False


# ── Memory scanner ───────────────────────────────────────────────────────────

HEX_CHARS = set(b'0123456789abcdefABCDEF')


def scan_by_salt(handle, salt_hexes):
    """Targeted scan: search memory for known salt hex strings, extract keys.

    Pattern in memory: x'<64hex_key><32hex_salt>'
    We search for the 32-char salt hex (ASCII) via bytes.find() (C-fast),
    then back up 66 bytes to verify x' prefix + 64 hex key chars.
    """
    # Pre-compute salt needles as ASCII bytes
    needles = [(sh.encode('ascii'), sh) for sh in salt_hexes]
    results = {}  # salt_hex -> (addr, key_hex)
    total = 0
    t0 = time.time()
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    while True:
        ret = windll.kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), byref(mbi), sizeof(mbi))
        if ret == 0:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and
                (mbi.Protect & 0xFF) in READABLE_PROTS and
                0 < size <= MAX_REGION):
            data = read_mem(handle, base, size)
            if data:
                total += len(data)
                for needle, sh in needles:
                    if sh in results:
                        continue
                    off = 0
                    while True:
                        idx = data.find(needle, off)
                        if idx == -1:
                            break
                        # Verify: x'<64hex_key><32hex_salt>'
                        # salt starts at idx, key starts at idx-64, x' at idx-66
                        if idx >= 66 and idx + 33 <= len(data):
                            if (data[idx - 66:idx - 64] == b"x'" and
                                    data[idx + 32:idx + 33] == b"'" and
                                    all(c in HEX_CHARS for c in data[idx - 64:idx])):
                                key_hex = data[idx - 64:idx].decode('ascii').lower()
                                hit_addr = base + idx - 66
                                results[sh] = (hit_addr, key_hex)
                                break
                        off = idx + 1
        nxt = base + size
        if nxt <= addr or nxt >= 0x7FFFFFFFFFFF:
            break
        addr = nxt
    elapsed = time.time() - t0
    print(f"  Scanned {total / 1048576:.0f} MB in {elapsed:.1f}s -> {len(results)}/{len(salt_hexes)} salts matched")
    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Dump NTQQ database keys from QQ memory")
    ap.add_argument('--qq', required=True, help='QQ number (account directory name)')
    ap.add_argument('--pid', type=int, default=0, help='QQ.exe PID (auto-detect)')
    ap.add_argument('-o', '--output', default=os.path.join(qpaths.data_dir(), 'key_map.json'), help='私密密钥文件，默认写到数据目录')
    ap.add_argument('--out-dir', default='',
                    help='解密输出目录 (默认 output/<qq>); 调用方可指向临时目录以实现原子替换')
    args = ap.parse_args()
    try:
        args.qq = qpaths.account_id(args.qq)
    except ValueError as e:
        ap.error(str(e))
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    # 1. Find QQ main process
    if args.pid:
        main_pid = args.pid
        print(f"Using PID: {main_pid}")
    else:
        main_pid = find_qq_main_pid()
        if not main_pid:
            print("ERROR: No QQ.exe found. Start QQ or use --pid.")
            sys.exit(1)

    # 2. Discover DBs
    db_dirs = find_db_dirs(args.qq)
    global_dbs, pa_salt_map = collect_db_info(db_dirs)
    print(f"\nGlobal DBs: {len(global_dbs)}  (fixed key, no scan)")
    print(f"Per-account: {sum(len(v) for v in pa_salt_map.values())} files, "
          f"{len(pa_salt_map)} unique salts")

    if not pa_salt_map and not global_dbs:
        print("ERROR: No NTQQ databases found.")
        sys.exit(1)

    # 3. Global DBs: fixed PRE_LOGIN_KEY
    global_results = {}
    for account, fp, page1 in global_dbs:
        salt = page1[:SALT_SIZE]
        enc_key = derive_enc_key(PRE_LOGIN_KEY, salt)
        fn = os.path.basename(fp)
        if verify_key_hmac(page1, enc_key):
            global_results[salt.hex()] = enc_key.hex()
            print(f"  [global] {fn}: OK")
        else:
            print(f"  [global] {fn}: HMAC mismatch")

    # 4. Per-account DBs: memory scan
    results = {}
    if pa_salt_map:
        handle = windll.kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, main_pid)
        if not handle:
            print(f"Cannot open PID {main_pid} (err {ctypes.GetLastError()})")
            sys.exit(1)

        # Targeted salt search: find known salt hex strings in memory
        print(f"\n-- Scanning PID {main_pid} for {len(pa_salt_map)} known salts --")
        scan_hits = scan_by_salt(handle, list(pa_salt_map.keys()))

        for salt_hex, (addr, key_hex) in scan_hits.items():
            enc_key = bytes.fromhex(key_hex)
            page1 = pa_salt_map[salt_hex][0][2]
            if verify_key_hmac(page1, enc_key):
                results[salt_hex] = key_hex
                fn = os.path.basename(pa_salt_map[salt_hex][0][1])
                print(f"  [hit] {fn}: key recovered (hidden)")

        windll.kernel32.CloseHandle(handle)

    # 5. Output
    all_results = {**global_results, **results}
    total_salts = len(set(p[2][:SALT_SIZE].hex() for p in global_dbs)) + len(pa_salt_map)

    print(f"\n{'=' * 72}")
    print(f"RESULTS: {len(all_results)}/{total_salts} database keys recovered")
    print(f"{'=' * 72}")

    for sh, kh in sorted(global_results.items()):
        names = ', '.join(os.path.basename(e[1]) for e in global_dbs if e[2][:SALT_SIZE].hex() == sh)
        print(f"  [global] key hidden ({names})")
    for sh, kh in sorted(results.items()):
        names = ', '.join(os.path.basename(e[1]) for e in pa_salt_map.get(sh, []))
        print(f"  [acct] key hidden ({names})")

    unresolved = [sh for sh in pa_salt_map if sh not in results]
    if unresolved:
        print(f"\nUnresolved ({len(unresolved)}):")
        for sh in unresolved:
            names = ', '.join(os.path.basename(e[1]) for e in pa_salt_map[sh])
            print(f"  unresolved ({names})")

    if all_results:
        with open(args.output, 'w') as f:
            json.dump({'key_map': all_results, '_generated': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=2)
        print(f"\nSaved to {args.output}")

    # 6. Decrypt all recovered DBs to output folder
    out_dir = args.out_dir or os.path.join(
        qpaths.data_dir(), 'output', args.qq)
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n-- Decrypting to {out_dir} --")
    ok_count = 0
    fail_count = 0

    # Build unified file list: (source_path, salt_hex, enc_key_bytes)
    decrypt_jobs = []
    for account, fp, page1 in global_dbs:
        sh = page1[:SALT_SIZE].hex()
        if sh in global_results:
            decrypt_jobs.append((fp, bytes.fromhex(global_results[sh])))
    for sh, kh in results.items():
        for account, fp, page1 in pa_salt_map[sh]:
            decrypt_jobs.append((fp, bytes.fromhex(kh)))

    for src_path, enc_key in decrypt_jobs:
        fn = os.path.basename(src_path)
        dst_path = os.path.join(out_dir, fn)
        ok = decrypt_db(src_path, dst_path, enc_key)
        if ok:
            sz = os.path.getsize(dst_path) / 1024
            print(f"  OK  {fn} ({sz:.0f} KB)")
            ok_count += 1
        else:
            print(f"  FAIL {fn}")
            fail_count += 1

    print(f"\nDecrypted: {ok_count} OK, {fail_count} failed -> {out_dir}")
    # 有任何一个库解密失败 → 非 0 退出, 调用方据此丢弃半成品、保留旧快照
    if fail_count or ok_count == 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
