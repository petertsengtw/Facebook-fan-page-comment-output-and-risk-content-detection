#!/usr/bin/env bash
#
# 每月更新：抓取 → 重掃 → 分類 → 產表
#
# 四個步驟有順序相依，其中「全量重掃」漏掉不會報錯、也不會有任何徵兆，
# 只是舊貼文上的新留言永遠不會進資料庫（見 repo.py:124）。
# 本腳本的存在就是為了讓那一步不可能被忘記。
#
# 用法：
#   scripts/monthly-update.sh                 # 互動，分類前會先報價
#   scripts/monthly-update.sh --yes           # 不詢問（排程用）
#   scripts/monthly-update.sh --since 2026-08-15        # 指定抓取起點
#   scripts/monthly-update.sh --report-since 2026-01-01 # 報表另訂區間
#   scripts/monthly-update.sh --force-rescan  # 強制重掃，不管距離上次幾天
#   scripts/monthly-update.sh --skip-rescan   # 明確跳過重掃（自負後果）

set -uo pipefail          # 不用 -e：smc 的結束碼 3 是「部分完成」，不該中斷流程

cd "$(dirname "$0")/.."
ROOT="$PWD"
SMC="$ROOT/.venv/bin/smc"
PY="$ROOT/.venv/bin/python"
RESCAN_STAMP="$ROOT/data/.last-full-rescan"
RESCAN_MAX_DAYS=30

SINCE=""
REPORT_SINCE=""
ASSUME_YES=0
FORCE_RESCAN=0
SKIP_RESCAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)         SINCE="$2"; shift 2 ;;
    --report-since)  REPORT_SINCE="$2"; shift 2 ;;
    --yes|-y)        ASSUME_YES=1; shift ;;
    --force-rescan)  FORCE_RESCAN=1; shift ;;
    --skip-rescan)   SKIP_RESCAN=1; shift ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知參數：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ROOT/data/logs" "$ROOT/data/reports"
LOG="$ROOT/data/logs/update-$TS.log"

say()  { printf '\n\033[1;36m══ %s\033[0m\n' "$*" | tee -a "$LOG"; }
info() { printf '   %s\n' "$*" | tee -a "$LOG"; }
warn() { printf '\033[1;33m   ⚠  %s\033[0m\n' "$*" | tee -a "$LOG"; }
die()  { printf '\033[1;31m   ✗  %s\033[0m\n' "$*" | tee -a "$LOG"; exit 1; }

# smc 結束碼：0 正常、3 部分完成（可接受）、其餘為失敗
run_smc() {
  local label="$1"; shift
  info "$ $SMC $*"
  "$SMC" "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  case $rc in
    0) return 0 ;;
    3) warn "$label 部分完成（結束碼 3）—— 通常是個別貼文權限不足，流程繼續" ; return 0 ;;
    2) die "$label 設定錯誤（結束碼 2）：檢查 .env 與權杖是否過期" ;;
    *) die "$label 失敗（結束碼 $rc）：詳見 $LOG" ;;
  esac
}

[[ -x "$SMC" ]] || die "找不到 $SMC —— 先建立虛擬環境並安裝（uv sync）"
[[ -f "$ROOT/.env" ]] || die "找不到 .env —— 權杖與金鑰未設定"

say "更新開始　$(date '+%Y-%m-%d %H:%M')　日誌：${LOG#$ROOT/}"
run_smc "狀態檢查" status

# ── 起始日期：以資料庫最新貼文往回 3 天，避免邊界落在某則貼文上形成破洞 ──
if [[ -z "$SINCE" ]]; then
  SINCE="$("$PY" - <<'PYEOF'
import sqlite3, datetime, pathlib
db = pathlib.Path("data/comments.db")
if not db.exists():
    print(""); raise SystemExit
row = sqlite3.connect(db).execute("select max(created_time) from posts").fetchone()[0]
if not row:
    print(""); raise SystemExit
d = datetime.date.fromisoformat(str(row)[:10]) - datetime.timedelta(days=3)
print(d.isoformat())
PYEOF
)"
  [[ -n "$SINCE" ]] || die "無法從資料庫推算起始日期，請用 --since 指定"
  info "起始日期（自動推算，已含 3 天重疊）：$SINCE"
fi
: "${REPORT_SINCE:=$SINCE}"

say "步驟 1／4　抓取貼文（自 $SINCE）"
run_smc "fetch posts" fetch posts --since "$SINCE"

say "步驟 2／4　抓取新貼文的留言"
run_smc "fetch comments" fetch comments

# ── 步驟 3：全量重掃。漏掉不會報錯，所以由腳本決定而非由人記得 ──
say "步驟 3／4　全量重掃（舊貼文的新留言）"
rescan_due=1
if [[ -f "$RESCAN_STAMP" ]]; then
  last="$(cat "$RESCAN_STAMP")"
  days=$(( ( $(date +%s) - $(date -j -f '%Y-%m-%d' "$last" +%s 2>/dev/null \
            || date -d "$last" +%s) ) / 86400 ))
  info "上次重掃：$last（$days 天前）"
  (( days < RESCAN_MAX_DAYS )) && rescan_due=0
else
  warn "查無重掃紀錄，視為需要重掃"
fi

if (( SKIP_RESCAN )); then
  warn "已指定 --skip-rescan。舊貼文上的新留言本次不會進資料庫，報表看起來仍會正常。"
elif (( FORCE_RESCAN )) || (( rescan_due )); then
  info "本次執行重掃（約 2,200 次 API 呼叫，需數十分鐘）"
  run_smc "fetch comments --full-rescan" fetch comments --full-rescan
  date +%F > "$RESCAN_STAMP"
  info "重掃完成，已記錄於 ${RESCAN_STAMP#$ROOT/}"
else
  info "距上次重掃未滿 $RESCAN_MAX_DAYS 天，略過（--force-rescan 可強制）"
fi

say "步驟 4／4　分類與產表"
info "先估算成本（不寫入、不計費）"
run_smc "classify --dry-run" classify --dry-run

if (( ! ASSUME_YES )); then
  printf '\n   以上為預估用量。要繼續分類嗎？[y/N] '
  read -r ans
  [[ "$ans" =~ ^[Yy]$ ]] || { warn "使用者中止，未進行分類"; exit 0; }
fi

run_smc "classify" classify

REPORT="data/reports/需關注留言_$(date +%Y%m%d).xlsx"
run_smc "report" report --since "$REPORT_SINCE" --output "$REPORT"

say "完成"
info "報表：$REPORT"
info "日誌：${LOG#$ROOT/}"
if [[ -f "$RESCAN_STAMP" ]]; then
  info "上次全量重掃：$(cat "$RESCAN_STAMP")"
fi
