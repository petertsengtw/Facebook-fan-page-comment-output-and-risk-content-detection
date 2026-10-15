#!/usr/bin/env bash
#
# 每月更新：刷新貼文 → 差異重掃留言 → 分類 → 產表
#
# 本腳本存在的理由：fetch comments 不加參數時只處理從未同步過的貼文
# （repo.py），舊貼文上的新留言永遠不會進資料庫，而且漏掉不會報錯、
# 報表也看起來完全正常。這種缺漏沒有任何徵兆，不該靠人記得。
#
# 用法：
#   scripts/monthly-update.sh                     # 互動，分類前先報價
#   scripts/monthly-update.sh --yes               # 不詢問（排程用）
#   scripts/monthly-update.sh --since 2026-01-01  # 報表區間（預設自動推算）
#   scripts/monthly-update.sh --force-rescan      # 強制全量重掃
#   scripts/monthly-update.sh --skip-rescan       # 跳過全量重掃安全網

set -uo pipefail          # 不用 -e：smc 的結束碼 3 是「部分完成」，不該中斷流程
#
# ⚠ bash 3.2（macOS 內建）在 UTF-8 locale 下，$VAR 緊接全形字元會把多位元組
#   當成變數名的一部分而噴 unbound variable。變數後面接中文一律寫 ${VAR}。

cd "$(dirname "$0")/.."
ROOT="$PWD"
SMC="$ROOT/.venv/bin/smc"
PY="$ROOT/.venv/bin/python"
RESCAN_STAMP="$ROOT/data/.last-full-rescan"
REPORT_STAMP="$ROOT/data/.last-report"
RESCAN_MAX_DAYS=90        # 差異重掃的死角由每季一次的全量重掃兜底

SINCE=""
ASSUME_YES=0
FORCE_RESCAN=0
SKIP_RESCAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)        SINCE="$2"; shift 2 ;;
    --yes|-y)       ASSUME_YES=1; shift ;;
    --force-rescan) FORCE_RESCAN=1; shift ;;
    --skip-rescan)  SKIP_RESCAN=1; shift ;;
    -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知參數：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ROOT/data/logs" "$ROOT/data/reports"
LOG="$ROOT/data/logs/update-${TS}.log"

say()  { printf '\n\033[1;36m══ %s\033[0m\n' "$*" | tee -a "$LOG"; }
info() { printf '   %s\n' "$*" | tee -a "$LOG"; }
warn() { printf '\033[1;33m   ⚠  %s\033[0m\n' "$*" | tee -a "$LOG"; }
die()  { printf '\033[1;31m   ✗  %s\033[0m\n' "$*" | tee -a "$LOG"; exit 1; }

# smc 結束碼：0 正常、3 部分完成（可接受）、2 設定錯誤、其餘失敗
run_smc() {
  local label="$1"; shift
  info "\$ smc $*"
  "$SMC" "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  case $rc in
    0) return 0 ;;
    3) warn "${label} 部分完成（結束碼 3）—— 通常是個別貼文權限不足，流程繼續"; return 0 ;;
    2) die "${label} 設定錯誤（結束碼 2）：檢查 .env、權杖是否過期" ;;
    *) die "${label} 失敗（結束碼 ${rc}）：詳見 $LOG" ;;
  esac
}

[[ -x "$SMC" ]] || die "找不到 $SMC —— 先建立虛擬環境並安裝（uv sync）"
[[ -f "$ROOT/.env" ]] || die "找不到 .env —— 權杖與金鑰未設定"

say "更新開始　$(date '+%Y-%m-%d %H:%M')　日誌：${LOG#$ROOT/}"
run_smc "狀態檢查" status

# ── 報表區間 ──
# 不可用「資料庫最新貼文往回 N 天」推算：同步成功後最新貼文就是今天，
# 報表會縮成「只看最近三天」。改以上次產表日為起點，才是真正的「這次新增」。
if [[ -z "$SINCE" ]]; then
  if [[ -f "$REPORT_STAMP" ]]; then
    SINCE="$(cat "$REPORT_STAMP")"
    info "報表區間起點（上次產表日）：${SINCE}"
  else
    SINCE="$("$PY" -c "import datetime;print((datetime.date.today()-datetime.timedelta(days=90)).isoformat())")"
    warn "查無上次產表紀錄，改用近 90 天：${SINCE}"
    warn "要涵蓋全部存量請改用：--since 2015-01-01"
  fi
fi

# ── 步驟 1：刷新全部貼文 ──
# 必須是 --all 而非 --since：差異重掃要比對「FB 回報留言數」與本地計數，
# 只刷新近期貼文的話，舊貼文的計數仍是舊的，變動偵測不到。
# 4,145 則貼文約 42 次呼叫（每頁 100 則），十幾秒，便宜到沒有省的必要。
say "步驟 1／4　刷新貼文與留言計數（全部）"
run_smc "fetch posts --all" fetch posts --all

# ── 步驟 2：差異重掃 ──
# 涵蓋「從未同步」與「FB 計數與本地不符」兩類，故不需再單獨跑一次增量。
say "步驟 2／4　差異重掃留言（僅計數有變動者）"
run_smc "fetch comments --changed-only" fetch comments --changed-only

# ── 步驟 3：全量重掃（安全網，每季一次）──
# 差異重掃的死角：同一則貼文新增一則又刪除一則時總數不變，因而被跳過。
say "步驟 3／4　全量重掃安全網"
rescan_due=1
if [[ -f "$RESCAN_STAMP" ]]; then
  last="$(cat "$RESCAN_STAMP")"
  days=$(( ( $(date +%s) - $(date -j -f '%Y-%m-%d' "$last" +%s 2>/dev/null \
            || date -d "$last" +%s) ) / 86400 ))
  info "上次全量重掃：${last}（${days} 天前，門檻 ${RESCAN_MAX_DAYS} 天）"
  (( days < RESCAN_MAX_DAYS )) && rescan_due=0
else
  warn "查無全量重掃紀錄，視為需要執行"
fi

if (( SKIP_RESCAN )); then
  warn "已指定 --skip-rescan，略過安全網"
elif (( FORCE_RESCAN )) || (( rescan_due )); then
  info "本次執行全量重掃（約 2,200 次呼叫，十餘分鐘）"
  run_smc "fetch comments --full-rescan" fetch comments --full-rescan
  date +%F > "$RESCAN_STAMP"
  info "已記錄於 ${RESCAN_STAMP#$ROOT/}"
else
  info "未達 ${RESCAN_MAX_DAYS} 天，略過（--force-rescan 可強制）"
fi

# ── 步驟 4：分類與產表 ──
say "步驟 4／4　分類與產表"
info "先估算用量（不寫入、不計費）"
run_smc "classify --dry-run" classify --dry-run

if (( ! ASSUME_YES )); then
  printf '\n   以上為預估用量。要繼續分類嗎？[y/N] '
  read -r ans
  [[ "$ans" =~ ^[Yy]$ ]] || { warn "使用者中止，未進行分類"; exit 0; }
fi
run_smc "classify" classify

REPORT="data/reports/需關注留言_$(date +%Y%m%d).xlsx"
run_smc "report" report --since "$SINCE" --output "$REPORT"
date +%F > "$REPORT_STAMP"

say "完成"
info "報表：${REPORT}（區間 ${SINCE} 起）"
info "日誌：${LOG#$ROOT/}"
[[ -f "$RESCAN_STAMP" ]] && info "上次全量重掃：$(cat "$RESCAN_STAMP")"
