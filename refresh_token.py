#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动刷新 WorkBuddy 令牌并写入 GitHub 仓库 Secret（WB_TOKEN）。

为什么需要它
------------
GitHub Actions 跑在云端，读不到本机（Windows 桌面客户端）的登录态；
而可用令牌以明文 `Bearer eyJ...` 形式留在桌面客户端日志里：
    %USERPROFILE%\\.workbuddy\\logs\\...\\workbuddyMainThread__*.log
本脚本在【本机】运行：扫描日志 -> 取「最新且未过期」的 JWT -> 校验 -> 推送仓库 Secret。

说明（重要约束）
----------------
云端 Actions 无法直接刷新（它读不到本机日志），所以刷新动作必须在本机做。
令牌有效期约 11 个月，因此这本质是「令牌快到期时，在本机跑一条命令续期」的保险，
而不是 7x24 无人值守刷新。本机可用 Windows 任务计划程序（见 README）定期触发。

用法
----
  # 仅扫描并打印最新可用令牌（脱敏），不写文件/不推送
  python refresh_token.py --dry-run

  # 写入本地 .wb_token（gitignored），并推送仓库 Secret WB_TOKEN
  GH_REPO=owner/repo python refresh_token.py
  python refresh_token.py owner/repo

  # 只写本地、不推送（适合先本地 checkin.py 验证）
  python refresh_token.py --local-only

  # 用 GitHub PAT 走 REST API 而非 gh CLI
  GH_TOKEN=ghp_xxx GH_REPO=owner/repo python refresh_token.py

依赖：仅标准库；推送 Secret 优选 gh CLI（已登录），
      否则需要 cryptography 库做公钥加密（GH_TOKEN 含 repo + secrets 写权限）。
"""

import os
import sys
import re
import json
import base64
import argparse
import subprocess
import datetime
from urllib import request as ureq, error as uerr, parse as uparse

DEFAULT_DOMAIN = "www.codebuddy.cn"
STATUS_EP = "https://%s/v2/billing/meter/checkin-activity-status" % DEFAULT_DOMAIN
# 匹配日志里的 Bearer eyJxxx.yyy.zzz
JWT_RE = re.compile(r'Bearer\s+(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')


def log(m):
    sys.stderr.write(m + "\n")


def mask(s):
    if not isinstance(s, str) or len(s) <= 12:
        return "<hidden>"
    return s[:6] + "..." + s[-4:]


def b64d(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def log_dirs():
    dirs = []
    base = os.path.expanduser("~/.workbuddy/logs")
    if os.path.isdir(base):
        dirs.append(base)
    la = os.environ.get("LOCALAPPDATA", "")
    for cand in [os.path.join(la, "CodeBuddyExtension"),
                 os.path.join(la, "WorkBuddy")]:
        if os.path.isdir(cand):
            dirs.append(cand)
    return dirs


def scan_tokens(dirs):
    """返回 [(exp, file, token)] 列表；exp=0 表示无法解析 payload。"""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cands = []
    for d in dirs:
        for dp, _, files in os.walk(d):
            if 'node_modules' in dp or 'CachedData' in dp:
                continue
            for fn in files:
                if not fn.lower().endswith('.log'):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    if os.path.getsize(fp) > 30_000_000:
                        continue
                    with open(fp, encoding='utf-8', errors='replace') as f:
                        for line in f:
                            m = JWT_RE.search(line)
                            if m:
                                tok = m.group(1)
                                exp = 0
                                try:
                                    p = json.loads(b64d(tok.split('.')[1]))
                                    exp = p.get('exp') or 0
                                except Exception:
                                    pass
                                cands.append((exp, fp, tok))
                except Exception:
                    pass
    return cands, now


def pick_best(cands, now):
    valid = [(e, f, t) for e, f, t in cands if e > now]
    if not valid:
        return None
    # 取 exp 最大（活得最久）的一条，减少刷新频率
    return max(valid, key=lambda x: x[0])


def validate(token):
    req = ureq.Request(STATUS_EP, method="POST")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with ureq.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return -1, {"error": str(e)}


def push_secret(repo, name, value):
    # 优先 gh CLI
    try:
        subprocess.run(["gh", "auth", "status"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(["gh", "secret", "set", name, "--repo", repo, "--body", value],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return "gh:" + (r.stdout.strip() or "ok")
        log("gh 写入失败: %s" % r.stderr.strip())
    except Exception:
        pass
    # 回退 GitHub REST API（需公钥加密）
    pat = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not pat:
        return "FAIL: 需 gh 登录 或 设置 GH_TOKEN(PAT, 含 repo+secrets 写权限) 与 GH_REPO"
    try:
        import secrets as _s
        req = ureq.Request("https://api.github.com/repos/%s/actions/secrets/public-key" % repo)
        req.add_header("Authorization", "Bearer %s" % pat)
        req.add_header("Accept", "application/vnd.github+json")
        with ureq.urlopen(req, timeout=15) as r:
            pk = json.loads(r.read().decode("utf-8", "replace"))
        key = base64.b64decode(pk["key"])
        try:
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives import hashes
            enc = padding.asymmetric_encrypt(
                key, value.encode(),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                             algorithm=hashes.SHA256(), label=None))
        except Exception:
            return "FAIL: REST 加密需 cryptography 库，请 `pip install cryptography` 后重试"
        enc_b64 = base64.b64encode(enc).decode()
        payload = json.dumps({"encrypted_value": enc_b64, "key_id": pk["key_id"]}).encode()
        req2 = ureq.Request(
            "https://api.github.com/repos/%s/actions/secrets/%s" % (repo, name),
            data=payload, method="PUT")
        req2.add_header("Authorization", "Bearer %s" % pat)
        req2.add_header("Content-Type", "application/json")
        req2.add_header("Accept", "application/vnd.github+json")
        with ureq.urlopen(req2, timeout=15) as r2:
            return "api:%s" % r2.status
    except Exception as e:
        return "FAIL: %s" % e


def main():
    ap = argparse.ArgumentParser(description="自动刷新 WorkBuddy 令牌并写入 GitHub Secret")
    ap.add_argument("repo", nargs="?", default=os.environ.get("GH_REPO", ""),
                    help="目标仓库 owner/repo")
    ap.add_argument("--dry-run", action="store_true", help="只扫描并打印，不写文件/不推送")
    ap.add_argument("--secret-name", default="WB_TOKEN", help="推送的 Secret 名，默认 WB_TOKEN")
    ap.add_argument("--no-local", action="store_true", help="不写本地 .wb_token")
    ap.add_argument("--local-only", action="store_true", help="只写本地 .wb_token，不推送")
    args = ap.parse_args()

    if not args.repo and not args.dry_run and not args.local_only:
        log("用法: GH_REPO=owner/repo python refresh_token.py [--dry-run] [--secret-name WB_TOKEN]")
        log("      (需 gh 已登录，或设置 GH_TOKEN=PAT 与 GH_REPO)")
        sys.exit(2)

    log("扫描桌面客户端日志中的明文 Bearer 令牌 ...")
    dirs = log_dirs()
    log("搜索目录: %s" % (", ".join(dirs) or "(无)"))

    cands, now = scan_tokens(dirs)
    log("扫描到 Bearer JWT 总数: %d" % len(cands))
    log("其中未过期 (exp>now): %d" % sum(1 for e, _, _ in cands if e > now))

    best = pick_best(cands, now)
    if not best:
        log("❌ 未找到有效令牌。请确认桌面客户端已登录且在运行（日志会刷新令牌）。")
        sys.exit(1)

    exp, fp, tok = best
    expdt = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    log("最新有效令牌: %s | exp=%s | 来源=%s" % (mask(tok), expdt, os.path.basename(fp)))

    code, body = validate(tok)
    if 200 <= int(code) < 300:
        log("令牌校验 ✅ HTTP=%s" % code)
    else:
        log("⚠️ 令牌校验失败 HTTP=%s: %s" % (code, str(body)[:160]))

    if args.dry_run:
        log("（dry-run 结束，未做任何写入/推送）")
        print(mask(tok))
        sys.exit(0)

    if not args.no_local:
        here = os.path.dirname(os.path.abspath(__file__))
        lp = os.path.join(here, ".wb_token")
        with open(lp, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        log("已写入本地 .wb_token（gitignored）: %s" % mask(tok))

    if not args.local_only:
        if not args.repo:
            log("未指定仓库，跳过推送。可加 owner/repo 或设置 GH_REPO。")
        else:
            res = push_secret(args.repo, args.secret_name, tok)
            log("推送 Secret %s -> %s" % (args.secret_name, res))

    log("完成。")


if __name__ == "__main__":
    main()
