#!/usr/bin/env python3
"""將 Graph API Explorer 的短期 User Token 換成永久 Page Token，並寫回 .env。

自動完成 docs/token-setup.md 的步驟 4（交換長期權杖）與步驟 5（取得 Page Token）。

輸入短期 User Token 的兩種方式（擇一）：
  A) 在 .env 暫時加一行  FB_USER_TOKEN=<短期權杖>   （腳本用完會自動移除該行）
  B) 直接執行，依提示以隱藏方式貼上（不會留下 shell 歷史）

用法:  python3 scripts/exchange_token.py
"""
from __future__ import annotations

import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OK, WARN, BAD = "✅", "⚠️ ", "❌"
NEEDED = {"pages_show_list", "pages_read_engagement", "pages_read_user_content"}


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        sys.exit(f"{BAD} 找不到 .env")
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def write_env(updates: dict[str, str], drop: set[str] = frozenset()) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out, seen = [], set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in drop:
                continue
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def get(version: str, path: str, params: dict[str, str]) -> dict:
    url = f"https://graph.facebook.com/{version}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "smc-exchange/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        err = body.get("error", {})
        sys.exit(
            f"{BAD} Graph API 錯誤 code={err.get('code')} subcode={err.get('error_subcode')}\n"
            f"   {err.get('message')}"
        )


def main() -> int:
    env = read_env()
    ver = env.get("FB_API_VERSION") or "v23.0"
    app_id, app_secret = env.get("FB_APP_ID", ""), env.get("FB_APP_SECRET", "")
    if not app_id or not app_secret:
        sys.exit(f"{BAD} .env 需要 FB_APP_ID 與 FB_APP_SECRET")

    user_token = env.get("FB_USER_TOKEN", "")
    from_env = bool(user_token)
    if not user_token:
        print("請貼上 Graph API Explorer 產生的短期 User Token（輸入不會顯示）：")
        user_token = getpass.getpass("Token: ").strip()
    if not user_token:
        sys.exit(f"{BAD} 未提供 User Token")

    # 步驟 4：交換長期 User Token
    print("\n[1/4] 交換長期 User Token …")
    long_lived = get(ver, "oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": user_token,
    })["access_token"]
    print(f"  {OK} 完成（長度 {len(long_lived)}）")

    # 驗證 scopes
    print("\n[2/4] 檢查權限 scopes …")
    d = get(ver, "debug_token", {
        "input_token": long_lived,
        "access_token": f"{app_id}|{app_secret}",
    })["data"]
    scopes = set(d.get("scopes", []))
    missing = NEEDED - scopes
    print(f"  已取得: {sorted(scopes)}")
    if missing:
        print(f"  {BAD} 缺少必要權限: {sorted(missing)}")
        print("     → 回到 Graph API Explorer，在「權限」加入缺少項目後，")
        print("       務必再按一次「產生存取權杖」並完成授權，然後重跑本腳本。")
        return 1
    print(f"  {OK} 三項必要權限齊備")

    # 步驟 5：取得 Page Token
    print("\n[3/4] 取得 Page Access Token …")
    pages = get(ver, "me/accounts", {
        "fields": "id,name,access_token,tasks",
        "limit": "100",
        "access_token": long_lived,
    }).get("data", [])
    if not pages:
        print(f"  {BAD} /me/accounts 回傳空陣列 —— 授權時未選取任何粉專。")
        return 1

    for i, pg in enumerate(pages):
        tasks = pg.get("tasks", [])
        flag = OK if "MODERATE" in tasks or "MANAGE" in tasks else WARN
        print(f"  [{i}] {flag} {pg['name']}  (id={pg['id']})")
        print(f"        tasks: {tasks}")

    want = env.get("FB_PAGE_ID", "")
    chosen = next((p for p in pages if str(p["id"]) == str(want)), None)
    if chosen is None:
        if len(pages) == 1:
            chosen = pages[0]
        else:
            idx = input(f"\n請輸入要使用的粉專編號 [0-{len(pages)-1}]: ").strip()
            chosen = pages[int(idx)]
    print(f"\n  選定: {chosen['name']} (id={chosen['id']})")

    tasks = chosen.get("tasks", [])
    if "MODERATE" not in tasks and "MANAGE" not in tasks:
        print(f"  {WARN} 此粉專未授予 MODERATE/MANAGE 任務，讀取留言的 from 欄位可能失敗。")

    # 步驟 6：寫回 .env
    print("\n[4/4] 寫回 .env …")
    write_env(
        {"FB_PAGE_ID": str(chosen["id"]), "FB_PAGE_ACCESS_TOKEN": chosen["access_token"]},
        drop={"FB_USER_TOKEN"} if from_env else set(),
    )
    print(f"  {OK} FB_PAGE_ID 與 FB_PAGE_ACCESS_TOKEN 已更新")
    if from_env:
        print(f"  {OK} 已自動移除 .env 中的暫存 FB_USER_TOKEN")

    print(f"\n{OK} 完成。接著執行驗證：")
    print("    python3 scripts/verify_fb_access.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
