#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy「Buddy 加油站」每日签到 + 派猫猫旅行 —— GitHub Actions 云端运行版

设计要点（对照需求）：
  1. 一次运行同时完成「签到」与「派猫猫旅行」闭环。
  2. 派猫猫：先领取已到家的旅行积分（arrived -> claim），再判断能否派新一趟；
     仅当 state 为空闲(idle) 且 未达每日上限(daily_limit_reached=False) 才 depart，
     今天已派过（daily_limit_reached=True）或正在旅行(traveling) 一律不派。
  3. 结果推送飞书，签到与猫猫状态分两节写清楚。
  4. 猫猫部分失败/异常 绝不 影响签到结论：签到成功 -> 整体成功(exit 0)。

凭证来源（安全约束）：
  - 令牌只从环境变量读取（由仓库 Secrets 注入），绝不读本地登录态、绝不落盘、绝不打印。
  - 所有输出对令牌做脱敏（仅显示前 6 + 后 4 位）。
接口地址以 WorkBuddy 客户端实际请求为准（见STATUS/CHECKIN/TRAVEL_* 常量）。
依赖：仅 Python 标准库（urllib / json / os / sys / random / datetime）。
"""

import json
import os
import sys
import random
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# ------------------------- 真实接口地址（取自客户端实际请求） -------------------------
CHECKIN_DOMAIN = os.environ.get("WB_DOMAIN") or "www.codebuddy.cn"
CHECKIN_BASE = "https://%s/v2" % CHECKIN_DOMAIN
STATUS_PATH = "/billing/meter/checkin-activity-status"      # POST 查询今日签到状态
CHECKIN_PATH = "/billing/meter/daily-checkin"               # POST 领取签到

TRAVEL_BASE = "https://www.workbuddy.cn"                   # 旅行接口独立域名，无 /v2 前缀
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"   # GET 旅行状态
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"   # GET 可选地点
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"     # POST 领取已到家积分
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"   # POST 派出 Buddy

HTTP_TIMEOUT = 20
MAX_RETRY = 2
TRAVEL_STATE_TEXT = {
    "arrived": "已到达（可领积分）",
    "traveling": "旅行中",
    "idle": "空闲（可派遣）",
    None: "未知",
    "": "空闲（可派遣）",
}


# ------------------------- 工具 -------------------------
def mask(s):
    """令牌脱敏：仅显示前 6 + 后 4 位。非字符串原样返回。"""
    if not isinstance(s, str):
        return s
    if len(s) <= 12:
        return "<hidden>"
    return s[:6] + "..." + s[-4:]


def log(msg):
    sys.stderr.write(msg + "\n")


def _now_beijing():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def api_call(base, path, token, payload=None, method="POST"):
    """发起请求。返回 (http_code, body)。body 为解析后的 dict，失败/非 JSON 时为 {"raw": text}。
    绝不把 token 写入任何返回/日志。"""
    url = base + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Actions/1.0")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            code = resp.status
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
    except Exception as e:
        return (-1, {"raw": "network_error: %s" % e})
    try:
        body = json.loads(text)
    except Exception:
        body = {"raw": text[:500]}
    return (code, body)


def _extract_balance(body):
    """尽力从响应中抽取积分余额；找不到返回 None。"""
    if not isinstance(body, dict):
        return None
    for key in ("balance", "credit_balance", "total_credit", "points"):
        if isinstance(body.get(key), (int, float)):
            return body[key]
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("balance", "credit_balance", "total_credit", "points"):
            if isinstance(data.get(key), (int, float)):
                return data[key]
    return None


# ------------------------- 签到 -------------------------
def do_checkin(token):
    """查询 -> 必要时领取。返回结构化结果。"""
    result = {"status": "unknown", "action": None, "points": None,
              "balance": None, "msg": "", "detail": {}}
    attempt = 0
    while attempt <= MAX_RETRY:
        attempt += 1
        try:
            st_code, st_body = api_call(CHECKIN_BASE, STATUS_PATH, token)
            result["detail"]["status_http"] = st_code
            result["detail"]["status_resp"] = _safe_mask_body(st_body)
            result["balance"] = _extract_balance(st_body)
            result["detail"]["balance"] = result["balance"]

            if not (200 <= int(st_code) < 300):
                raise RuntimeError("状态查询失败：HTTP %s" % st_code)

            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif isinstance(st_body.get("data"), dict) and st_body["data"].get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True

            if today_signed:
                bal = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                result.update(status="ok", action="skip_already_signed",
                              msg="今日已签到，无需重复领取" + bal)
                result["detail"]["today_signed"] = True
                return result

            ck_code, ck_body = api_call(CHECKIN_BASE, CHECKIN_PATH, token)
            result["detail"]["checkin_http"] = ck_code
            result["detail"]["checkin_resp"] = _safe_mask_body(ck_body)

            if isinstance(ck_body, dict):
                code = str(ck_body.get("code", ""))
                msg = ck_body.get("msg") or ck_body.get("message") or ""
                data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
                if code == "10001" or "已签到" in msg or "今天已签到" in msg:
                    result.update(status="ok", action="skip_already_signed",
                                  msg="今日已签到（接口返回 code=10001）")
                    return result
                success_code = code in ("", "0", "200")
                if 200 <= ck_code < 300 and success_code:
                    credit = (ck_body.get("credit") or data.get("credit")
                              or data.get("daily_credit") or data.get("today_credit"))
                    streak = ck_body.get("streak_days") or data.get("streak_days")
                    bbal = _extract_balance(ck_body)
                    if bbal is not None:
                        result["balance"] = bbal
                        result["detail"]["balance"] = bbal
                    bal = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                    result.update(status="ok", action="checked_in", points=credit,
                                  msg="领取成功" + (("，+%s 积分" % credit) if credit else "")
                                       + (("，连续第 %s 天" % streak) if streak else "") + bal)
                    result["detail"]["streak_days"] = streak
                    return result
                result.update(status="error", action="failed",
                              msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
                return result
            else:
                result.update(status="error", action="failed",
                              msg="领取接口返回非 JSON")
                return result
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last_err = "网络错误: %s" % e.reason
        except Exception as e:
            last_err = "异常: %s" % e
        if attempt <= MAX_RETRY:
            import time
            time.sleep(2)
    result.update(status="error", msg="重试 %d 次后仍失败: %s" % (MAX_RETRY, last_err))
    return result


# ------------------------- 派猫猫旅行（隔离，失败不影响签到） -------------------------
def _travel_get(token, path):
    try:
        st, body = api_call(TRAVEL_BASE, path, token, method="GET")
        if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
            return None
        return body.get("data")
    except Exception:
        return None


def _fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours > 0:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes > 0:
        return "%d 分钟" % minutes
    return "%d 秒" % seconds


def _safe_mask_body(body):
    """对响应做脱敏：过滤掉任何含 token/secret 的字段（保险起见），其余原样保留。"""
    if not isinstance(body, dict):
        return body
    blocked = ("token", "accesstoken", "refreshtoken", "secret", "password", "authorization")
    out = {}
    for k, v in body.items():
        if any(b in str(k).lower() for b in blocked):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def do_travel(token, location_id=None):
    """派猫猫旅行闭环（隔离执行）。任何异常都捕获，返回结构化结果，绝不向外抛。"""
    log("--- 派猫猫旅行开始 ---")
    result = {"available": False, "success": False, "skipped": False,
              "log": [], "detail": {}}
    try:
        status = _travel_get(token, TRAVEL_STATUS_PATH)
        if status is None:
            result["log"].append("查询旅行状态失败（接口异常），本次跳过全部写操作")
            return result
        result["available"] = True
        result["detail"]["status"] = _safe_mask_body(status)

        state = status.get("state")
        result["state"] = state
        result["state_text"] = TRAVEL_STATE_TEXT.get(state, state)
        result["daily_limit_reached"] = status.get("daily_limit_reached")
        loc = status.get("location")
        if isinstance(loc, dict):
            result["location_name"] = loc.get("name")
        if state == "traveling":
            arrive, now = status.get("arrive_at"), status.get("server_now")
            if isinstance(arrive, int) and isinstance(now, int):
                rem = max(0, arrive - now)
                result["remaining_seconds"] = rem
                result["remaining_text"] = _fmt_duration(rem)
        result["log"].append("当前状态：%s" % result["state_text"])

        # 1) 先领已到家的积分
        if state == "arrived":
            try:
                cst, cbody = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
                result["detail"]["claim_resp"] = _safe_mask_body(cbody)
                if cst == 200 and isinstance(cbody, dict) and cbody.get("code") == 0:
                    credit = (cbody.get("data") or {}).get("reward_credit")
                    result["log"].append("已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?"))
                    result["claimed_credit"] = credit
                    # 领取后刷新状态
                    fresh = _travel_get(token, TRAVEL_STATUS_PATH)
                    if fresh is not None:
                        status = fresh
                        result["detail"]["status"] = _safe_mask_body(status)
                        state = status.get("state")
                        result["state"] = state
                        result["state_text"] = TRAVEL_STATE_TEXT.get(state, state)
                        result["daily_limit_reached"] = status.get("daily_limit_reached")
                else:
                    m = (cbody.get("msg") if isinstance(cbody, dict) else None) or ("HTTP %s" % cst)
                    result["log"].append("领取旅行积分未成功：%s" % m)
            except Exception as e:
                result["log"].append("领取旅行积分异常：%s" % e)

        # 2) 再判断能否派新一趟：空闲(idle) 且 未达每日上限
        if status.get("daily_limit_reached"):
            result["log"].append("今日派遣次数已用完，不再派遣")
            result["skipped"] = True
        elif state in (None, "", "idle"):
            chosen = location_id
            if chosen is None:
                config = _travel_get(token, TRAVEL_CONFIG_PATH) or {}
                locs = [l for l in (config.get("locations") or []) if isinstance(l, dict) and l.get("id") is not None]
                if locs:
                    chosen = random.choice(locs).get("id")
            if chosen is None:
                result["log"].append("未能确定派遣地点，跳过派遣")
                result["skipped"] = True
            else:
                try:
                    dst, dbody = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token,
                                          payload={"location_id": chosen})
                    result["detail"]["depart_resp"] = _safe_mask_body(dbody)
                    if dst == 200 and isinstance(dbody, dict) and dbody.get("code") == 0:
                        dloc = (dbody.get("data") or {}).get("location") or {}
                        result["success"] = True
                        result["log"].append("已派出 Buddy 前往【%s】" % (dloc.get("name") or ("地点 %s" % chosen)))
                    else:
                        m = (dbody.get("msg") if isinstance(dbody, dict) else None) or ("HTTP %s" % dst)
                        result["log"].append("派遣未成功（可能今日已派过）：%s" % m)
                        result["skipped"] = True
                except Exception as e:
                    result["log"].append("派遣异常：%s" % e)
                    result["skipped"] = True
        else:
            # traveling 等其他状态：不派遣
            result["log"].append("当前状态非空闲，不派遣")
            result["skipped"] = True

        log("派猫猫结果：" + " | ".join(result["log"]))
        return result
    except Exception as e:
        result["log"].append("派猫猫整体异常（已隔离，不影响签到）：%s" % e)
        return result


# ------------------------- 飞书推送 -------------------------
def push_feishu(webhook, checkin, travel):
    if not webhook:
        return {"pushed": False, "reason": "未配置 FEISHU_WEBHOOK"}
    lines = []
    lines.append("📅 WorkBuddy 每日签到 · %s" % _now_beijing())
    lines.append("")
    # 第一节：签到
    c = checkin
    lines.append("【签到】%s" % ("✅ 成功" if c.get("status") == "ok" else "❌ 失败"))
    lines.append("  · 动作：%s" % (c.get("action") or "-"))
    lines.append("  · 说明：%s" % (c.get("msg") or "-"))
    if c.get("balance") is not None:
        lines.append("  · 积分余额：%s" % c["balance"])
    if c.get("points") is not None:
        lines.append("  · 本次获得：%s" % c["points"])
    lines.append("")
    # 第二节：派猫猫（独立呈现）
    t = travel
    if t is None:
        lines.append("【派猫猫旅行】⚪ 未执行")
    else:
        if not t.get("available"):
            lines.append("【派猫猫旅行】⚠️ 查询失败（不影响签到）")
        else:
            lines.append("【派猫猫旅行】%s" % ("✅ 已处理" if t.get("success") or t.get("claimed_credit") is not None or t.get("skipped") else "ℹ️ 无操作"))
            lines.append("  · 状态：%s" % (t.get("state_text") or "-"))
            if t.get("location_name"):
                lines.append("  · 地点：%s" % t["location_name"])
            if t.get("remaining_text"):
                lines.append("  · 到达倒计时：%s" % t["remaining_text"])
            if t.get("claimed_credit") is not None:
                lines.append("  · 领取积分：+%s" % t["claimed_credit"])
            lines.append("  · 明细：" + ("；".join(t.get("log", [])) or "-"))
    text = "\n".join(lines)
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            code = resp.status
            rb = resp.read().decode("utf-8", "replace")
        return {"pushed": True, "http": code, "resp": rb[:200]}
    except Exception as e:
        return {"pushed": False, "reason": str(e)}


# ------------------------- 主流程 -------------------------
def _load_token():
    """令牌来源（安全优先级）：
    1) 环境变量 WB_TOKEN（GitHub Actions 由 Secrets 注入，首选，绝不落盘）。
    2) 本地文件 .wb_token（仅本地测试用，已被 .gitignore 忽略，绝不入库）。
    """
    tok = os.environ.get("WB_TOKEN")
    if tok:
        return tok.strip(), "env:WB_TOKEN"
    # 兜底：仓库内的 .wb_token（gitignored，纯本地）
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, ".wb_token")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok, "file:.wb_token"
        except Exception:
            pass
    return None, None


def main():
    token, src = _load_token()
    if not token:
        print(json.dumps({"status": "error",
                          "msg": "缺少令牌：请设置环境变量 WB_TOKEN，或在仓库内放置 .wb_token 文件（一行明文 token，.gitignore 已忽略）"},
                         ensure_ascii=False, indent=2))
        sys.exit(2)
    print("令牌来源：%s" % src)

    feishu_webhook = os.environ.get("FEISHU_WEBHOOK")
    location_id = os.environ.get("TRAVEL_LOCATION") or None
    if location_id is not None:
        try:
            location_id = int(location_id)
        except Exception:
            location_id = None

    print("== 开始执行（%s）==" % _now_beijing())
    print("令牌：%s" % mask(token))

    # 1) 签到
    checkin = do_checkin(token)
    # 2) 派猫猫（隔离）
    travel = do_travel(token, location_id=location_id)

    # 3) 飞书推送
    push = push_feishu(feishu_webhook, checkin, travel)

    # 4) 汇总输出（令牌已脱敏）
    summary = {
        "run_at": _now_beijing(),
        "checkin": checkin,
        "travel": travel,
        "feishu": push,
        "token_masked": mask(token),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 退出码：签到成功即成功；猫猫失败不影响
    if checkin.get("status") == "ok":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
