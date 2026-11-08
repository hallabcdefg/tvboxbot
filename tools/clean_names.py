#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_names.py — 清洗 TVBox 线路配置里 name 字段的符号（老安卓兼容）

背景：FongMi / 老版本安卓对 4 字节 emoji、制表符 ┃、变体选择符等
非 ASCII 字符解析会崩溃（Map.entrySet() NPE）。本脚本把配置里所有
name 字段的符号清掉，只保留中文 / 字母 / 数字。

特点：
  * 全程二进制处理：读写用 'rb'/'wb'，正则跑在 bytes 上
    -> 换行符(CRLF/LF)、BOM、文件编码、缩进、字段顺序一律零改动
  * 只改 name 的值；key / api / jar / ext / url 等其它字段一字节不动
  * 幂等：重复执行结果完全一致
  * 容错：非标准 JSON / 编码异常 / 转义异常的文件或字段跳过，不中断
  * 只用 Python 标准库，GitHub Actions ubuntu-latest 直接可用，无需 pip

用法：
  python3 clean_names.py                 # 扫描当前目录（递归，所有 .json）
  python3 clean_names.py .              # 同上
  python3 clean_names.py a.json b/ c.json   # 指定文件 / 目录

清洗口径 MODE（改下面这一行即可）：
  'strict' —— 字面全删：只保留 CJK 汉字、ASCII 字母、ASCII 数字
              （与 mytb 仓库 allinone/meow/drpy 的清洗口径一致）
  'light'  —— 温和清洗：只删 emoji / ┃ / 变体选择符 / 首尾空格 / 连续空格，
              保留 - 编号、[标签]、(官)、_ 等 ASCII 符号
"""

import json
import os
import re
import sys

# ===================== 可配置：清洗口径 =====================
MODE = 'strict'   # 'strict' | 'light'
# =========================================================

# bytes 正则：匹配 "name": "值"，允许冒号两侧空白；值内部允许 JSON 转义
NAME_RE = re.compile(rb'("name"\s*:\s*")((?:[^"\\]|\\.)*)(")')

# light 口径用的 emoji / 特殊符号区间（沿用昨天 tools 脚本的口径）
_EMOJI_RE = re.compile(
    '['
    '\U0001F300-\U0001F9FF'
    '\U0001FA00-\U0001FAFF'
    '\U0001F000-\U0001F02F'
    '\U0001F0A0-\U0001F0FF'
    '\U0001F100-\U0001F1FF'
    '\U0001F200-\U0001F2FF'
    '\U00002600-\U000027BF'
    '\U00002B00-\U00002BFF'
    '\U00002300-\U000023FF'
    '\U0000FE00-\U0000FE0F'
    '\U000020E3'
    '\U0000203C'
    '\U00002049'
    '\U00002122'
    '\U000000A9'
    '\U000000AE'
    '\U00002460-\U000024FF'
    '\U00002500-\U0000257F'
    '\U000025A0-\U000025FF'
    ']+',
    flags=re.UNICODE,
)
_EXTRA_CHARS = str.maketrans('', '', '\ufe0f\ufe0e\u200d\u2503')

# tools/ 是生成脚本的工作目录，其中 dianshi.json / jsm.json 是 xiao.py
# 的输入模板；该脚本按 name 去重(dedupe_by_name)，清洗模板会与"未清洗的
# 远程数据"失配导致重复插入站点，故排除。此处只清洗对外线路与生成产物。
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'tools'}
SKIP_FILES = {'package.json', 'package-lock.json', 'manifest.json',
              'tsconfig.json', 'composer.json'}


def _keep_strict(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) \
        or (ch.isascii() and ch.isalnum())


def _clean_strict(s: str) -> str:
    return ''.join(ch for ch in s if _keep_strict(ch))


def _clean_light(s: str) -> str:
    s = s.translate(_EXTRA_CHARS)
    s = _EMOJI_RE.sub('', s)
    s = s.strip()
    s = re.sub(r'  +', ' ', s)
    return s


def clean_text(s: str) -> str:
    return _clean_strict(s) if MODE == 'strict' else _clean_light(s)


def clean_literal_bytes(lit: bytes):
    """把 JSON 字符串字面量(bytes)解码 -> 清洗 -> 返回可安全写回的 bytes。
    任何异常返回 None（调用方跳过，保持原字节，绝不破坏文件）。"""
    try:
        real = json.loads('"' + lit.decode('utf-8') + '"')
    except Exception:
        return None
    if not isinstance(real, str):
        return None
    cleaned = clean_text(real)
    if any(c in cleaned for c in ('"', '\\')):
        return None
    return cleaned.encode('utf-8')


def iter_json_files(targets):
    seen = set()
    for t in targets:
        if os.path.isfile(t):
            if t.lower().endswith('.json') and t not in seen:
                seen.add(t)
                yield t
        elif os.path.isdir(t):
            for dp, dn, fn in os.walk(t):
                dn[:] = [d for d in dn if d not in SKIP_DIRS]
                for f in fn:
                    if not f.lower().endswith('.json') or f in SKIP_FILES:
                        continue
                    p = os.path.join(dp, f)
                    if p not in seen:
                        seen.add(p)
                        yield p


def process_file(path):
    """返回 (变更的 name 数, 是否写回)。任何异常都被吞掉并返回 (0, False)。"""
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
    except Exception:
        return 0, False

    changed = [0]

    def repl(m):
        new_lit = clean_literal_bytes(m.group(2))
        if new_lit is None or new_lit == m.group(2):
            return m.group(0)
        changed[0] += 1
        return m.group(1) + new_lit + m.group(3)

    try:
        out = NAME_RE.sub(repl, raw)
    except Exception:
        return 0, False

    if changed[0] == 0 or out == raw:
        return 0, False

    try:
        with open(path, 'wb') as fh:
            fh.write(out)
    except Exception:
        return 0, False
    return changed[0], True


def main():
    targets = sys.argv[1:] or ['.']
    files = list(iter_json_files(targets))
    total_names = 0
    total_files = 0
    for p in files:
        n, ok = process_file(p)
        if ok:
            total_files += 1
            total_names += n
    print('[clean_names] MODE=%s | 扫描 %d 个 .json | 修改 %d 个文件 / %d 个 name 字段'
          % (MODE, len(files), total_files, total_names))
    # 永不因单个文件失败而让工作流整体报错
    return 0


if __name__ == '__main__':
    sys.exit(main())
