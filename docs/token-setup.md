# Facebook 權杖取得指南

> 對應規格：[sdd.md](../sdd.md) §7.1
> 執行者身分需求：**花蓮慈濟醫院粉專管理員（Facebook 完整控制權）**
> 預估耗時：15–25 分鐘（一次性作業）

## 0. 你最後會得到什麼

| 項目 | 用途 | 最終存放位置 |
| --- | --- | --- |
| **Page ID** | 指定要擷取的粉專 | `.env` → `FB_PAGE_ID` |
| **Page Access Token** | 讀取貼文與留言 | `.env` → `FB_PAGE_ACCESS_TOKEN` |

App ID、App Secret、短期 User Token、長期 User Token 皆為**中繼產物**，取得 Page Token 後即不需保存於專案內
（App Secret 請自行妥善保管於密碼管理工具，日後重新換發權杖時會再用到）。

> ⚠️ **安全提醒**
> - `.env` 已列入 `.gitignore`，切勿將任何權杖或 App Secret 提交進版控。
> - 切勿將權杖貼進聊天視窗、Issue、截圖或對外文件。
> - App Secret 外洩等同他人可代表你的 App 換發權杖，請比照密碼等級保護。

---

## 1. 前置檢查：確認粉專角色

至粉專「設定 → 管理和查看權限」確認你被授予的項目。

### 新版粉專體驗：畫面中文項目 ↔ Graph API 任務對照

| 畫面上的中文 | Graph API task | 本專案是否需要 |
| --- | --- | --- |
| 內容 | `CREATE_CONTENT` | 否 |
| 訊息和來電 | `MESSAGING` | 否 |
| **社群動態** | **`MODERATE`** | **是（關鍵）** |
| 廣告 | `ADVERTISE` | 否 |
| 洞察報告 | `ANALYZE` | 否 |
| 權限 | `MANAGE` | 否（僅交接時需要） |
| 刪除粉絲專頁 | — | 否 |

**最低需求：具備「社群動態」（`MODERATE`）即可**，不必擁有完整控制權（即同時具備「權限」與「刪除粉絲專頁」）。
本專案 v1 不執行隱藏／刪除／回覆，因此不需要 `MANAGE`。

僅有「洞察報告」「廣告」等被動角色**無法**取得 `pages_read_user_content`，會導致留言的 `from` 欄位取不到、
或端點回傳權限錯誤（code 10 / 200）。

> ⚠️ Meta 官方文件並未公開完整的「任務 ↔ 權限」對照表，上表為依實務歸納。
> **唯一權威判準是 §7.3 的實測**：先跑一遍，`from` 拿得到就代表權限足夠，不必事先申請更高權限。

### 缺少 `MANAGE`（「權限」）的影響

| 面向 | 影響 |
| --- | --- |
| 讀取貼文與留言 | 預期無影響 |
| v1 全部功能 | 無影響 |
| 日後交接 | **有影響** — 無法自行將同仁加入粉專角色，需請具完整控制權者協助 |

---

## 2. 建立 Meta App

1. 前往 <https://developers.facebook.com/> → 右上「我的應用程式」→「**建立應用程式**」
2. 使用案例／類型選擇「**企業（Business）**」
3. 填寫應用程式名稱（建議：`TzuChi-Hualien-SocialMediaControl`）與聯絡信箱
4. 建立完成後，至「**設定 → 基本資料**」記下：
   - **應用程式編號（App ID）**
   - **應用程式密鑰（App Secret）**（需點「顯示」並重新輸入密碼）

不需要新增任何產品（Facebook 登入、Webhooks 等），本專案僅透過 Graph API Explorer 與 REST 呼叫即可完成。

---

## 3. 產生短期 User Token

1. 前往 <https://developers.facebook.com/tools/explorer/>
2. 右上「Meta 應用程式」選擇步驟 2 建立的 App
3. 「使用者或粉絲專頁」選擇「**使用者權杖（User Token）**」
4. 在「權限」區塊加入以下三項：

```
pages_show_list
pages_read_engagement
pages_read_user_content
```

5. 點「**產生存取權杖**」，於彈出視窗完成授權（需選取花蓮慈濟醫院粉專）

此權杖有效期約 1–2 小時，僅用於下一步交換，不需保存。

> 本專案 v1 **不需要** `pages_manage_engagement`（不執行隱藏／刪除／回覆），請勿多要權限。

---

## 4. 交換長期 User Token（有效期約 60 天）

```bash
curl -sG "https://graph.facebook.com/v23.0/oauth/access_token" --data-urlencode "grant_type=fb_exchange_token" --data-urlencode "client_id=<APP_ID>" --data-urlencode "client_secret=<APP_SECRET>" --data-urlencode "fb_exchange_token=<步驟3的短期權杖>"
```

預期回應：

```json
{ "access_token": "EAA...", "token_type": "bearer", "expires_in": 5183944 }
```

`expires_in` 約 5,184,000 秒（60 天）即為正確。

---

## 5. 取得 Page Access Token

```bash
curl -sG "https://graph.facebook.com/v23.0/me/accounts" --data-urlencode "access_token=<步驟4的長期User Token>"
```

預期回應：

```json
{
  "data": [
    {
      "access_token": "EAA...",
      "category": "醫院",
      "name": "花蓮慈濟醫院",
      "id": "1234567890"
    }
  ]
}
```

- `id` → 即 **Page ID**
- `access_token` → 即 **Page Access Token**

> **重點**：由「長期 User Token」衍生的 Page Token **不會過期**。
> 但下列情況會使其失效：使用者變更密碼、移除 App 授權、粉專角色被調整、App 權限被 Meta 撤銷。

---

## 6. 寫入 `.env`

```bash
cp .env.example .env
```

編輯 `.env`（請手動填入，勿使用會留下 shell 歷史紀錄的方式）：

```
FB_PAGE_ID=<步驟5的id>
FB_PAGE_ACCESS_TOKEN=<步驟5的access_token>
FB_API_VERSION=v23.0
```

---

## 7. 驗證（三道關卡，全部通過才算完成）

### 7.1 權杖體檢

```bash
curl -sG "https://graph.facebook.com/v23.0/debug_token" --data-urlencode "input_token=<PAGE_TOKEN>" --data-urlencode "access_token=<APP_ID>|<APP_SECRET>"
```

檢查回應中的 `data` 物件：

| 欄位 | 期望值 | 不符時的意義 |
| --- | --- | --- |
| `type` | `PAGE` | 拿錯權杖，仍是 User Token |
| `is_valid` | `true` | 權杖無效或已撤銷 |
| `expires_at` | `0` | 非 0 表示來源是短期 User Token，請重做步驟 4 |
| `scopes` | 含 §3 三項權限 | 授權時漏勾，請重做步驟 3 |
| `profile_id` | 等於你的 Page ID | 選錯粉專 |

### 7.2 讀取貼文

```bash
curl -sG "https://graph.facebook.com/v23.0/<PAGE_ID>/published_posts" --data-urlencode "fields=id,message,created_time,permalink_url,comments.summary(true).limit(0)" --data-urlencode "limit=3" --data-urlencode "access_token=<PAGE_TOKEN>"
```

應回傳最近 3 則貼文，且每則含 `comments.summary.total_count`。

### 7.3 讀取留言（**最關鍵的一關**）

取上一步任一 `id`：

```bash
curl -sG "https://graph.facebook.com/v23.0/<POST_ID>/comments" --data-urlencode "fields=id,message,created_time,from{id,name},like_count,comment_count,parent{id},permalink_url" --data-urlencode "filter=stream" --data-urlencode "order=chronological" --data-urlencode "limit=5" --data-urlencode "access_token=<PAGE_TOKEN>"
```

**判讀標準：**

- ✅ `message` 有值 **且** `from` 含 `{id, name}` → `pages_read_user_content` 生效，可以開始開發
- ⚠️ `message` 有值但 `from` 全為 null → 權限未生效，回頭檢查步驟 1（粉專角色）與步驟 3（權限勾選）
- ❌ 回傳錯誤 code 10 / 200 → 權限不足
- ❌ 回傳錯誤 code 190 → 權杖失效，重做步驟 3–5

> 註：即使權限正常，**個別留言**的 `from` 仍可能因留言者隱私設定或留言者本身為粉專而缺漏，屬正常現象（見 sdd.md §7.5）。
> 判斷依據是「多數留言有 `from`」，而非「每一則都有」。

---

## 8. 是否需要 App Review？

**本專案 v1 不需要。**

| 情境 | 存取層級 | 是否需審核 |
| --- | --- | --- |
| 由粉專管理員（同時是 App 管理員）本人執行 | 標準存取 Standard Access | 否 |
| 交由非 App 角色的同仁執行、或部署於共用伺服器以他人身分執行 | 進階存取 Advanced Access | **是**（需企業驗證 + 權限審核） |

若日後要交接給其他同仁執行，最省事的做法是把對方加為 App 的「開發者」或「測試人員」角色，即可沿用標準存取，不必送審。

---

## 9. 長期維運

| 事項 | 週期 | 說明 |
| --- | --- | --- |
| **資料使用檢查（Data Use Checkup）** | 每年 | Meta 會寄信要求確認權限用途，**逾期未完成將撤銷權限、權杖失效**。收信後請於期限內至 App 後台完成。見 sdd.md §16 R8 |
| Graph API 版本淘汰 | 約每 2 年 | 各版本約有 2 年支援期，屆期須調整 `.env` 的 `FB_API_VERSION` 並回歸測試 |
| 權杖健康檢查 | 每次執行 | `smc token check` 會自動驗證有效性與 scopes 完整性 |
| 密碼變更後 | 事件觸發 | 變更 Facebook 密碼會使 Page Token 失效，需重做步驟 3–5 |

---

## 10. 疑難排解

| 症狀 | 可能原因 | 處置 |
| --- | --- | --- |
| `(#100) Tried accessing nonexisting field` | API 版本不支援該欄位 | 確認 `FB_API_VERSION`，或移除該欄位 |
| code 190 subcode 463 | 權杖過期 | 重做步驟 3–5 |
| code 190 subcode 467 | 使用者已變更密碼或撤銷授權 | 重做步驟 3–5 |
| code 10 / 200 | 權限不足 | 檢查粉專角色與 scopes |
| code 4 / 17 / 32 / 613 | 觸及速率限制 | 降低 `FB_MAX_QPS`，等待後重試 |
| `/me/accounts` 回傳空陣列 | 授權時未勾選該粉專，或無管理權限 | 重做步驟 3，授權視窗中確實選取粉專 |
| `published_posts` 回傳空但粉專有貼文 | `since`/`until` 區間設定錯誤 | 先不帶區間參數測試 |
