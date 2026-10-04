"""Mask identity metadata in reports and diagnostic text, preserving local data."""
import re

_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|/(?:home|Users)/)[^\s<>\"\'，。；,;]+')
_IDS = re.compile(r'(?<![\w])\d{5,}(?![\w])')
_PHONE = re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)')
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_SECRET = re.compile(r'(?i)(\b(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*)[^\s,;，；]+')
_API_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}')


def redact_text(value, identifiers=()):
    text = str(value if value is not None else '')
    text = _PATH.sub('[本地路径已隐藏]', text)
    text = _EMAIL.sub('[邮箱已隐藏]', text)
    text = _PHONE.sub('[手机号已隐藏]', text)
    text = _SECRET.sub(r'\1[密钥已隐藏]', text)
    text = _API_KEY.sub('[密钥已隐藏]', text)
    ids = sorted({str(x) for x in identifiers if str(x).isdigit()}, key=len, reverse=True)
    if ids:
        text = re.sub(r'(?<!\d)(?:' + '|'.join(map(re.escape, ids)) + r')(?!\d)', '[身份编号已隐藏]', text)
    return text


def diagnostic(value):
    return _IDS.sub('[身份编号已隐藏]', redact_text(value))
