# -*- coding: utf-8 -*-
"""refetch.py — 重新从 QQ 源库解密到 output/ (复用 key_map.json 里已抓到的密钥)

为什么要用它:
  QQ 的 nt_msg.db 处于 WAL 模式, 解密脚本只解主库、不合并 -wal。
  若 QQ 常驻运行, 主库里可能残留旧页/空正文。让 QQ **完全退出**(退出时
  SQLite 会把 WAL checkpoint 回主库) 后再跑一次本脚本, 往往能补回缺失内容。

用法:
  python refetch.py              # 只检查(解密到临时文件, 报告差异, 不动 output/)
  python refetch.py --apply      # 覆盖 output/<账号>/ 下的 nt_msg.db 与 group_msg_fts.db
  python refetch.py --apply --only nt_msg.db

依赖: 已由 dump_qq_key_auto.py 生成的 key_map.json
"""
import os
import sys
import json
import struct
import shutil
import sqlite3
import time
import argparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.stdout.reconfigure(encoding="utf-8")

# 数据目录与 qqlib 保持一致: 默认 %TEMP%/qq_decrypt, 可用 QQ_DATA_DIR 覆盖。
# 代码目录不再放任何数据 (4GB 快照 / 密钥 / 缓存), 这样代码可以整体搬进 skill。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qqlib          # noqa: E402
DATA_DIR = qqlib.DATA_DIR
NT_DB_BASE = qqlib.SOURCE_BASE
DEFAULT_ACCOUNT = None

EXT_HEADER = 1024
PAGE_SIZE = 4096
SALT_SIZE = 16
RESERVE = 48
IV_SIZE = 16
KDF_ITER = 4000


def decrypt_page(page_data, enc_key, skip_salt=0):
    data = page_data[skip_salt:]
    enc = data[:len(data) - RESERVE]
    iv = data[len(data) - RESERVE:len(data) - RESERVE + IV_SIZE]
    d = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    return d.update(enc) + d.finalize()


def salt_of(path):
    with open(path, 'rb') as f:
        f.seek(EXT_HEADER)
        return f.read(SALT_SIZE)


def decrypt_db_stream(src, dst, enc_key, quiet=False):
    """流式逐页解密 (不把整个文件读进内存)"""
    total_pages = (os.path.getsize(src) - EXT_HEADER) // PAGE_SIZE
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fi.seek(EXT_HEADER)
        for pgno in range(1, total_pages + 1):
            raw = fi.read(PAGE_SIZE)
            if len(raw) < PAGE_SIZE:
                break
            skip = SALT_SIZE if pgno == 1 else 0
            try:
                dec = decrypt_page(raw, enc_key, skip_salt=skip)
            except Exception:
                dec = b'\x00' * (PAGE_SIZE - skip)
            if pgno == 1:
                full = bytearray(b'SQLite format 3\x00' + dec)
                full[16:18] = struct.pack('>H', PAGE_SIZE)
                full[20] = 0
                fo.write(bytes(full[:PAGE_SIZE]).ljust(PAGE_SIZE, b'\x00'))
            else:
                fo.write(dec.ljust(PAGE_SIZE, b'\x00'))
    return total_pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--apply", action="store_true",
                    help="覆盖 output/ 下的解密库 (会先备份)")
    ap.add_argument("--only", default="nt_msg.db,group_msg_fts.db",
                    help="要处理的库, 逗号分隔")
    a = ap.parse_args()
    a.account = a.account or qqlib.detect_account() or qqlib.qpaths.detect_source_account()
    if not a.account:
        ap.error("未找到账号；请指定 --account 或配置 QQ_SOURCE_DIR")
    try:
        a.account = qqlib.qpaths.account_id(a.account)
    except ValueError as e:
        ap.error(str(e))

    km_path = os.path.join(DATA_DIR, "key_map.json")
    if not os.path.isfile(km_path):
        print(f"缺 {km_path}, 请先运行 dump_qq_key_auto.py --qq {a.account}",
              "(需 QQ 在线才能从进程内存取密钥)")
        return 1
    km = json.load(open(km_path, encoding="utf-8"))["key_map"]

    src_dir = os.path.join(NT_DB_BASE, a.account, "nt_qq", "nt_db")
    out_dir = os.path.join(qqlib.OUTPUT_DIR, a.account)
    targets = [x.strip() for x in a.only.split(",") if x.strip()]
    if any(os.path.basename(x) != x or '/' in x or '\\' in x or not x.endswith('.db') for x in targets):
        ap.error('--only 仅接受数据库文件名，不接受路径')

    # QQ 是否在跑 (WAL 未 checkpoint 会丢内容)
    import subprocess
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe"],
                           capture_output=True, text=True, timeout=20)
        if "QQ.exe" in (r.stdout or ""):
            print("⚠ 检测到 QQ.exe 仍在运行: 退出时才会把 WAL 合并回主库,")
            print("  建议先完全退出 QQ 再跑本脚本, 否则可能仍拿到旧数据。\n")
    except Exception:
        pass

    ok = 0
    for fn in targets:
        src = os.path.join(src_dir, fn)
        if not os.path.isfile(src):
            print(f"跳过 {fn}: 源库不存在")
            continue
        salt = salt_of(src).hex()
        key_hex = km.get(salt)
        if not key_hex:
            print(f"跳过 {fn}: key_map 无此 salt {salt[:12]}..")
            continue
        print(f"处理 {fn}  ({os.path.getsize(src)/2**30:.2f} GB) …")
        tmp_enc = os.path.join(DATA_DIR, f"_refetch_{fn}.enc")
        tmp_db = os.path.join(DATA_DIR, f"_refetch_{fn}")
        t0 = time.time()
        shutil.copyfile(src, tmp_enc)
        cp = time.time() - t0
        t0 = time.time()
        n = decrypt_db_stream(tmp_enc, tmp_db, bytes.fromhex(key_hex))
        print(f"  复制 {cp:.1f}s + 解密 {time.time()-t0:.1f}s  页数={n}")

        dst = os.path.join(out_dir, fn)
        if a.apply:
            if os.path.isfile(dst):
                bak = dst + ".bak"
                if not os.path.isfile(bak):
                    shutil.copyfile(dst, bak)
                    print(f"  已备份 → {os.path.basename(bak)}")
            shutil.move(tmp_db, dst)
            for ext in ("-wal", "-shm"):
                p = dst + ext
                if os.path.isfile(p):
                    os.remove(p)
            # 清掉依赖源库签名的缓存
            cache = os.path.join(qqlib.CACHE_DIR, "cache.db")
            if os.path.isfile(cache):
                try:
                    c = sqlite3.connect(cache)
                    c.execute("DELETE FROM kv")
                    c.commit()
                    c.close()
                    print("  已清空 Web 缓存 (下次打开会重建)")
                except Exception:
                    pass
            print(f"  ✔ 已更新 {dst}")
            for p in (tmp_enc, tmp_db):
                if os.path.isfile(p):
                    os.remove(p)
        else:
            print(f"  (检查模式) 新库在 {tmp_db}")
        ok += 1
    print(f"\n完成 {ok} 个库。{'已写入 output/' if a.apply else '仅检查, 未改动 output/'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
