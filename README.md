# Facebook 粉專留言擷取與風險內容偵測

醫院粉絲專頁留言的擷取、風險分類與人工複核報表工具。

從數千則留言中收斂出需要小編處理的少數，取代人工逐則翻閱。
**完整規格見 [sdd.md](sdd.md)；開發前請先讀 §5（法遵、隱私與倫理），該節為硬性約束。**

- [研究論文討論簡報-詐騙](https://hlm.tzuchi.com.tw/home/images/peter/research_2026-10-15.pdf)

## 這個工具做什麼

```
Facebook Graph API
      ↓  擷取貼文與留言（含已隱藏者）
   SQLite
      ↓  規則前篩（空白／個資／廣告）
      ↓  LLM 分類（人身攻擊、機構謾罵、嘲諷、政治…）
   Excel 報表
      ↓  依嚴重度排序，附直達 Facebook 的留言連結
   人工複核
```

實測規模：**4,199 則貼文、14,664 則留言 → 776 則需關注**（2015-08 ～ 2026-10）。

## 價值

| | |
| --- | --- |
| **涵蓋率** | 十一年、14,664 則留言全數判讀。人工做不到，所以漏掉什麼也無從得知 |
| **跨貼文關聯** | 實測發現同一段文字貼 55 次散在 55 則貼文（全被逐則隱藏，等於 55 次手動操作）、另一段貼 41 次散在 13 則貼文（僅 25 則被隱藏，16 則漏網）。逐則審核的介面讓協同行為在人眼前解體成雜訊 |
| **證據留存** | 資料庫含 FB 已不再回傳的留言（遭刪除或過濾），這部分無法重新取得 |
| **意外發現** | 民眾主動通報冒用醫院名義詐騙的留言，16 則中有 11 則被評為嚴重度 0 —— 系統為偵測攻擊而設計，而這類留言性質相反 |

**它不是什麼**：不減少人力（複核仍須人工）、不防止攻擊發生、單則判斷不比人準（最佳類別準確率 0.85）。
優勢在涵蓋率與一致性，不在判斷力。

## 成本

| 項目 | 金額／時間 |
| --- | --- |
| 建置（一次性全量判讀） | US$6.46 |
| 每月判讀 | 約 US$1（實測兩個月份 US$0.29） |
| 每月執行 | 約 1 分鐘，單一指令 |
| 每月複核（估） | 約 10 分鐘 |

Graph API 不計費，重抓只花時間。真正的成本是**存量清理的一次性人力**（776 則，約 4 小時）。

## 這個工具不做什麼

- **不執行任何自動處置**。不隱藏、不刪除、不封鎖、不回覆。所有動作由人工在 Meta Business Suite 執行。
- **不蒐集留言者身分**。標準存取下 Graph API 不回傳一般民眾的姓名或 ID，
  資料庫中亦無該欄位。複核者經留言連結至 Facebook 查看。
- **不處理圖片留言**（v1 已知盲區，見 [sdd.md](sdd.md) §16 R10）。

## 快速開始

需要 Python 3.12+ 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env          # 填入 FB_PAGE_ID 與 FB_PAGE_ACCESS_TOKEN
uv run smc init
uv run smc token check
```

權杖取得流程見 [docs/token-setup.md](docs/token-setup.md)（含逐步驗證）。

```bash
uv run smc fetch posts --all      # 首次全量
uv run smc fetch comments         # 逐則貼文 checkpoint，可中斷續跑
uv run smc classify               # 規則前篩 + LLM 分類
uv run smc report                 # 產出 Excel
```

`smc status` 隨時可看資料量、擷取涵蓋範圍與待辦。

### 例行更新

首次建庫之後，用腳本跑，不要手動下指令：

```bash
scripts/monthly-update.sh
```

理由是 `fetch comments` 不加參數時**只處理從未同步過的貼文**，
舊貼文上的新留言不會進資料庫 —— 而且漏掉不會報錯，報表看起來完全正常。
腳本固定以 `--changed-only` 涵蓋變動貼文，每季強制一次 `--full-rescan` 兜底，
分類前先報價待確認，全程寫入 `data/logs/`。

## 指令

| 指令 | 說明 |
| --- | --- |
| `smc init` | 建立目錄與資料庫 schema |
| `smc token check` | 驗證權杖有效性、到期時間與權限 |
| `smc fetch posts` / `comments` / `all` | 擷取。`--since`／`--until` 作用於**貼文時間** |
| `smc fetch comments --changed-only` | 只抓 FB 回報留言數與本地不符者。須先 `fetch posts --all` 刷新計數 |
| `smc fetch comments --full-rescan` | 全部重抓。差異重掃的安全網，建議每季一次 |
| `smc classify` | 分類。`--dry-run` 以 `count_tokens` 精算費用，不計費 |
| `smc report` | 報表。`--since`／`--until` 作用於**留言時間** |
| `smc eval import` / `run` | 匯入既有人工標註、量測分類品質 |
| `smc status` | 資料統計與擷取涵蓋範圍 |
| `smc purge` | 清除逾保存期限資料（M8，未實作） |

離開碼：`0` 成功、`1` 一般錯誤、`2` 設定／權杖錯誤、`3` 部分失敗。

## 品質

以既有的 877 則人工標註為基準（`smc eval run`）：

| 人工類別 | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| 謾罵／人身攻擊 | 0.85 | 0.76 | 0.80 |
| 嘲諷／陰陽怪氣 | 0.71 | 0.63 | 0.67 |
| 負面質疑 | 0.44 | 0.62 | 0.52 |

**偵測率 87%** —— 人工認定有問題的留言中，被標記進報表的比例。
此指標比 F1 更貼近實務：標錯類別但仍進報表，與完全漏抓的後果天差地遠。

每次修改提示詞都須重跑評估並記錄於 [docs/eval-log.md](docs/eval-log.md)，避免無據調參。

## 架構

```
src/smc/
├── cli.py          typer 入口
├── config.py       設定與機密遮罩
├── fb/             Graph API：分頁、退避、錯誤分類
├── storage/        SQLModel + SQLite
├── classify/       規則前篩、LLM 分類、評估指標
├── report/         Excel 產生
└── util/           雜湊、區間運算、速率控制

scripts/
└── monthly-update.sh   例行更新流程（四步驟，有順序相依）

config/lexicon/     關鍵詞表（業務人員可直接維護）
config/context.md   判讀背景（依當前輿情更新）
prompts/            分類提示詞，版本化
```

## 開發

```bash
uv run pytest        # 130 個測試
uv run ruff check .
```

### 本機設定檔

版控中的 `config/lexicon/*.txt` 與 `config/context.md` **僅為範本**。
實際使用時請建立同名的 `.local` 版本（已列入 `.gitignore`），程式會優先採用：

```
config/lexicon/spam.local.txt
config/lexicon/self_harm.local.txt
config/lexicon/political.local.txt
config/context.local.md
```

公開版本刻意不含真實內容：一份「監測中的政治關鍵詞」清單若被單獨截圖轉發，
讀者不會看到規格 §5.5「政治為中性標記、不得作為處置依據」的說明；
而公開完整偵測準則等同於教人規避 —— 實測已證實有人將攻擊文字製成圖片以閃避文字偵測。

測試透過 [tests/conftest.py](tests/conftest.py) 與真實 `.env`、資料庫、API 隔離。
若 `test_environment_is_isolated_from_production` 失敗，代表測試可能正在操作正式資料庫
—— 立即停止並修復，不要略過。

## 資料安全

`data/` 內含民眾留言，屬個資檔案，已列入 `.gitignore`。

| 要求 | 檢查方式 |
| --- | --- |
| 全碟加密 | `fdesetup status` → `FileVault is On.` |
| **不得**位於雲端同步資料夾 | `pwd` 檢查；搬移專案後須重新確認 |
| 不進版控 | `git check-ignore -v data/comments.db` |

第二項最易疏忽：專案若被搬進 iCloud／Dropbox，含個資的資料庫會**自動上傳至院外**。

## 進度

| 里程碑 | 狀態 |
| --- | --- |
| M0 專案骨架 | ✅ |
| M1 權杖驗證 | ✅ |
| M2 貼文擷取 + DB | ✅ |
| M3 留言擷取（分頁、增量、對帳） | ✅ |
| M4 規則前篩 | ✅ |
| M5 LLM 分類 | ✅ |
| M6 Excel 報表 | ✅ |
| M7 品質驗證 | ✅ |
| M8 維運（複核回寫、purge、排程） | ⬜ |

M8 未做的後果需留意：**目前無法標記「已處理」**（`reviews` 表為空），
因此報表每次都會把同一批留言重新列出，清單不會變短。

[sdd2.md](sdd2.md) 為下一階段規格（爭議留言即時通報與刪除偵測），**僅規格，尚未實作**。

本工具設計為**單一管理者的本機工具**，非多人服務。若日後需改為內網多人使用，前置條件見 [sdd.md](sdd.md) §17。

## 已知限制

| 限制 | 說明 |
| --- | --- |
| 圖片留言未判讀 | 987 則純圖片留言完全未經處理；已知有人以此規避偵測（§16 R10、DEC3） |
| 無留言者身分 | 標準存取限制；核心功能不受影響（§16 DEC1） |
| 嘲諷判讀較弱 | 依賴 `config/context.md` 的時事背景，需定期更新 |
| 對帳有落差 | FB 回報數多於實抓數，多數在 2% 內，但實測有單則貼文落差達一半（FB 363 → 實抓 185）。成因為遭刪除或遭 FB 過濾的留言不再回傳，無從取得（§7.5） |
| 資料庫未備份 | `data/comments.db` 含已無法從 FB 重抓的留言，目前無自動備份機制 |
