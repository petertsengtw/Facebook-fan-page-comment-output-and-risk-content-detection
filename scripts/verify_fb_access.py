#!/usr/bin/env python3
"""驗證 Facebook Page Token 是否足以執行本專案（sdd.md §7.1 / docs/token-setup.md §7）。

僅使用標準函式庫，不需安裝相依套件。
輸出經過遮罩：不會印出 access token、App Secret，也不會印出留言者姓名或留言內文。

用法:  python3 scripts/verify_fb_access.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OK, WARN, BAD = "✅", "⚠️ ", "❌"


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        sys.exit(f"{BAD} 找不到 {ENV_PATH}。請先 `cp .env.example .env` 並填入 FB_PAGE_ID 與 FB_PAGE_ACCESS_TOKEN。")
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip("'\"")
    return env


def mask(token: str) -> str:
    return f"{token[:6]}...{token[-4:]}（長度 {len(token)}）" if len(token) > 12 else "（過短，疑似未填）"


def get(version: str, path: str, params: dict[str, str]) -> tuple[int, dict]:
    url = f"https://graph.facebook.com/{version}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "smc-verify/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": {"message": str(e)}}
    except OSError as e:  # 網路層錯誤（URLError 亦為其子類）
        return 0, {"error": {"message": f"連線失敗: {e}"}}


def show_error(body: dict) -> str:
    err = body.get("error", {})
    return (
        f"code={err.get('code')} subcode={err.get('error_subcode')} "
        f"type={err.get('type')}\n     訊息: {err.get('message')}"
    )


def main() -> int:
    env = load_env()
    ver = env.get("FB_API_VERSION") or "v23.0"
    page_id = env.get("FB_PAGE_ID", "")
    token = env.get("FB_PAGE_ACCESS_TOKEN", "")
    app_id = env.get("FB_APP_ID", "")
    app_secret = env.get("FB_APP_SECRET", "")

    print("=" * 66)
    print("  Facebook 存取權限驗證")
    print("=" * 66)
    print(f"API 版本   : {ver}")
    print(f"Page ID    : {page_id or '（未填）'}")
    print(f"Page Token : {mask(token)}")
    print()

    if not page_id or not token:
        print(f"{BAD} FB_PAGE_ID 或 FB_PAGE_ACCESS_TOKEN 未填，中止。")
        return 2

    failures = 0

    # ── 檢查 1：debug_token ────────────────────────────────────────────
    print("─" * 66)
    print("【檢查 1／3】權杖體檢 (debug_token)")
    print("─" * 66)
    if app_id and app_secret:
        status, body = get(ver, "debug_token", {
            "input_token": token,
            "access_token": f"{app_id}|{app_secret}",
        })
        if status != 200:
            print(f"{BAD} 呼叫失敗: {show_error(body)}")
            failures += 1
        else:
            d = body.get("data", {})
            checks = [
                ("類型 type", d.get("type"), "PAGE", d.get("type") == "PAGE"),
                ("有效 is_valid", d.get("is_valid"), "True", bool(d.get("is_valid"))),
                ("到期 expires_at", d.get("expires_at"), "0（永不過期）", d.get("expires_at") == 0),
                ("粉專 profile_id", d.get("profile_id"), page_id, str(d.get("profile_id")) == str(page_id)),
            ]
            for label, actual, expect, passed in checks:
                mark = OK if passed else (WARN if label.startswith("到期") else BAD)
                print(f"  {mark} {label:<18} 實際={actual}  期望={expect}")
                if not passed and not label.startswith("到期"):
                    failures += 1
            scopes = d.get("scopes", [])
            need = {"pages_show_list", "pages_read_engagement", "pages_read_user_content"}
            missing = need - set(scopes)
            print(f"  {OK if not missing else BAD} scopes            {sorted(scopes)}")
            if missing:
                print(f"     缺少必要權限: {sorted(missing)}")
                failures += 1
    else:
        print(f"{WARN} 未填 FB_APP_ID / FB_APP_SECRET，跳過此檢查（非必要，檢查 2、3 才是關鍵）。")
    print()

    # ── 檢查 2：published_posts ────────────────────────────────────────
    print("─" * 66)
    print("【檢查 2／3】讀取貼文 (published_posts)")
    print("─" * 66)
    status, body = get(ver, f"{page_id}/published_posts", {
        "fields": "id,created_time,comments.summary(true).limit(0)",
        "limit": "10",
        "access_token": token,
    })
    posts = []
    if status != 200:
        print(f"{BAD} 呼叫失敗: {show_error(body)}")
        failures += 1
    else:
        posts = body.get("data", [])
        print(f"  {OK if posts else WARN} 取得貼文數: {len(posts)}")
        for p in posts[:5]:
            total = p.get("comments", {}).get("summary", {}).get("total_count", "?")
            print(f"     - {p['id']}  {p.get('created_time','')}  留言數={total}")
        if not posts:
            print(f"  {WARN} 未取得貼文，無法進行檢查 3。")
    print()

    # ── 檢查 3：讀取留言（關鍵）────────────────────────────────────────
    print("─" * 66)
    print("【檢查 3／3】讀取留言與 from 欄位  ← 最關鍵")
    print("─" * 66)
    def ccount(p: dict) -> int:
        return p.get("comments", {}).get("summary", {}).get("total_count", 0) or 0

    # 挑留言最多的貼文，樣本數太少會讓 from 覆蓋率失真
    target = max(posts, key=ccount) if posts else None
    if target is not None and ccount(target) == 0:
        target = None
    if not posts:
        print(f"{WARN} 因【檢查 2】未取得貼文而略過。請先修正檢查 2 的錯誤。")
    elif target is None:
        print(f"{WARN} 最近 10 則貼文皆無留言，無法驗證 from 欄位。")
        print("     請於粉專找一則有留言的貼文後重跑，或加大 published_posts 的 limit。")
    else:
        print(f"  測試貼文: {target['id']}")
        status, body = get(ver, f"{target['id']}/comments", {
            "fields": "id,message,created_time,from{id,name},like_count,comment_count,parent{id},permalink_url,attachment{type},is_hidden",
            "filter": "stream",
            "order": "chronological",
            "limit": "100",
            "access_token": token,
        })
        if status != 200:
            print(f"  {BAD} 呼叫失敗: {show_error(body)}")
            failures += 1
        else:
            rows = body.get("data", [])
            with_from = sum(1 for c in rows if c.get("from", {}).get("id"))
            with_msg = sum(1 for c in rows if (c.get("message") or "").strip())
            replies = sum(1 for c in rows if c.get("parent"))
            print(f"  {OK} 取得留言數      : {len(rows)}（其中回覆 {replies} 則）")
            print(f"  {OK} 有文字內容      : {with_msg} / {len(rows)}")
            ratio = (with_from / len(rows) * 100) if rows else 0
            inconclusive = len(rows) < 5
            mark = WARN if inconclusive else (OK if ratio >= 50 else BAD)
            print(f"  {mark} 有 from 欄位    : {with_from} / {len(rows)}  ({ratio:.0f}%)")
            with_link = sum(1 for c in rows if c.get("permalink_url"))
            print(f"  {OK if with_link == len(rows) else BAD} 有 permalink    : {with_link} / {len(rows)}  ← 報表定位用，關鍵")
            print(f"  {OK} 有分頁游標      : {'是' if body.get('paging', {}).get('next') else '否（僅一頁）'}")
            print()
            if inconclusive:
                print(f"  {WARN} 樣本僅 {len(rows)} 則，不足以判定 from 覆蓋率，結論從缺。")
                print("     請於粉專找一則留言較多的貼文後重跑。")
            elif ratio >= 50:
                print(f"  {OK} pages_read_user_content 生效，可讀取留言者資訊。")
                if with_from < len(rows):
                    print(f"     （{len(rows) - with_from} 則缺 from 屬正常：留言者為粉專或隱私設定所致，見 sdd.md §7.5）")
            else:
                print(f"  {WARN} from 覆蓋率低 —— 標準存取下屬**預期行為**，非錯誤。")
                print("     Meta 僅對 App 角色回傳身分資料，一般民眾一律遮蔽。")
                print("     本專案決定不申請進階存取（sdd.md §16 DEC1）：偵測與複核不需身分，")
                print("     小編經 permalink_url 點回 Facebook 即可定位並處置。")
                if not any(c.get("permalink_url") for c in rows):
                    print(f"  {BAD} 但 permalink_url 亦缺漏 —— 這才是真正的問題，小編將無法定位留言。")
                    failures += 1
    print()

    # ── 總結 ──────────────────────────────────────────────────────────
    print("=" * 66)
    if failures == 0:
        print(f"{OK} 全部通過 — 權限足夠，可以開始開發 M0。")
    else:
        print(f"{BAD} 有 {failures} 項未通過 — 請依上方訊息排除後重跑。")
        print("   對照 docs/token-setup.md §10 疑難排解。")
    print("=" * 66)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
