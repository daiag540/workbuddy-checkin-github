#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[已合并] 自动取令牌逻辑已统一到 refresh_token.py：
自动从桌面客户端日志提取「最新且未过期」的明文 Bearer JWT，校验后写入 GitHub 仓库 Secret。

本文件保留为兼容入口，直接转调 refresh_token.main()。
新用法见 refresh_token.py 与 README.md。

示例：
  python export_secret.py owner/repo
  GH_REPO=owner/repo python export_secret.py --dry-run
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refresh_token  # noqa: E402

if __name__ == "__main__":
    refresh_token.main()
