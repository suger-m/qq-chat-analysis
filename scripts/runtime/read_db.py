"""
Read decrypted NTQQ SQLite databases from output/<QQ号>/ and display their contents.

Usage:
    python read_db.py --qq <账号ID>
    python read_db.py --qq <账号ID> --db nt_msg.db
    python read_db.py --qq <账号ID> --db nt_msg.db --table c2c_msg_table --limit 20
"""

import argparse
import sqlite3
import os
import sys
from pathlib import Path
import qpaths


def list_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return list of (name, type) for all tables/views."""
    cur = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table','view') ORDER BY name"
    )
    return cur.fetchall()


# FTS shadow table suffixes — these depend on the custom tokenizer
FTS_SHADOW_SUFFIXES = ('_fts', '_fts_config', '_fts_data', '_fts_docsize', '_fts_idx')


def is_fts_shadow(name: str, all_names: set[str]) -> bool:
    """Check if `name` is likely an FTS5 virtual / shadow table."""
    for sfx in FTS_SHADOW_SUFFIXES:
        if name.endswith(sfx):
            return True
    return False


def table_row_count(conn: sqlite3.Connection, table: str) -> int | str:
    try:
        cur = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
        return cur.fetchone()[0]
    except sqlite3.DatabaseError:
        return "?"


def show_table(conn: sqlite3.Connection, table: str, limit: int = 10):
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" LIMIT {limit}')
    except sqlite3.DatabaseError as e:
        print(f"\n  ┌─ Table: {table}  [SKIP: {e}]")
        print(f"  └─")
        return

    columns = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    count = table_row_count(conn, table)

    print(f"\n  ┌─ Table: {table}  ({count} rows total, showing {len(rows)})")
    print(f"  │  Columns: {', '.join(columns)}")
    print(f"  │")

    for i, row in enumerate(rows):
        cells = []
        for val in row:
            if isinstance(val, bytes):
                if len(val) > 64:
                    cells.append(f"<BLOB {len(val)} bytes>")
                else:
                    cells.append(val.hex())
            elif isinstance(val, str) and len(val) > 120:
                cells.append(val[:120] + "...")
            else:
                cells.append(repr(val))
        print(f"  │  [{i}] {', '.join(cells)}")

    print(f"  └─")


def inspect_db(db_path: Path, table_filter: str | None = None, limit: int = 10):
    print(f"\n{'='*72}")
    print(f"  Database: {db_path.name}  ({db_path.stat().st_size / 1024:.1f} KB)")
    print(f"{'='*72}")

    try:
        conn = sqlite3.connect(str(db_path))
        all_tables = list_tables(conn)

        if not all_tables:
            print("  (no tables)")
            conn.close()
            return

        all_names = {name for name, _ in all_tables}
        regular = [(n, t) for n, t in all_tables if not is_fts_shadow(n, all_names)]
        skipped = [(n, t) for n, t in all_tables if is_fts_shadow(n, all_names)]

        print(f"  Tables ({len(all_tables)}): {len(regular)} regular, "
              f"{len(skipped)} FTS (skipped)")
        print(f"  Regular: {', '.join(n for n, _ in regular)}")
        if skipped:
            print(f"  FTS (skip): {', '.join(n for n, _ in skipped)}")

        if table_filter:
            if table_filter in all_names:
                show_table(conn, table_filter, limit)
            else:
                print(f"  [!] Table '{table_filter}' not found")
        else:
            for name, _ in regular:
                show_table(conn, name, limit)

        conn.close()
    except sqlite3.DatabaseError as e:
        print(f"  [ERROR] {e}")


def main():
    parser = argparse.ArgumentParser(description="Read decrypted NTQQ databases")
    parser.add_argument("--qq", required=True, help="QQ number (matches output/<qq>/ folder)")
    parser.add_argument("--db", default=None, help="Specific .db file to read (e.g. nt_msg.db)")
    parser.add_argument("--table", default=None, help="Specific table to display")
    parser.add_argument("--limit", type=int, default=10, help="Max rows per table (default: 10)")
    args = parser.parse_args()
    try:
        args.qq = qpaths.account_id(args.qq)
    except ValueError as e:
        parser.error(str(e))

    output_dir = Path(qpaths.data_dir()) / "output" / str(args.qq)
    if not output_dir.exists():
        print(f"[ERROR] Directory not found: {output_dir}")
        sys.exit(1)

    db_files = sorted(output_dir.glob("*.db"))
    if not db_files:
        print(f"[ERROR] No .db files found in {output_dir}")
        sys.exit(1)

    if args.db:
        target = output_dir / args.db
        if not target.exists():
            print(f"[ERROR] {args.db} not found in {output_dir}")
            print(f"Available: {', '.join(f.name for f in db_files)}")
            sys.exit(1)
        db_files = [target]

    print(f"QQ: {args.qq}")
    print(f"Directory: {output_dir}")
    print(f"Databases: {len(db_files)}")

    for db_path in db_files:
        inspect_db(db_path, args.table, args.limit)

    print(f"\nDone. Inspected {len(db_files)} database(s).")


if __name__ == "__main__":
    main()
