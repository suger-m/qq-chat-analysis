"""Runtime code is portable; machine settings and private data live outside it."""
import json
import os
from pathlib import Path
import tempfile
import re


def data_dir():
    target = os.path.abspath(os.environ.get('QQ_DATA_DIR') or os.path.join(tempfile.gettempdir(), 'qq_decrypt'))
    package = str(Path(__file__).resolve().parents[2])
    try:
        inside = os.path.normcase(os.path.commonpath([os.path.realpath(target), package])) == os.path.normcase(package)
    except ValueError:
        inside = False
    if inside:
        raise ValueError('QQ_DATA_DIR 必须位于技能包外，避免打包私密数据')
    return target


def account_id(value):
    text = str(value or '')
    if not re.fullmatch(r'[0-9]{5,15}', text):
        raise ValueError('账号编号必须为 5–15 位数字')
    return text


def settings():
    try:
        with open(os.path.join(data_dir(), 'settings.json'), encoding='utf-8-sig') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def source_base():
    configured = os.environ.get('QQ_SOURCE_DIR') or settings().get('source_base')
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    home = Path.home()
    candidates = [home / 'Documents/Tencent Files', home / 'Tencent Files',
                  Path(os.environ.get('APPDATA', str(home))) / 'Tencent/QQ/UserData']
    for p in candidates:
        if p.is_dir() and any(p.glob('*/nt_qq/nt_db')):
            return str(p)
    return str(candidates[0])


def detect_source_account():
    root = Path(source_base())
    candidates = []
    for p in root.glob('*/nt_qq/nt_db'):
        account = p.parents[1].name
        if account.isdigit():
            db = p / 'nt_msg.db'
            try:
                candidates.append((db.stat().st_mtime if db.exists() else p.stat().st_mtime, account))
            except OSError:
                continue
    return max(candidates)[1] if candidates else None


def nt_data_dir(account=None):
    explicit = os.environ.get('QQ_NT_DATA_DIR')
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(source_base(), str(account or detect_source_account() or ''), 'nt_qq', 'nt_data')
