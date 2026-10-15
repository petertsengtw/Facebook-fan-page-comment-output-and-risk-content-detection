# SDD — 花蓮慈濟醫院 FB 粉專留言匯出與風險內容偵測

| 項目 | 內容 |
| --- | --- |
| 文件版本 | v0.1（草案） |
| 建立日期 | 2026-08-18 |
| 專案代號 | SocialMediaControl |
| 目標讀者 | 開發者（本專案由單人逐步依規格開發） |

---

## 1. 目的與範圍

### 1.1 目的
建立一套可重複執行的工具，用於：
1. 透過 Facebook Graph API，匯出**花蓮慈濟醫院官方粉絲專頁**貼文底下的所有留言（含回覆）。
2. 對留言內容進行自動分類與風險標記，偵測 **人身攻擊、嘲諷/酸言、政治議題** 等需人工關注的內容。
3. 產出可供公關/社群小編人工複核的報表。

### 1.2 範圍內（In Scope）
- 粉專自有貼文（`published_posts`）之留言與巢狀回覆的擷取。
- 留言的增量同步（僅抓取新增/變動部分）。
- 本地資料庫保存（SQLite）。
- 規則式前篩 + LLM 分類的兩段式偵測。
- CSV / Excel 報表輸出。

### 1.3 範圍外（Out of Scope，v1 不做）
- **不做**自動隱藏、刪除、封鎖或自動回覆。所有處置一律由人工在 Meta Business Suite 執行（見 §5 法遵）。
- 不擷取貼文的私訊（Messenger）、評論以外的互動（按讚名單、分享名單）。
- 不擷取其他粉專或個人帳號的內容。
- 不做即時（webhook）觸發，v1 為排程/手動批次執行。
- 不做圖片/影片留言內容之影像辨識（僅處理文字與 sticker 標記）。
  > ⚠ **此決定已被實證推翻，待重新評估**（見 §16 R10）。公傳室 2026-08-18 的人工標註發現，
  > 已有留言者將長篇攻擊文字製成圖片張貼以規避文字偵測。資料庫中 967 則 photo 類留言
  > 目前完全未經判讀。
- 不做網頁介面、多人帳號、線上部署。v1 為單人本機工具，Excel 交付（未來規劃見 §17）。

### 1.4 成功條件
- 指定期間內的貼文留言，匯出完整率 ≥ 99%（以 FB 後台留言數對帳）。
- 高風險留言（人身攻擊）之偵測召回率 ≥ 90%（以人工標註 200 則樣本驗證）。
- 全量重跑 1,000 則留言的 LLM 成本可控且可快取（重跑不重複計費）。

---

## 2. 名詞定義

| 名詞 | 說明 |
| --- | --- |
| Page | Facebook 粉絲專頁，本專案指花蓮慈濟醫院官方粉專 |
| Page Access Token | 代表粉專身分的存取權杖，用於讀取留言 |
| Post | 粉專發布的貼文 |
| Comment | 貼文下的留言；`parent_id` 為空者為第一層留言 |
| Reply | 留言的回覆（第二層留言），Graph API 中同為 comment 物件 |
| ASID | App-Scoped ID，留言者在本 App 範圍內的匿名化 ID，跨 App 不同 |
| 標記（Label） | 分類器輸出的風險類別 |
| 分數（Severity） | 該類別的嚴重程度 0–3 |

---

## 3. 需求

### 3.1 功能需求（FR）

| ID | 需求 | 優先 |
| --- | --- | --- |
| FR-01 | 以 Page Access Token 驗證並取得粉專基本資訊 | P0 |
| FR-02 | 依日期區間列出粉專貼文（支援 `--since` / `--until`） | P0 |
| FR-03 | 擷取每則貼文的所有留言與回覆，含分頁處理 | P0 |
| FR-04 | 留言欄位保存：id、貼文 id、父留言 id、內文、建立時間、按讚數、回覆數、**permalink**、附件類型、隱藏狀態。作者欄位保留但實務上為空（見 §7.5） | P0 |
| FR-05 | 增量同步：以 `last_synced_at` 與留言 id 去重，重跑不產生重複資料 | P0 |
| FR-06 | 規則式前篩（關鍵詞/樣式）標記明顯垃圾訊息與敏感詞 | P1 |
| FR-07 | LLM 分類：對每則留言輸出多標籤 + 嚴重度 + 判斷理由 | P0 |
| FR-08 | 分類結果快取：同一留言內容（hash 相同）不重複呼叫 LLM | P0 |
| FR-09 | 匯出報表：全量 CSV + 風險留言 Excel（含篩選與顏色標記） | P0 |
| FR-10 | 人工複核欄位：`review_status`（pending/confirmed/false_positive）可回寫 | P1 |
| FR-11 | 執行摘要：本次新增留言數、各類別命中數、Top 風險貼文、對帳差異 | P1 |
| FR-13 | 報表須區分**已隱藏**（小編已處理）與**未隱藏**（待處理）留言，預設僅列後者；`--include-hidden` 可納入 | P0 |
| FR-14 | 記錄每次擷取實際涵蓋的時間區間；`report` 須檢查所查區間之覆蓋率，發現缺口即警告（見 §6.7、§16 R9） | P0 |
| FR-12 | 權杖到期偵測與明確錯誤訊息（含更換權杖指引） | P0 |

### 3.2 非功能需求（NFR）

| ID | 需求 |
| --- | --- |
| NFR-01 | 全流程可離線重跑分析（留言已入庫後，分類不需再打 FB API） |
| NFR-02 | 遵守 Graph API 速率限制，遇 429/錯誤碼 4/17/32 自動退避重試 |
| NFR-03 | 單次執行需可中斷後續跑（checkpoint 於 DB，非記憶體） |
| NFR-04 | 所有機密（token、API key）僅存於 `.env`，不進版控 |
| NFR-05 | 資料庫與報表預設輸出至 `data/`，該目錄列入 `.gitignore` |
| NFR-06 | 日誌不得輸出完整 token；留言內文於 INFO 層級僅輸出前 30 字 |
| NFR-07 | Python 3.12+，以 `uv` 管理相依 |

---

## 4. 系統架構

### 4.1 元件圖

```
            ┌──────────────────────────────────────────────┐
            │                  CLI (typer)                 │
            │  fetch / classify / report / status / init    │
            └───────┬─────────────┬───────────────┬────────┘
                    │             │               │
        ┌───────────▼───┐  ┌──────▼───────┐  ┌────▼────────┐
        │  fb_client    │  │  classifier  │  │  reporter   │
        │  Graph API    │  │  rules + LLM │  │  CSV/XLSX   │
        └───────┬───────┘  └──────┬───────┘  └────┬────────┘
                │                 │               │
                └────────┬────────┴───────────────┘
                         │
                  ┌──────▼───────┐
                  │   storage    │  SQLite (data/comments.db)
                  │  SQLModel    │
                  └──────────────┘
```

### 4.2 資料流

```
Page Token ──► fb_client.list_posts() ──► posts 表
                      │
                      └► fb_client.list_comments(post_id) ──► comments 表 (raw)
                                                                    │
                    rules.prefilter() ◄─────────────────────────────┘
                            │
                            ├─ 明顯垃圾/廣告 ─► 直接標記，跳過 LLM
                            └─ 其餘 ─► llm.classify_batch() ─► classifications 表
                                                                    │
                                                    reporter ◄──────┘
                                                        │
                                              data/report_YYYYMMDD.xlsx
```

### 4.3 技術選型

| 層 | 選擇 | 理由 |
| --- | --- | --- |
| 語言 | Python 3.12+ | 生態完整、開發速度 |
| 套件管理 | `uv` | 本機已安裝，速度快 |
| CLI | `typer` | 型別即介面 |
| HTTP | `httpx`（含 timeout/retry） | 同步即可，支援 HTTP/2 |
| DB | SQLite + `SQLModel` | 單機、零維運、易備份；落地位置與安全要求見 §4.4 |
| LLM | Anthropic Claude API（`claude-sonnet-5` 為預設，高疑慮案件可升 `claude-opus-5`） | 中文語意判斷佳、支援結構化輸出 |
| 報表 | `openpyxl` / `pandas` | Excel 格式與條件式格式化 |
| 設定 | `pydantic-settings` + `.env` | 型別驗證 |
| 日誌 | `structlog` 或標準 `logging`（JSON formatter） | 可觀測性 |

### 4.4 部署與資料落地（Deployment）

本專案**無伺服器元件**。SQLite 為嵌入式資料庫，即單一檔案，無獨立程序、無網路埠、無帳密。

| 項目 | 內容 |
| --- | --- |
| 執行位置 | 開發者本機（macOS） |
| 資料庫檔 | `<專案根目錄>/data/comments.db`，路徑由 `.env` 的 `DB_PATH` 決定 |
| 報表輸出 | `data/reports/` |
| 日誌 | `data/logs/` |
| 對外連線 | 僅 outbound：Graph API（graph.facebook.com）、Anthropic API |
| 對內監聽 | 無 |
| 預估容量 | 每則留言含 `raw_json` 約 1.5 KB；10 萬則約 150 MB |

#### 本機資料安全要求（開發環境檢核）

`data/comments.db` 內含民眾留言與 ASID，屬個資檔案。執行環境須滿足：

| # | 要求 | 檢查方式 |
| --- | --- | --- |
| D1 | 全碟加密已啟用 | `fdesetup status` → `FileVault is On.` |
| D2 | 專案目錄**不得**位於 iCloud Drive / Dropbox / Google Drive / OneDrive 等自動同步路徑 | `pwd` 檢查路徑；搬移專案時須重新確認 |
| D3 | `data/` 已列入 `.gitignore`，不得進版控 | `git check-ignore -v data/comments.db` |
| D4 | 備份（若有）須存放於加密媒體，且比照 §5.6 保存期限管理 | 人工確認 |
| D5 | 不得將資料庫檔以附件、雲端連結等方式傳送給第三方 | 作業規範 |

> D2 為最容易疏忽者：專案若被搬進雲端同步資料夾，含個資的資料庫將**自動上傳至院外伺服器**，
> 屬未經評估之資料流出。搬移專案目錄後務必重新檢查。

#### 何時需要改變架構

僅在下列情形才需評估改用 PostgreSQL 等主從式資料庫（皆不在 v1 範圍）：
多人同時寫入、需要網頁儀表板即時查詢、資料量超過 10 GB、需院內資安納管與稽核軌跡。
因採用 SQLModel，屆時主要變更為連線字串與少量方言差異。

---

## 5. 法遵、隱私與倫理（開發前必讀）

> 本節為硬性約束，實作時不得以「方便」為由跳過。

1. **合法基礎**：僅擷取本院自有粉專之公開留言，且必須以本院粉專管理者授權之 Page Token 執行。不得擷取他人粉專。
2. **個資最小化**：
   - **平台已代為落實**：標準存取下 Graph API 不回傳一般民眾之 `from`，故資料庫中**不存在**留言者姓名或 ID（見 §7.5）。
   - 即使日後取得進階存取，留言者識別亦僅得使用 **ASID**，不得嘗試反查真實身分、不得與院內病歷/HIS 系統任何資料串接比對。
   - 資料庫中留言者姓名欄位（`author_name`）預設**雜湊化儲存**（SHA-256 + 專案 salt），僅在報表輸出時，若使用者以 `--reveal-name` 明示且有業務需要才顯示明碼。
3. **用途限定**：本工具產出僅供輿情觀測與人工複核，**不得**用於對特定民眾之針對性行為（封鎖清單、名單建檔、對外公布）。

   > **關於法務蒐證**：若院方需就特定留言採取法律行動，本工具的角色是「**快速收斂候選範圍**」
   > （14,315 則 → 數十則），而非產出證據。資料庫中的姓名欄位在法律上不具證明力；
   > 妨害名譽之蒐證應以網頁公證或存證方式為之，並經 `permalink_url` 直達該則留言現場擷取。
   >
   > 若日後確需系統性蒐集留言者身分，屬**蒐集目的變更**，須重新界定用途、
   > 取得個資保護窗口同意，並連動檢討保存期限（訴訟時效 vs 現行 12 個月）。見 §16 DEC1。
4. **不自動處置**：系統一律不執行隱藏/刪除/回覆。誤判成本高（醫療議題的正當批評與人身攻擊界線模糊），必須人工判讀。
5. **政治類別的定位**：`politics` 標記**僅代表「內容涉及政治議題」**，屬中性資訊標記，不代表該留言應被處理。報表中須以文字註記此點，避免誤用。
6. **保存期限**：原始留言預設保存 **12 個月**，提供 `purge --before <date>` 指令清除逾期資料。
7. **醫療風險特例**：若偵測到 `self_harm`（自傷/自殺意念）標記，報表須置頂並提示轉介關懷專線流程，此為病人安全事項而非負面輿情。
8. **平台條款**：遵守 Meta Platform Terms；資料不得轉售、不得提供第三方；不得規避速率限制。

---

## 6. 資料模型

### 6.1 `pages`
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| id | TEXT PK | Page ID |
| name | TEXT | 粉專名稱 |
| last_synced_at | DATETIME | 上次同步完成時間 |

### 6.2 `posts`
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| id | TEXT PK | Post ID（格式 `{page_id}_{post_id}`） |
| page_id | TEXT FK | |
| message | TEXT | 貼文內文（可 NULL，如純圖貼文） |
| created_time | DATETIME | |
| permalink_url | TEXT | |
| comments_count | INTEGER | FB 回報之留言總數（用於對帳） |
| fetched_at | DATETIME | |
| comments_synced_at | DATETIME | 該貼文留言最後同步時間（checkpoint） |

### 6.3 `comments`
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| id | TEXT PK | Comment ID |
| post_id | TEXT FK | |
| parent_id | TEXT NULL | 為 NULL 表第一層留言 |
| author_id | TEXT NULL | ASID；**標準存取下實務上恆為 NULL**（見 §7.5）。欄位保留以備日後取得進階存取 |
| author_name_hash | TEXT NULL | SHA-256(salt + name)；同上，實務上恆為 NULL |
| author_name | TEXT NULL | 明碼，受 `STORE_PLAIN_NAME` 開關控制，預設 false；同上，實務上恆為 NULL |
| message | TEXT | 留言內文，可能為空字串（純貼圖/附件） |
| message_hash | TEXT | SHA-256(normalize(message))，分類快取鍵 |
| created_time | DATETIME | |
| like_count | INTEGER | |
| reply_count | INTEGER | |
| permalink_url | TEXT | |
| attachment_type | TEXT NULL | photo / sticker / video / share |
| is_hidden | BOOLEAN | FB 回報之隱藏狀態。**實測約 15% 留言為隱藏**，仍完整回傳且必須保留（見 §7.5） |
| fetched_at | DATETIME | |
| raw_json | TEXT | 原始回應（保留以利日後回溯，可設定關閉） |

索引：`idx_comments_post`(post_id)、`idx_comments_created`(created_time)、`idx_comments_hash`(message_hash)

### 6.4 `classifications`
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| id | INTEGER PK AUTOINCREMENT | |
| comment_id | TEXT FK UNIQUE | |
| message_hash | TEXT | 快取鍵 |
| labels_json | TEXT | `[{"label":"personal_attack","severity":2}, ...]` |
| max_severity | INTEGER | 各標記中最高嚴重度，用於排序 |
| rationale | TEXT | LLM 一句話理由（繁中，≤50 字） |
| engine | TEXT | `rules` / `llm:claude-sonnet-5` |
| model_version | TEXT | 提示詞版本（如 `p1.0`），供日後重跑比對 |
| classified_at | DATETIME | |

### 6.5 `reviews`
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| comment_id | TEXT PK FK | |
| review_status | TEXT | pending / confirmed / false_positive / escalated |
| reviewer | TEXT | |
| note | TEXT | |
| reviewed_at | DATETIME | |

### 6.7 `sync_runs`（擷取涵蓋範圍，規格 FR-14）

記錄每次 `fetch` 實際涵蓋的區間。**沒有這張表，資料庫就無法區分
「該期間沒有問題留言」與「該期間從未擷取」** —— 見 §16 R9。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| id | INTEGER PK AUTOINCREMENT | |
| kind | TEXT | `posts` / `comments` |
| since | DATETIME NULL | 區間起；NULL 表不設下限（`--all`） |
| until | DATETIME NULL | 區間迄；NULL 表至執行當下 |
| started_at | DATETIME | |
| completed_at | DATETIME NULL | **NULL 表該次執行未完成**，其涵蓋範圍不計入覆蓋率 |
| posts_seen | INTEGER | |
| comments_seen | INTEGER | |
| note | TEXT NULL | 中斷原因等 |

**覆蓋率判定**：所查區間須被 `completed_at IS NOT NULL` 之各筆 `[since, until]` 聯集完全涵蓋，
否則視為有缺口。未完成的執行**不得**計入 —— 否則中斷的跑批會被誤判為已涵蓋，
反而製造出更難察覺的偽陰性。

### 6.6 `api_calls`（稽核用，選配）
記錄每次 Graph API 呼叫的 endpoint、狀態碼、用量標頭，用於除錯與速率調校。

---

## 7. Facebook Graph API 介接規格

> API 版本以設定檔 `FB_API_VERSION` 控制，開發時先以 `v23.0` 起始；實作第一步先呼叫 `GET /{version}/me` 驗證版本可用性，若平台已淘汰該版本則調整。**不要把版本號硬編在多處。**

### 7.1 權杖取得流程（一次性人工作業，寫入 `docs/token-setup.md`）
1. 於 Meta for Developers 建立 App（類型：Business）。
2. 以具備粉專「社群動態」（`MODERATE` 任務）以上權限之帳號，在 Graph API Explorer 產生 User Access Token，勾選權限：（角色需求詳見 `docs/token-setup.md` §1；不需完整控制權）
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_read_user_content`（讀取使用者留言的必要權限）
   - （v1 不需要 `pages_manage_engagement`，因不做隱藏/刪除）
3. 以 `GET /oauth/access_token?grant_type=fb_exchange_token` 換取 **長期 User Token（約 60 天）**。
4. 以長期 User Token 呼叫 `GET /me/accounts`，取得該粉專的 **Page Access Token**（由長期 User Token 衍生者不會過期，但帳號改密碼/移除授權即失效）。
5. 將 Page Token 與 Page ID 寫入 `.env`。
6. **App Review**：若 App 尚未通過審核，僅 App 角色（管理員/開發者/測試者）之權限可用；由於執行者本身即為粉專管理員，開發階段以 Standard Access 即可運作；若日後需交由非管理員帳號執行，需申請 Advanced Access。

實作提供 `smc token check` 指令：呼叫 `GET /debug_token`，輸出到期時間、scopes、是否有效。

### 7.2 取得貼文
```
GET /{version}/{page-id}/published_posts
  ?fields=id,message,created_time,permalink_url,comments.summary(true).limit(0)
  &since={unix}&until={unix}
  &limit=25
  &access_token={page_token}
```
- `comments.summary(true).limit(0)` 取得 `summary.total_count` 作為對帳基準。
- 分頁：跟隨 `paging.cursors.after`，直到無 `paging.next`。
- 注意：`published_posts` 不含他人在粉專塗鴉牆的貼文（本專案不需要）。

### 7.3 取得留言
```
GET /{version}/{post-id}/comments
  ?fields=id,message,created_time,from{id,name},like_count,comment_count,parent{id},permalink_url,attachment{type},is_hidden
  &filter=stream
  &order=chronological
  &limit=100
  &access_token={page_token}
```
- `filter=stream`：**扁平化**回傳所有留言與回覆（含巢狀），可省去逐則抓 replies。`parent.id` 用以還原樹狀結構。
- `order=chronological`：分頁游標穩定，配合增量同步。
- 增量策略（**實作與原規劃不同，以此為準**）：原規劃為逐則貼文以 `reverse_chronological`
  抓到 checkpoint 即停。實作上 `list_comments` 未帶時間參數，**每則貼文一律整串重抓**；
  `comments_synced_at` 僅用於決定「要不要造訪這則貼文」，不限制貼文內的抓取範圍。
  因 Graph API 不計費，此差異的代價是時間而非金錢，故未改回原設計。
- 貼文選取有三種模式，差別在於**哪些貼文會被造訪**：

  | 模式 | 選取範圍 | 本粉專實測 | 用途 |
  | --- | --- | --- | --- |
  | 預設（增量） | `comments_synced_at IS NULL` | 1 則 | 僅新貼文。**舊貼文的新留言抓不到** |
  | `--changed-only` | 上列 ∪「FB 回報留言數 ≠ 本地頂層留言數」 | 176 則 | 例行更新。須先 `fetch posts --all` 刷新計數 |
  | `--full-rescan` | 全部有留言的貼文 | 2,211 則 | 安全網 |

  `--changed-only` 的比對基準為 §7.5 的頂層留言數（含隱藏、不含回覆）。
  已知死角：同一則貼文新增一則又刪除一則時總數不變而被跳過，
  故仍須定期（建議每季）執行 `--full-rescan`。例行流程見 `scripts/monthly-update.sh`。
- 空內文留言（純貼圖）保留，`message` 存空字串，分類階段直接標 `no_text` 跳過 LLM。

### 7.4 速率限制與退避
- 讀取回應標頭 `X-App-Usage`、`X-Business-Use-Case-Usage`；任一 `call_count`/`total_cputime`/`total_time` > 80 時，主動 sleep 60s。
- 錯誤碼處理：

| code | subcode | 意義 | 處置 |
| --- | --- | --- | --- |
| 4 | — | App 層級速率限制 | 指數退避（60s → 300s → 900s），最多 3 次 |
| 17 | — | User 層級速率限制 | 同上 |
| 32 | — | Page 層級速率限制 | 同上 |
| 613 | — | 呼叫過於頻繁 | 同上 |
| 190 | 463/467 | Token 過期/失效 | **立即中止**，輸出換發指引 |
| 100 | 33 | 物件不存在或無權限 | 記錄並跳過該物件，繼續 |
| 10 / 200 | — | **物件層級**權限不足 | **跳過該物件並繼續**，記入摘要；連續 ≥10 則且尚無任何成功時才視為全域問題並中止 |
| 1 / 2 | — | 暫時性錯誤 | 重試 3 次（2s/8s/30s） |

> ⚠️ **code 10 並非必然是全域權限問題**（2026-08-19 實測修正）。
> 粉專自有貼文中偶有個別貼文（推測為轉貼／跨頁內容）連貼文端點本身都回 code 10，
> 但同批其他貼文完全正常。初版規格將其列為致命錯誤，導致**一則異常貼文擋掉 2,203 則的擷取**。
> 現行處置：視為可跳過的物件層級錯誤，並以「連續失敗門檻」偵測真正的全域權限問題。

- 所有請求：connect timeout 10s、read timeout 30s。
- 全域 QPS 上限以設定 `FB_MAX_QPS`（預設 3）節流。

### 7.5 已知限制（實作時需容錯）
- **`from` 欄位在標準存取（Standard Access）下幾乎必然為 null**（2026-08-18 實測：100 則留言中僅 1 則有值，且該則為 App 角色本人所留）。
  Meta 僅對「在 App 中具有角色者（管理員／開發者／測試者）」回傳身分資料，一般民眾一律遮蔽。
  取得一般民眾之 `from` 需申請 `pages_read_user_content` 的**進階存取**（App 審查 + 企業驗證）。
  **本專案決定不申請**，理由見 §16 DEC1。程式**不得**假設 `from` 存在，亦不得因其缺漏而中止。
- **對帳公式（2026-08-19 實測導出，6 則貼文 / 598 則留言）**：

  ```
  summary.total_count  ≈  頂層留言數（含隱藏，不含回覆）
  filter=stream 實抓數  =  頂層留言數 + 回覆數
  ```

  實測 520 頂層 + 78 回覆 = 598 實抓，與公式完全吻合；6 則中 5 則 `total_count` 精準相符。
  唯一差異（−3）出現在當日仍持續增加留言的貼文，屬**兩次 API 呼叫之間的時間差**，非邏輯錯誤。

  **M3 對帳規則**：以 `COUNT(parent_id IS NULL)` 與 `summary.total_count` 比對，
  差異絕對值 ≤ 3 或 ≤ 2% 視為正常時間差，僅記錄；超出則列入執行摘要供人工查核。

- **被管理者隱藏的留言仍會完整回傳**，並以 `is_hidden=true` 標示（實測 89/598，佔 14.9%）。
  程式**不得**過濾掉隱藏留言 —— 它們往往正是本專案要偵測的內容。
- ⚠️ **已知限制**：`is_hidden` 無法區分「管理者主動隱藏」與「FB 自動垃圾過濾」。
  另有部分遭 FB 自動過濾之留言可能完全不回傳，此部分無從偵測，亦無法量化。
- 刪除的留言不會回傳，資料庫中舊資料保留但可標 `is_deleted`（v1 選配）。
- **欄位可用性實測**（2026-08-18，單則貼文 100 則留言樣本）：

| 欄位 | 有值比例 | 備註 |
| --- | --- | --- |
| `id`／`created_time`／`like_count`／`comment_count`／`is_hidden`／`can_hide` | 100/100 | 穩定可用 |
| `permalink_url` | 100/100 | **關鍵欄位**：報表據此讓小編點回 FB 定位並處置 |

> **`permalink_url` 可用性已實測確認（2026-08-19）**：全部 14,315 則留言的連結皆含 `comment_id`，
> 回覆另含 `reply_comment_id`；以粉專管理者身分點擊可直接跳至該則留言。
> 連結有兩種格式：一般貼文 `/{page}/posts/{post}?comment_id=`、
> 短影音 `/reel/{id}/?comment_id=`，兩者皆經實測可用。
>
> **此為 DEC1（不申請進階存取）的成立前提** —— 若日後 Meta 變更連結行為導致無法定位，
> DEC1 須重新評估。
| `message` | 74/100 | 其餘 26 則為純貼圖／圖片留言，`message` 缺漏 |
| `attachment{type}` | 38/100 | 圖片、貼圖 |
| `parent{id}` | 4/100 | 回覆層級可正確還原 |
| `message_tags` | 2/100 | 標註他人 |
| `from{id,name}` | **1/100** | 見上方說明，實務上視為不可用 |

> 約 **26% 留言無文字內容**，此比例直接決定 `no_text` 跳過 LLM 的節費幅度，估算成本時應納入。
> 上表為單則貼文 100 則留言之樣本；粉專層級的實際總量見 §8.5。
> 欄位不得於同一次請求中重複指定，否則回傳 code 2500 語法錯誤。

---

## 8. 分類引擎規格

### 8.1 標籤集（Label Set v1）

| label | 中文 | 定義 | 嚴重度範圍 |
| --- | --- | --- | --- |
| `personal_attack` | 人身攻擊 | 針對特定個人（醫師、護理師、員工、其他留言者）之侮辱、貶低、詛咒、人格否定 | 1–3 |
| `sarcasm` | 嘲諷／酸言 | 以反諷、譏刺語氣表達不滿，未達人身攻擊 | 1–2 |
| `politics` | 政治相關 | 涉及政黨、選舉、政治人物、政策立場之內容（**中性標記**） | 1–2 |
| `complaint` | 醫療抱怨／申訴 | 對就醫流程、等候、費用、醫療結果之具體不滿（需客服跟進） | 1–3 |
| `misinformation` | 疑似不實資訊 | 對醫療事實或本院措施之明顯錯誤陳述 | 1–3 |
| `spam` | 廣告／洗版 | 商品推銷、外連導流、重複洗版 | 1–2 |
| `pii_leak` | 個資揭露 | 留言中出現病歷號、身分證、電話、完整姓名與病情連結 | 2–3 |
| `self_harm` | 自傷風險 | 表達自殺、自傷意念 | 3（固定） |
| `threat` | 威脅恐嚇 | 對人員或機構之暴力威脅、揚言提告以外的恐嚇 | 3 |
| `positive` | 正面回饋 | 感謝、讚美 | 0 |
| `neutral` | 中性 | 詢問、事實陳述 | 0 |

嚴重度定義：`0=無需處理`、`1=留意`、`2=建議人工回應`、`3=需主管即刻處理`。

### 8.2 第一段：規則前篩（`rules.py`）
目的是省成本與提供可解釋基準，**不取代 LLM**。
- `no_text`：`message` 為空 → 直接歸檔，不進 LLM。
- `spam`：命中 URL 樣式 + 電商關鍵詞。
- **重複內容不由規則逕自判定為廣告**（2026-08-19 依實測修訂）：
  同一 `message_hash` 於 24 小時內出現 ≥ 5 次且內容長度 ≥ 8 字時，**僅提升處理優先序並將線索交給 LLM**，
  不自行指派 `spam` 標籤。

  > **修訂原因**：初版規則將重複達 5 次者直接標為 `spam`。實測本粉專重複次數最高的內容為
  > 皆為 2–6 字的日常祈福與感謝用語（重複 10–21 次不等）——
  > 這些是**不同民眾各自表達的日常祈福語**，非洗版。在佛教醫院粉專上，此類誤判會淹沒整個偵測結果
  > （實測 2,000 則樣本中 250 則被誤標）。
  >
  > 根本限制：缺少作者資訊（DEC1）時，無法分辨「一人重複張貼 20 次」與「20 人各發一次」。
  > 此判斷需依內容語境，屬 LLM 職責而非規則職責。長度門檻用於先濾除明顯的日常短語。
- `pii_leak`：正則偵測台灣身分證（`[A-Z][12]\d{8}`）、手機（`09\d{8}`）、病歷號樣式。
- `self_harm`：關鍵詞表命中即**強制送 LLM 覆核**並提升優先序（不可只靠規則否定）。
- 關鍵詞表置於 `config/lexicon/*.txt`，一詞一行，可由業務人員維護，程式啟動時載入。
- 規則命中不終止流程（除 `no_text` 與 `spam` 可設定跳過 LLM），其結果與 LLM 結果合併，取各標籤最高嚴重度。

### 8.3 第二段：LLM 分類（`llm.py`）
- **模型**：預設 `claude-sonnet-5`；`--model opus` 可切 `claude-opus-5` 用於高疑慮批次或標註集校準。
- **批次**：每次請求包含 **20 則**留言（附貼文內文摘要作為上下文，因嘲諷須看語境），以編號對應輸出。
- **上下文**：提供該留言所屬貼文的 `message` 前 200 字；若為回覆，附上父留言內文（判斷是否針對其他留言者的人身攻擊）。
- **輸出**：使用 tool use / structured output 強制 JSON schema：

```json
{
  "results": [
    {
      "index": 1,
      "labels": [{"label": "personal_attack", "severity": 2}],
      "rationale": "以貶低性稱呼指涉特定醫師"
    }
  ]
}
```
- **提示詞要點**（存於 `prompts/classify_v1.txt`，版本號寫入 `model_version`）：
  1. 角色設定：醫院社群小編的內容分級助手，判讀繁體中文（含台語漢字、注音文、火星文、簡體）。
  2. 明確要求：**對本院服務的批評、負評、投訴本身不等於人身攻擊**；只有針對「人」的侮辱貶低才是 `personal_attack`。避免把正當醫療申訴誤標。
  3. 嘲諷須依語境判斷反話（例：「醫術真好呢」配合負面上下文 → sarcasm）。
  4. `politics` 為中性資訊標記，不因政治立場調整嚴重度；不得因留言者支持特定政黨而提高分數。
  5. 可多標籤；找不到適配者輸出 `neutral`。
  6. `rationale` 限繁中 50 字內，僅陳述判斷依據，不得複述辱罵內容。
- **快取**：以 `message_hash + model_version` 為鍵；命中則不呼叫 API。
- **失敗處理**：解析失敗重試 1 次（要求僅輸出 JSON）；再失敗則標記 `classify_error`，留待下次 `classify --retry-failed`。
- **成本控管**：`classify` 支援 `--limit N` 與 `--dry-run`（僅估算 token 與費用，不實際呼叫）。

### 8.5 成本估算（2026-08-19 實測基準）

**定價**（每百萬 token）：

| 模型 | Input | Output |
| --- | --- | --- |
| `claude-sonnet-5`（預設） | $3.00（優惠 $2.00，至 2026-08-31） | $15.00（優惠 $10.00） |
| `claude-opus-5` | $5.00 | $25.00 |
| 快取寫入 | 1.25×（5 分鐘 TTL）／2×（1 小時 TTL） | — |
| 快取讀取 | **0.1×** | — |

**全量跑批估算**（基於 M2 實際入庫資料，2026-08-19）：

| 項目 | 數值 | 來源 |
| --- | --- | --- |
| 貼文總數 | 4,145 則（2015-08-20 起） | 實際入庫 |
| 有留言之貼文 | 2,211 則 | 實際入庫 |
| 留言總數（頂層，含隱藏） | 13,399 則 | FB `summary.total_count` 加總 |
| 含回覆之留言總數 | 約 15,400 則 | 依實測回覆佔比（78/598）外推 |
| 需送 LLM（扣除 26% 純貼圖） | 約 11,400 則 | 依 §7.5 實測比例外推 |
| LLM 呼叫數（每批 20 則） | 約 571 次 | |
| Input tokens | 約 1.9 M | |
| Output tokens | 約 0.7 M | |
| **Sonnet 5 總成本** | **約 US$10.7（優惠價）／ US$16.0（標準價）** | |
| Opus 5 總成本 | 約 US$26.7 | |

> 貼文與留言數為**實際入庫值**；token 與費用為依中文字元推估，
> **須以 `smc classify --dry-run` 呼叫 `count_tokens` 驗證後才可對外引用**。

**Prompt caching 適用性**：分類提示詞（§8.3）內容固定，符合快取條件。
最小可快取前綴：`claude-sonnet-5` 為 1024 tokens、`claude-opus-5` 為 512 tokens；
提示詞須確保長度達標，否則靜默不快取（`cache_creation_input_tokens` 為 0）。
預設 5 分鐘 TTL 對連續跑批已足夠；**提示詞內不得插入時間戳或隨機值**，否則前綴失效、快取全數落空。

**結論**：成本不構成歷史範圍的限制條件（見 §16 DEC2）。

### 8.4 品質驗證
- 建立 `data/goldset.csv`：人工標註 200 則歷史留言（各類別至少 15 則）。
- **種子來源建議**：`is_hidden=true` 的留言（實測約佔 15%）是小編已實際判斷過需處理的內容，
  可作為 `personal_attack` / `spam` 等類別的高品質正樣本起點，大幅縮短標註時間。
  惟仍須人工複核 —— 隱藏原因不一定等同本規格的標籤定義。
- `smc eval --goldset data/goldset.csv` 輸出各標籤 precision / recall / F1 與混淆矩陣。
- 提示詞每次修改須跑 eval 並記錄結果於 `docs/eval-log.md`，避免無據調參。

---

## 9. CLI 規格

指令前綴 `smc`（透過 `uv run smc ...`）。

| 指令 | 說明 | 主要參數 |
| --- | --- | --- |
| `smc init` | 建立 `data/`、初始化 DB schema、產生 `.env.example` | `--force` |
| `smc token check` | 驗證權杖有效性與 scopes | |
| `smc fetch posts` | 抓取貼文清單 | `--since 2026-01-01 --until 2026-08-18` |
| `smc fetch comments` | 抓取留言 | `--post-id` / `--since` / `--full-rescan` / `--max-posts` |
| `smc fetch all` | posts + comments 一次跑完 | 同上 |
| `smc classify` | 對未分類留言執行分類 | `--limit` `--model` `--retry-failed` `--dry-run` `--force`（忽略快取重跑） |
| `smc report` | 產出報表 | `--since` `--until` `--min-severity 1` `--include-hidden` `--output` |
| `smc review set` | 標記人工複核結果 | `--comment-id` `--status` `--note` |
| `smc status` | 顯示 DB 統計、上次同步時間、待分類數 | |
| `smc eval` | 跑黃金標註集評估 | `--goldset` |
| `smc purge` | 刪除逾期資料 | `--before 2025-08-18` `--yes` |

### 9.1 `--since` / `--until` 的語意（易踩雷，務必區分）

| 指令 | 區間作用於 | 理由 |
| --- | --- | --- |
| `smc fetch` | **貼文發布時間** | Graph API 僅支援以貼文時間篩選，無「跨貼文取某時段所有留言」之端點 |
| `smc report` | **留言時間** | 小編需要的是「本週應處理什麼」，與留言掛在哪則舊貼文無關 |

因此抓取階段寧可放寬（涵蓋較舊貼文），報表階段再依留言時間收斂。
舊貼文的新增留言由 `--full-rescan` 定期補齊（見 §16 R7）。

`fetch` 的預設值：未指定 `--since` 時預設近 90 天；`--all` 表示回溯至粉專最早貼文。

**關於分區間擷取**：資料以 ID 為主鍵 upsert，區間重疊不會產生重複資料；
但區間不相連會在資料庫留下缺口，故一律以 `sync_runs`（§6.7）記錄實際涵蓋範圍。
另請注意，分區間**不是**中斷續跑的替代方案 —— 續跑由 `posts.comments_synced_at`
checkpoint 保證（§11.1），與區間選擇彼此獨立。

行為約定：
- 所有寫入型指令支援 `--dry-run`。
- 進度以 `rich` progress bar 呈現，結束輸出摘要表。
- 離開碼：0 成功、1 一般錯誤、2 設定/權杖錯誤、3 部分失敗（有 skip 項目）。

---

## 10. 設定與機密管理

`.env`（不進版控；`.env.example` 進版控）：

```
FB_PAGE_ID=
FB_PAGE_ACCESS_TOKEN=
FB_API_VERSION=v23.0
FB_MAX_QPS=3

ANTHROPIC_API_KEY=
CLASSIFY_MODEL=claude-sonnet-5
CLASSIFY_BATCH_SIZE=20

DB_PATH=data/comments.db
NAME_HASH_SALT=            # 任意隨機字串，變更後既有雜湊將失效
STORE_PLAIN_NAME=false
STORE_RAW_JSON=true
RETENTION_MONTHS=12
LOG_LEVEL=INFO
```

`.gitignore` 必含：`.env`、`data/`、`*.db`、`*.xlsx`、`__pycache__/`、`.venv/`。

---

## 11. 錯誤處理與韌性

1. **Checkpoint**：每則貼文的留言抓完即更新 `posts.comments_synced_at`；程式中斷後重跑自動從未完成的貼文續抓。
2. **交易邊界**：每頁留言（最多 100 筆）一個 transaction，`INSERT OR REPLACE` 以 comment id 去重。
3. **部分失敗不中止**：單則貼文失敗（含 code 10 物件層級權限不足）記入摘要並繼續，離開碼 3。
   無法取得留言的貼文須明確列出 —— 報表不含其內容，使用者有權知道缺了什麼。
4. **致命錯誤即中止**：權杖失效（190）、權限不足（10/200）、設定缺漏。
5. **重試**：僅對暫時性錯誤與速率限制重試；4xx 語意錯誤不重試。

---

## 12. 日誌與可觀測性

- 格式：JSON lines，輸出至 `data/logs/smc-YYYYMMDD.log` 與 stdout。
- 必要欄位：`ts`、`level`、`event`、`post_id`/`comment_id`、`duration_ms`、`api_usage`。
- 遮罩規則：token 僅顯示前 6 後 4 字元；留言內文於 INFO 僅前 30 字，DEBUG 才完整（DEBUG 僅本機使用）。
- 每次執行結束輸出 run summary：新增貼文/留言數、API 呼叫數、LLM 呼叫數與估算成本、各標籤命中數、耗時。

---

## 13. 測試策略

| 層級 | 內容 |
| --- | --- |
| 單元測試 | 分頁游標處理、增量截止判斷、規則前篩正則、雜湊、退避計算、LLM 輸出解析（含格式錯誤案例） |
| 整合測試 | 以 `respx` 模擬 Graph API 回應（含多頁、缺 `from`、錯誤碼 4/190），驗證入庫結果 |
| 契約測試 | 保存數則真實 API 回應樣本於 `tests/fixtures/`（**須先去識別化**）作為回歸基準 |
| 分類品質 | `smc eval` 對 goldset 跑 F1，設門檻：`personal_attack` recall ≥ 0.90 |
| 手動驗收 | 挑一則已知高互動貼文，比對 FB 後台留言數與匯出筆數 |

測試不得使用真實 token；CI（若有）僅跑 mock 測試。

---

## 14. 開發里程碑（逐步開發順序）

> 每個里程碑結束時皆可獨立驗證，勿跳關。

| M | 目標 | 完成定義（DoD） |
| --- | --- | --- |
| **M0** | 專案骨架 | `uv init`、目錄結構、`.env.example`、`smc --help` 可執行 |
| **M1** | 權杖驗證 | `smc token check` 正確顯示粉專名稱、scopes、到期時間 |
| **M2** | 貼文擷取 + DB | `smc fetch posts --since` 能將指定期間貼文寫入 SQLite，重跑不重複 |
| **M3** | 留言擷取 | `smc fetch comments` 完成分頁、巢狀還原、增量同步；依 §7.5 對帳規則驗證。<br>**實測規模**：2,211 則貼文有留言（其中 18 則超過 100 則需分頁，單則最多 695 則），最少約 2,250 次 API 呼叫，以 `FB_MAX_QPS=3` 計約需 12 分鐘 —— 須支援中斷續跑（NFR-03） |
| **M4** | 規則前篩 | `rules` 標記 spam/pii/no_text，`smc status` 顯示統計 |
| **M5** | LLM 分類 | `smc classify` 產出多標籤結果並寫入 `classifications`，快取生效（重跑 0 次 API 呼叫） |
| **M6** | 報表 | `smc report` 產出 Excel：Sheet1 高風險（severity ≥ 2）、Sheet2 全量、Sheet3 摘要統計 |
| **M7** | 品質驗證 | 完成 200 則 goldset 標註，`smc eval` 達標；提示詞定版 v1.0 |
| **M8** | 維運 | 人工複核回寫、`purge`、排程（launchd/cron）範例、`docs/runbook.md` |

---

## 15. 目錄結構

```
22_SocialMediaControl/
├── sdd.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── src/smc/
│   ├── __init__.py
│   ├── cli.py             # typer 入口
│   ├── config.py          # pydantic-settings
│   ├── fb/
│   │   ├── client.py      # Graph API 封裝、分頁、退避
│   │   ├── models.py      # API 回應 schema
│   │   └── errors.py
│   ├── storage/
│   │   ├── db.py
│   │   ├── models.py      # SQLModel 定義
│   │   └── repo.py        # 查詢/寫入
│   ├── classify/
│   │   ├── rules.py
│   │   ├── llm.py
│   │   ├── labels.py      # 標籤列舉與嚴重度
│   │   └── merge.py       # 規則 + LLM 結果合併
│   ├── report/
│   │   ├── excel.py
│   │   └── summary.py
│   └── util/
│       ├── hashing.py
│       ├── logging.py
│       └── ratelimit.py
├── prompts/
│   └── classify_v1.txt
├── config/lexicon/
│   ├── spam.txt
│   ├── self_harm.txt
│   └── political.txt
├── tests/
│   ├── fixtures/
│   └── ...
├── docs/
│   ├── token-setup.md
│   ├── runbook.md
│   └── eval-log.md
└── data/                  # gitignored
    ├── comments.db
    ├── logs/
    └── reports/
```

---

## 16. 風險與未決事項

> 編號規則：`R*` 為風險、`DEC*` 為已定案決策、`Q*` 為待確認事項。
> （§4.4 的 D1–D5 為部署檢核項，與此處編號無關。）

| # | 風險／問題 | 影響 | 對策／待確認 |
| --- | --- | --- | --- |
| R1 | Graph API 版本淘汰（每版約 2 年） | 程式停擺 | 版本集中設定；`token check` 附版本檢查 |
| R2 | `pages_read_user_content` 未通過 App Review | 非管理員無法執行 | v1 由粉專管理員本人執行；若需交接再申請 Advanced Access |
| R3 | 嘲諷/反諷判讀為 LLM 最弱環節 | 誤判率高 | 提供上下文、goldset 校準、severity ≤ 2 一律人工複核 |
| R4 | 政治標記被誤用為言論篩選依據 | 聲譽/倫理風險 | 報表明示中性標記、不自動處置（§5.5） |
| R5 | LLM 成本隨留言量成長 | 預算 | 規則前篩 + hash 快取 + `--dry-run` 估算 |
| R6 | 留言者姓名屬個資 | 法遵 | 預設雜湊、保存期限、purge 指令 |
| R7 | 舊貼文新增回覆不會被增量抓到 | 資料缺漏 | 定期 `--full-rescan`（建議每週一次近 90 天貼文） |
| R10 | **攻擊文字製成圖片張貼，可完全規避文字偵測** | 系統將無文字留言一律歸為嚴重度 0 並跳過 LLM；實測資料庫中有 967 則 photo 類留言完全未經判讀 | 公傳室人工複核已實際發現此手法（2026-08-18 標註表）。v1 規格 §1.3 將影像辨識列為範圍外，此決定須依實證重新評估 —— 這不是可選功能，而是**已知被利用的規避手法** |
| R9 | **區間擷取造成資料缺口，且缺口本身無紀錄** | 報表對未擷取期間回傳空結果，外觀等同「該期間無問題留言」—— **靜默偽陰性**，是本專案最嚴重的失效模式 | 以 `sync_runs`（§6.7）記錄實際涵蓋範圍；`report` 檢查覆蓋率並於缺口時警告（FR-14）。`status` 一併顯示缺口 |
| R8 | Meta 年度「資料使用檢查」(Data Use Checkup) 逾期未完成 | 權限遭撤銷、權杖失效、專案停擺 | 於 `docs/runbook.md` 記錄到期月份並設行事曆提醒；`smc token check` 每次執行皆驗證 scopes 是否仍完整 |
| **DEC1** | **決策（2026-08-18）：不申請 `pages_read_user_content` 進階存取** | 資料庫無留言者身分；無法依作者統計累犯 | 核心目的（偵測＋人工複核）不需身分；`permalink_url` 已足以讓小編定位處置；洗版偵測改用 `message_hash`；且更符合 §5.2 個資最小化。**如日後有明確業務需要，須重新評估並取得個資保護窗口同意** |
| **DEC2** | **決策（2026-08-19）：首次全量匯出，之後增量** | 首次跑批 **4,145 則貼文、13,399 則留言**，回溯至 2015-08-20 | 實測成本仍低（見 §8.5），無須限縮歷史。首次以 `--all` 回溯至粉專最早貼文，之後每次以 `--since` 接續。<br>**註**：初期估算誤稱「1,500 則貼文 / 4,917 則留言」，係因探測腳本設有 15 頁上限而低估約 2.7 倍，已於 M2 實際入庫後更正 |
| **DEC3** | **決策（2026-08-19）：v1 暫不處理圖片留言** | 967 則 photo、415 則 GIF 完全未經判讀；已知有攻擊文字做成圖片以規避偵測（§16 R10） | 前置成本高：資料庫未存圖片網址，須重抓全部留言（約 12 分鐘）；且 Graph API 是否回傳圖片網址尚未驗證。費用估算不確定性大（US$1.7–6.6）。**此為暫緩而非放棄** —— R10 的風險仍然存在，報表使用者應知悉圖片留言為已知盲區 |
| **Q2** | 執行頻率？（每日排程 / 手動） | 影響 M8 設計 | **待確認** |
| **Q3** | 報表交付對象與格式偏好（Excel / 內部儀表板） | 影響 M6 | **待確認** |
| **Q4** | 是否需將 `complaint` 類別串接既有客訴流程 | 範圍擴充 | **待確認** |
| **Q5** | 是否規劃 Phase 2 網頁版供公傳室小編使用？若是，內網 VM 申請流程為何 | 決定是否需提早與資訊室接洽 | **待確認**（見 §17） |

---

## 17. 未來階段：多人使用與網路部署（Phase 2，非 v1 範圍）

> 本節為**預留規劃**，明確界定「若日後要讓公傳室小編透過網頁使用」需要什麼前提。
> v1 一律以本機 CLI + Excel 交付，**不得**在未完成本節前置條件的情況下對外提供服務。

### 17.1 為何 v1 不直接做網頁版

| 理由 | 說明 |
| --- | --- |
| 價值未經驗證 | 分類準確度、小編是否真的採用，皆須實測。Phase 1 可能得出「不需要」的結論 |
| 成本不對稱 | 網頁版工作量約為 v1 的 2–3 倍，且產生持續維運責任 |
| 風險性質改變 | 見 §17.4 |

### 17.2 需要改動的部分

| 層 | v1（本機） | Phase 2（內網） |
| --- | --- | --- |
| 介面 | CLI + Excel | Web UI（清單、篩選、複核操作） |
| 資料庫 | SQLite 單檔 | PostgreSQL（多人並發寫入 `reviews`） |
| 機密管理 | `.env` | 伺服器端 secret 管理，token 不落地於程式碼庫 |
| 身分驗證 | 無 | 院內 SSO / LDAP 整合 |
| 授權 | 無 | 角色分級（如：小編僅見高風險、主管見全量） |
| 稽核 | 無 | 存取軌跡：誰、何時、查詢了哪些留言 |
| 傳輸 | 無 | HTTPS（院內憑證） |
| 備份 | 手動複製檔案 | 由資訊室納管 |

### 17.3 部署位置評估

| 選項 | 評價 | 結論 |
| --- | --- | --- |
| **院內內網 VM** | 資料不出院、走既有資安規範、資訊室納管 | ✅ **唯一建議** |
| 公有雲 | 個資離開院內，須經資安與個資保護窗口審查 | ⚠️ 除非院方已有核准之雲端環境，否則不建議 |
| 個人 VPS／自建對外主機 | 以個人身分承擔院方個資責任 | ❌ **禁止** |

### 17.4 風險性質的改變（必讀）

v1 的產出是「一次性的 Excel，看完即處理」。
Phase 2 的產出是「**長期累積、可搜尋、多人可存取的民眾言論資料庫**」，內含留言者識別碼、
留言原文，以及 AI 給定的 `personal_attack`／`politics` 等標記。

此性質改變意謂：系統須能對「為何建立民眾言論檔案」提出正當性說明。
因此 §5 之全部原則於 Phase 2 仍完全適用，且需額外強化：最小權限、稽核軌跡、保存期限自動執行。

### 17.5 啟動 Phase 2 的前置條件（全部滿足才可開工）

| # | 前置條件 | 負責對話對象 |
| --- | --- | --- |
| P1 | Phase 1 已運行 ≥ 3 個月，且有量化成效數據 | 專案自身 |
| P2 | 分類品質達 §1.4 成功條件 | 專案自身 |
| P3 | 取得內網 VM 與資安檢測要求 | 資訊室 |
| P4 | 個資檔案登記需求、保存期限、當事人權利處理程序確認 | 個資保護窗口／法務 |
| P5 | 存取角色與權限分級核定 | 公傳室主管 |
| P6 | 確認維運責任歸屬（誰負責更新、備份、權杖換發） | 資訊室／公傳室 |

> 建議以 Phase 1 的實際數據作為向資訊室提案的依據，較空泛提案容易取得資源。

---

## 附錄 A：Graph API 回應樣本（留言）

```json
{
  "data": [
    {
      "id": "17845...789_10012...345",
      "message": "醫護人員辛苦了，感謝你們",
      "created_time": "2026-08-01T03:12:45+0000",
      "from": { "id": "1234567890123456", "name": "王小明" },
      "like_count": 12,
      "comment_count": 1,
      "permalink_url": "https://www.facebook.com/...",
      "is_hidden": false
    }
  ],
  "paging": {
    "cursors": { "before": "QVFI...", "after": "QVFI..." },
    "next": "https://graph.facebook.com/v23.0/..."
  }
}
```

## 附錄 B：報表欄位（Excel Sheet1「需關注留言」）

`嚴重度 | 標籤 | 留言時間 | 貼文連結 | 留言連結(permalink) | 留言內容 | 判斷理由 | 已隱藏 | 複核狀態 | 複核備註`

> 不含留言者欄位 —— 標準存取下無此資料（§7.5）。小編經 **留言連結** 點回 Facebook 即可看到留言者並直接處置。
>
> **預設只列出未隱藏的留言**（FR-13）。已隱藏者代表小編先前已處理，若每次報表都重複列出，
> 工具會迅速失去可用性。需複查時以 `--include-hidden` 納入。

排序：`max_severity` 降冪 → `created_time` 降冪。條件式格式：severity 3 紅底、2 橘底、1 黃底。

> **`--min-severity` 預設為 1（2026-08-19 依實測修訂）**。
> 初版預設為 2，實測偵測率僅 58%：人工認定有問題的 69 則中，有 20 則系統確實標記了
> （嚴重度 1）卻被門檻擋在報表外，其中 15 則為嘲諷 —— 而 `sarcasm` 的嚴重度範圍
> 本就是 1–2，天生容易落在 1。
>
> 門檻改為 1 後偵測率升至 **87%**，報表則數自 300 增至約 600（相對 14,315 則仍極小）。
> 報表本就依嚴重度降冪排序，最嚴重者在最上方，故降低門檻不影響閱讀效率。
