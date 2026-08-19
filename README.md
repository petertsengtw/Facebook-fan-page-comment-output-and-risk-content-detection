# Facebook 粉專留言擷取與風險內容偵測

醫院粉絲專頁留言的擷取、風險分類與人工複核報表工具。

從數千則留言中收斂出需要小編處理的少數，取代人工逐則翻閱。
**完整規格見 [sdd.md](sdd.md)；開發前請先讀 §5（法遵、隱私與倫理），該節為硬性約束。**

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

實測規模：**4,145 則貼文、14,315 則留言 → 694 則需關注**，全量分類一次約 US$6.5。

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
uv run smc fetch posts --all      # 首次全量；之後用 --since 增量
uv run smc fetch comments         # 逐則貼文 checkpoint，可中斷續跑
uv run smc classify               # 規則前篩 + LLM 分類
uv run smc report                 # 產出 Excel
```

`smc status` 隨時可看資料量、擷取涵蓋範圍與待辦。

## 指令

| 指令 | 說明 |
| --- | --- |
| `smc init` | 建立目錄與資料庫 schema |
| `smc token check` | 驗證權杖有效性、到期時間與權限 |
| `smc fetch posts` / `comments` / `all` | 擷取。`--since`／`--until` 作用於**貼文時間** |
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

config/lexicon/     關鍵詞表（業務人員可直接維護）
config/context.md   判讀背景（依當前輿情更新）
prompts/            分類提示詞，版本化
```

## 開發

```bash
uv run pytest        # 128 個測試
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

## 已知限制

| 限制 | 說明 |
| --- | --- |
| 圖片留言未判讀 | 967 則 photo 完全未經處理；已知有人以此規避偵測（§16 R10、DEC3） |
| 無留言者身分 | 標準存取限制；核心功能不受影響（§16 DEC1） |
| 嘲諷判讀較弱 | 依賴 `config/context.md` 的時事背景，需定期更新 |
| 對帳誤差約 2% | `summary.total_count` 與實抓數的落差，成因見 §7.5 |
