"""smc CLI 入口（規格 §9）。

離開碼約定：0 成功、1 一般錯誤、2 設定/權杖錯誤、3 部分失敗。
"""
from __future__ import annotations

import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from itertools import pairwise
from pathlib import Path

import anthropic
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from sqlmodel import select

from smc import __version__
from smc.classify.evaluate import (
    compute_coarse_metrics,
    compute_metrics,
    confusion_pairs,
    detection_recall,
    import_manual_annotations,
    is_coarse_goldset,
    load_any_goldset,
    macro_f1,
    write_goldset_template,
    write_goldset_xlsx,
)
from smc.classify.labels import CHINESE_NAME, Label
from smc.classify.llm import (
    Batch,
    CommentInput,
    LLMFatalError,
    build_batches,
    classify_batch,
    count_batch_tokens,
    load_prompt,
)
from smc.classify.merge import hits_to_json, max_severity, merge_hits
from smc.classify.rules import Lexicon, prefilter
from smc.config import PROJECT_ROOT, get_settings, mask
from smc.fb.client import GraphClient
from smc.fb.errors import GraphAPIError
from smc.fb.models import comment_to_row, post_to_row
from smc.report.excel import build_workbook
from smc.report.excel import save as save_workbook
from smc.storage import repo
from smc.storage.db import init_schema, make_engine, session_scope
from smc.storage.models import Classification, Comment, Post
from smc.util.hashing import normalize_message
from smc.util.intervals import DATETIME_MIN

console = Console()
err_console = Console(stderr=True)


class Exit(IntEnum):
    OK = 0
    ERROR = 1
    CONFIG = 2
    PARTIAL = 3


NEEDED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_read_user_content"}
DEFAULT_FETCH_DAYS = 90  # 規格 §9.1

app = typer.Typer(
    name="smc",
    help="花蓮慈濟醫院 FB 粉專留言匯出與風險內容偵測（規格見 sdd.md）",
    no_args_is_help=True,
    add_completion=False,
)
token_app = typer.Typer(help="權杖相關操作", no_args_is_help=True)
fetch_app = typer.Typer(help="自 Graph API 擷取資料", no_args_is_help=True)
review_app = typer.Typer(help="人工複核結果回寫", no_args_is_help=True)
app.add_typer(token_app, name="token")
app.add_typer(fetch_app, name="fetch")
app.add_typer(review_app, name="review")


def _display_path(path: Path) -> str:
    """專案內顯示相對路徑，專案外（如加密備份磁碟）顯示絕對路徑。"""
    try:
        return f"{path.relative_to(PROJECT_ROOT)}"
    except ValueError:
        return str(path)


def _parse_date(value: str | None, *, flag: str) -> datetime | None:
    """解析 --since/--until 的 YYYY-MM-DD。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        err_console.print(f"[red]{flag} 格式錯誤[/red]：{value!r}，應為 YYYY-MM-DD")
        raise typer.Exit(Exit.CONFIG) from exc


def _todo(milestone: str, what: str) -> None:
    """尚未實作的里程碑：明確告知而非靜默失敗。"""
    err_console.print(f"[yellow]尚未實作[/yellow]：{what}")
    err_console.print(f"  屬里程碑 [bold]{milestone}[/bold] 範圍，見 sdd.md §14。")
    raise typer.Exit(Exit.ERROR)


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", help="顯示版本後結束"),
) -> None:
    if version:
        console.print(f"smc {__version__}")
        raise typer.Exit(Exit.OK)


# ─────────────────────────────── init ───────────────────────────────
@app.command()
def init(force: bool = typer.Option(False, "--force", help="重建既有目錄結構")) -> None:
    """建立 data/ 目錄結構並檢查設定完整性（規格 §9）。"""
    s = get_settings()
    for path in (s.data_dir, s.reports_dir, s.logs_dir):
        path.mkdir(parents=True, exist_ok=True)
        console.print(f"  [green]✓[/green] {_display_path(path)}/")

    engine = make_engine(s.db_path)
    init_schema(engine)
    console.print(f"  [green]✓[/green] {_display_path(s.db_path)}（schema 已建立）")

    missing = s.missing_for_fetch()
    if missing:
        err_console.print(f"\n[red]✗[/red] .env 尚缺：{', '.join(missing)}")
        err_console.print("  參考 docs/token-setup.md 取得權杖後填入。")
        raise typer.Exit(Exit.CONFIG)
    console.print("\n[green]完成[/green]。下一步：[bold]smc token check[/bold]")


# ────────────────────────────── token ───────────────────────────────
@token_app.command("check")
def token_check() -> None:
    """驗證 Page Token 的有效性、到期時間與 scopes（規格 FR-01 / FR-12）。"""
    s = get_settings()
    if missing := s.missing_for_fetch():
        err_console.print(f"[red]設定缺漏[/red]：{', '.join(missing)}")
        raise typer.Exit(Exit.CONFIG)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("API 版本", s.fb_api_version)
    table.add_row("Page ID", s.fb_page_id)
    table.add_row("Page Token", mask(s.fb_page_access_token))
    console.print(table)
    console.print()

    with GraphClient(s) as client:
        try:
            page = client.page_info()
        except GraphAPIError as exc:
            err_console.print(f"[red]讀取粉專失敗[/red]：{exc}")
            if exc.is_token_expired:
                err_console.print("  權杖已失效，請重跑 scripts/exchange_token.py 換發。")
            raise typer.Exit(Exit.CONFIG) from exc
        console.print(f"[green]✓[/green] 粉專：{page.get('name')}（{page.get('category', '—')}）")

        if not (s.fb_app_id and s.fb_app_secret):
            console.print("[yellow]○[/yellow] 未設定 FB_APP_ID / FB_APP_SECRET，略過權杖體檢")
            return

        try:
            data = client.debug_token()
        except GraphAPIError as exc:
            err_console.print(f"[yellow]○[/yellow] debug_token 失敗（非致命）：{exc}")
            return

        expires = data.get("expires_at")
        console.print(
            f"[green]✓[/green] 類型 {data.get('type')}｜有效 {data.get('is_valid')}｜"
            f"到期 {'永不過期' if expires == 0 else expires}"
        )
        scopes = set(data.get("scopes", []))
        if missing_scopes := NEEDED_SCOPES - scopes:
            err_console.print(f"[red]✗[/red] 缺少必要權限：{sorted(missing_scopes)}")
            raise typer.Exit(Exit.CONFIG)
        console.print(f"[green]✓[/green] 三項必要權限齊備 {sorted(scopes)}")


# ────────────────────────────── fetch ───────────────────────────────
@fetch_app.command("posts")
def fetch_posts(
    since: str = typer.Option(None, "--since", help="貼文發布時間起（YYYY-MM-DD）"),
    until: str = typer.Option(None, "--until", help="貼文發布時間迄（YYYY-MM-DD）"),
    all_history: bool = typer.Option(False, "--all", help="回溯至最早貼文"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只抓取不寫入"),
) -> None:
    """擷取貼文清單。--since/--until 作用於貼文發布時間（規格 §9.1）。"""
    s = get_settings()
    if missing := s.missing_for_fetch():
        err_console.print(f"[red]設定缺漏[/red]：{', '.join(missing)}")
        raise typer.Exit(Exit.CONFIG)

    since_dt = _parse_date(since, flag="--since")
    until_dt = _parse_date(until, flag="--until")
    if not (since_dt or all_history):
        since_dt = datetime.now(UTC) - timedelta(days=DEFAULT_FETCH_DAYS)
        console.print(
            f"[dim]未指定 --since，預設近 {DEFAULT_FETCH_DAYS} 天（--all 可回溯全部）[/dim]"
        )
    if since_dt and until_dt and since_dt > until_dt:
        err_console.print("[red]--since 不得晚於 --until[/red]")
        raise typer.Exit(Exit.CONFIG)

    engine = make_engine(s.db_path)
    init_schema(engine)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    naive_since = since_dt.replace(tzinfo=None) if since_dt else None
    naive_until = until_dt.replace(tzinfo=None) if until_dt else None
    rows: list[dict] = []

    # 先開一筆 sync_run；completed_at 保持 NULL 直到成功走完（規格 §6.7）
    run_id = None
    if not dry_run:
        with session_scope(engine) as session:
            run_id = repo.start_sync_run(
                session, kind="posts", since=naive_since,
                until=naive_until, started_at=fetched_at,
            ).id

    def _abort(message: str) -> None:
        if run_id is not None:
            with session_scope(engine) as session:
                repo.annotate_sync_run(session, run_id, f"中斷：{message}")

    with GraphClient(s) as client:
        try:
            page = client.page_info()
        except GraphAPIError as exc:
            err_console.print(f"[red]無法讀取粉專[/red]：{exc}")
            if exc.is_token_expired:
                err_console.print("  權杖已失效，請重跑 scripts/exchange_token.py。")
            _abort(str(exc))
            raise typer.Exit(Exit.CONFIG) from exc

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None), TextColumn("{task.completed} 則"),
            console=console, transient=True,
        ) as progress:
            task = progress.add_task("擷取貼文…", total=None)
            try:
                for post in client.list_posts(since=since_dt, until=until_dt):
                    rows.append(post_to_row(post, page_id=s.fb_page_id, fetched_at=fetched_at))
                    progress.advance(task)
            except GraphAPIError as exc:
                err_console.print(f"[red]擷取中斷[/red]：{exc}")
                err_console.print(f"  已取得 {len(rows)} 則，本次未計入涵蓋範圍。")
                _abort(str(exc))
                raise typer.Exit(Exit.CONFIG if exc.is_fatal else Exit.ERROR) from exc
            except KeyboardInterrupt:
                err_console.print("\n[yellow]使用者中斷[/yellow]，本次未計入涵蓋範圍。")
                _abort("使用者中斷")
                raise typer.Exit(Exit.ERROR) from None

    total_comments = sum(r["comments_count"] for r in rows)
    if dry_run:
        console.print(
            f"[yellow]dry-run[/yellow]：取得 {len(rows)} 則貼文、"
            f"FB 回報留言合計 {total_comments} 則，未寫入資料庫。"
        )
        raise typer.Exit(Exit.OK)

    completed_at = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(engine) as session:
        repo.upsert_page(session, page_id=s.fb_page_id, name=page.get("name", ""))
        result = repo.upsert_posts(session, rows)
        repo.mark_page_synced(session, s.fb_page_id, fetched_at)
        if run_id is not None:
            repo.complete_sync_run(
                session, run_id, completed_at=completed_at,
                posts_seen=len(rows), comments_seen=total_comments,
            )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("粉專", page.get("name", "—"))
    table.add_row("取得貼文", f"{len(rows)} 則")
    table.add_row("新增／更新", f"{result.inserted} ／ {result.updated}")
    table.add_row("FB 回報留言合計", f"{total_comments} 則")
    table.add_row("API 呼叫", f"{client.call_count} 次")
    lo = f"{naive_since:%Y-%m-%d}" if naive_since else "最早"
    hi = f"{naive_until:%Y-%m-%d}" if naive_until else "現在"
    table.add_row("本次涵蓋區間", f"{lo} ～ {hi}")
    console.print(table)
    console.print("\n[green]完成[/green]。下一步：[bold]smc fetch comments[/bold]（M3）")


# 對帳容差（規格 §7.5）：時間差造成的小幅落差屬正常
RECONCILE_ABS_TOLERANCE = 3
RECONCILE_PCT_TOLERANCE = 0.02
# 連續這麼多則貼文都無存取權，研判為全域權限問題而非個別貼文異常
GLOBAL_PERMISSION_THRESHOLD = 10
# 規則版本，寫入 classifications.model_version 供日後比對
RULES_VERSION = "rules-v1"
PROMPT_VERSION = "classify_v2"
# 成本估算用的取樣批次數（count_tokens 不計費，但逐批呼叫 580 次過慢）
COST_SAMPLE_BATCHES = 30


def _within_tolerance(expected: int, actual: int) -> bool:
    diff = abs(actual - expected)
    if diff <= RECONCILE_ABS_TOLERANCE:
        return True
    return expected > 0 and diff / expected <= RECONCILE_PCT_TOLERANCE


def _fetch_mode(full_rescan: bool, changed_only: bool) -> str:
    """供 sync_runs.note 標示本次選取模式，日後查涵蓋範圍時可區分。"""
    if full_rescan:
        return "全量重掃"
    return "差異重掃" if changed_only else "增量"


@fetch_app.command("comments")
def fetch_comments(
    post_id: str = typer.Option(None, "--post-id", help="只抓單一貼文"),
    full_rescan: bool = typer.Option(False, "--full-rescan", help="忽略 checkpoint 全量重掃"),
    changed_only: bool = typer.Option(
        False, "--changed-only",
        help="只抓 FB 回報留言數與本地不符者（須先 fetch posts 刷新計數）",
    ),
    max_posts: int = typer.Option(None, "--max-posts", help="最多處理 N 則貼文"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只抓取不寫入"),
) -> None:
    """擷取留言與回覆（含隱藏留言），逐則貼文 checkpoint 以支援中斷續跑。"""
    if full_rescan and changed_only:
        err_console.print("[red]--full-rescan 與 --changed-only 不可併用[/red]")
        raise typer.Exit(Exit.CONFIG)
    s = get_settings()
    if missing := s.missing_for_fetch():
        err_console.print(f"[red]設定缺漏[/red]：{', '.join(missing)}")
        raise typer.Exit(Exit.CONFIG)

    # 姓名雜湊 salt 為隱私必要條件（規格 §5.2）—— 缺少時直接擋下，不容許無 salt 寫入
    if not s.name_hash_salt:
        err_console.print("[red]NAME_HASH_SALT 未設定[/red]（規格 §5.2 要求留言者姓名雜湊儲存）")
        err_console.print("  請將下列一行加入 .env，然後重跑：\n")
        err_console.print(f"    NAME_HASH_SALT={secrets.token_urlsafe(32)}\n")
        err_console.print("  [dim]註：salt 變更後既有雜湊將無法比對，請勿隨意更換。[/dim]")
        raise typer.Exit(Exit.CONFIG)

    engine = make_engine(s.db_path)
    init_schema(engine)

    with session_scope(engine) as session:
        posts = repo.posts_to_fetch_comments(
            session, full_rescan=full_rescan, changed_only=changed_only, post_id=post_id
        )
    if max_posts:
        posts = posts[:max_posts]
    if not posts:
        console.print("[green]沒有待抓留言的貼文[/green]（--full-rescan 可強制重掃）")
        raise typer.Exit(Exit.OK)

    expected_comments = sum(p.comments_count for p in posts)
    console.print(
        f"待處理 [bold]{len(posts)}[/bold] 則貼文，FB 回報留言合計 "
        f"[bold]{expected_comments}[/bold] 則"
    )
    if not dry_run:
        est_min = len(posts) / max(s.fb_max_qps, 0.1) / 60
        console.print(f"[dim]以 {s.fb_max_qps} QPS 估算，至少需 {est_min:.0f} 分鐘。"
                      f"可隨時 Ctrl-C 中斷，已完成的貼文不會重抓。[/dim]")

    started_at = datetime.now(UTC).replace(tzinfo=None)
    run_id = None
    if not dry_run:
        with session_scope(engine) as session:
            run_id = repo.start_sync_run(
                session, kind="comments", since=None, until=None, started_at=started_at,
                note=f"{_fetch_mode(full_rescan, changed_only)}，{len(posts)} 則貼文",
            ).id

    stats = {"posts": 0, "inserted": 0, "updated": 0, "hidden": 0, "replies": 0}
    mismatches: list[tuple[str, int, int]] = []
    skipped: list[tuple[str, str]] = []
    interrupted: str | None = None
    consecutive_denied = 0

    with GraphClient(s) as client, Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("{task.completed}/{task.total} 則貼文"),
        console=console,
    ) as progress:
        task = progress.add_task("擷取留言…", total=len(posts))
        for post in posts:
            try:
                raw = list(client.list_comments(post.id))
            except GraphAPIError as exc:
                if exc.is_fatal:
                    interrupted = str(exc)
                    break
                if exc.is_skippable_object:
                    # 個別貼文無存取權（實測確有此情形）—— 跳過，不中止整批（規格 §11.3）
                    consecutive_denied += 1
                    if consecutive_denied >= GLOBAL_PERMISSION_THRESHOLD and stats["posts"] == 0:
                        interrupted = (
                            f"連續 {consecutive_denied} 則貼文皆無存取權，"
                            f"研判為全域權限問題而非個別貼文異常：{exc}"
                        )
                        break
                    skipped.append((post.id, f"code={exc.code}"))
                    progress.advance(task)
                    continue
                skipped.append((post.id, str(exc)[:60]))
                progress.advance(task)
                continue
            except KeyboardInterrupt:
                interrupted = "使用者中斷"
                break

            rows = [
                comment_to_row(
                    c, post_id=post.id, fetched_at=datetime.now(UTC).replace(tzinfo=None),
                    salt=s.name_hash_salt, store_plain_name=s.store_plain_name,
                    store_raw_json=s.store_raw_json,
                )
                for c in raw
            ]
            top_level = sum(1 for r in rows if r["parent_id"] is None)
            stats["replies"] += len(rows) - top_level
            stats["hidden"] += sum(1 for r in rows if r["is_hidden"])

            if not _within_tolerance(post.comments_count, top_level):
                mismatches.append((post.id, post.comments_count, top_level))

            if not dry_run:
                # 每則貼文一個交易 → 中斷最多損失一則的進度（規格 §11.1）
                with session_scope(engine) as session:
                    result = repo.upsert_comments(session, rows)
                    repo.mark_post_comments_synced(
                        session, post.id, datetime.now(UTC).replace(tzinfo=None)
                    )
                stats["inserted"] += result.inserted
                stats["updated"] += result.updated
            stats["posts"] += 1
            consecutive_denied = 0
            progress.advance(task)

    if not dry_run and run_id is not None:
        completed_at = datetime.now(UTC).replace(tzinfo=None)
        with session_scope(engine) as session:
            lo, hi = repo.synced_post_range(session)
            repo.complete_sync_run(
                session, run_id, completed_at=completed_at,
                posts_seen=stats["posts"],
                comments_seen=stats["inserted"] + stats["updated"],
            )
            repo.set_sync_run_range(session, run_id, since=lo, until=hi)
            if interrupted:
                repo.annotate_sync_run(
                    session, run_id,
                    f"中斷（{interrupted}）—— 已處理 {stats['posts']}/{len(posts)} 則貼文，"
                    f"涵蓋範圍僅計已完成部分",
                )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("處理貼文", f"{stats['posts']} / {len(posts)} 則")
    table.add_row("留言新增／更新", f"{stats['inserted']} ／ {stats['updated']}")
    table.add_row("其中回覆", f"{stats['replies']} 則")
    table.add_row("其中隱藏", f"{stats['hidden']} 則")
    table.add_row("API 呼叫", f"{client.call_count} 次")
    console.print(table)

    if skipped:
        console.print(f"\n[yellow]跳過 {len(skipped)} 則貼文[/yellow]（無存取權或物件不存在）")
        for pid, reason in skipped[:5]:
            console.print(f"  {pid}  {reason}")
        if len(skipped) > 5:
            console.print(f"  [dim]…另有 {len(skipped) - 5} 則[/dim]")
        console.print("  [dim]這些貼文的留言無法取得，報表將不含其內容。[/dim]")

    if mismatches:
        console.print(f"\n[yellow]對帳差異 {len(mismatches)} 則[/yellow]"
                      f"（容差：±{RECONCILE_ABS_TOLERANCE} 或 {RECONCILE_PCT_TOLERANCE:.0%}）")
        for pid, expected, actual in mismatches[:10]:
            note = "擷取失敗" if actual < 0 else f"FB {expected} → 實抓頂層 {actual}"
            console.print(f"  {pid}  {note}")
        if len(mismatches) > 10:
            console.print(f"  [dim]…另有 {len(mismatches) - 10} 則[/dim]")

    if interrupted:
        err_console.print(f"\n[yellow]中斷[/yellow]：{interrupted}")
        err_console.print("  已完成的貼文已寫入，重跑本指令即可續抓。")
        raise typer.Exit(Exit.PARTIAL)

    console.print("\n[green]完成[/green]。下一步：[bold]smc classify[/bold]（M4／M5）")


@fetch_app.command("all")
def fetch_all(
    since: str = typer.Option(None, "--since"),
    until: str = typer.Option(None, "--until"),
    all_history: bool = typer.Option(False, "--all"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """posts + comments 一次跑完。"""
    _todo("M3", "貼文與留言完整擷取")


# ──────────────────────── classify / report ─────────────────────────
@app.command()
def classify(
    limit: int = typer.Option(None, "--limit", help="僅處理前 N 則"),
    model: str = typer.Option(None, "--model", help="覆寫 CLASSIFY_MODEL"),
    rules_only: bool = typer.Option(False, "--rules-only", help="只跑規則前篩，不呼叫 LLM"),
    skip_llm_on_spam: bool = typer.Option(
        False, "--skip-llm-on-spam", help="規則判定為廣告者不送 LLM"
    ),
    goldset: str = typer.Option(
        None, "--goldset", help="只處理標註集中的留言（供調參後快速量測）"
    ),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    force: bool = typer.Option(False, "--force", help="重新分類已分類者"),
    dry_run: bool = typer.Option(False, "--dry-run", help="僅估算，不寫入"),
) -> None:
    """規則前篩 + LLM 分類（規格 §8）。

    M4 已實作規則前篩；LLM 分類為 M5 範圍，目前以 --rules-only 執行。
    """
    s = get_settings()
    engine_db = make_engine(s.db_path)
    init_schema(engine_db)

    lexicon = Lexicon.load()
    console.print(
        f"[dim]詞表：廣告 {len(lexicon.spam)}、自傷 {len(lexicon.self_harm)}、"
        f"政治 {len(lexicon.political)} 個關鍵詞[/dim]"
    )

    restrict: set[str] | None = None
    if goldset:
        gpath = Path(goldset) if Path(goldset).is_absolute() else PROJECT_ROOT / goldset
        try:
            restrict = set(load_any_goldset(gpath))
        except (FileNotFoundError, ValueError) as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(Exit.CONFIG) from exc
        console.print(f"[dim]限定標註集中的 {len(restrict)} 則留言[/dim]")

    with session_scope(engine_db) as session:
        if force:
            comments = repo.all_comments(session, ids=restrict, limit=limit)
        else:
            comments = repo.unclassified_comments(session, limit=limit, ids=restrict)
        duplicates = repo.duplicate_counts(session)

    if not comments:
        console.print("[green]沒有待分類的留言[/green]")
        raise typer.Exit(Exit.OK)
    if duplicates:
        console.print(f"[dim]偵測到 {len(duplicates)} 組 24 小時內重複的內容[/dim]")

    now = datetime.now(UTC).replace(tzinfo=None)
    counters: Counter[str] = Counter()
    archived = 0
    for_llm: list[Comment] = []
    rule_hits: dict[str, list] = {}
    rule_notes: dict[str, str] = {}
    hash_by_id: dict[str, str] = {}

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None), TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("規則前篩…", total=len(comments))
        for comment in comments:
            result = prefilter(
                comment.message,
                lexicon=lexicon,
                duplicate_count=duplicates.get(comment.message_hash, 0),
                skip_llm_on_spam=skip_llm_on_spam,
            )
            for label in result.labels:
                counters[str(label)] += 1
            for reason in result.force_reasons:
                counters[f"_force_{reason}"] += 1

            # 只有可安全跳過 LLM 者才在此定案；其餘留待 M5
            if result.skip_llm and not dry_run:
                with session_scope(engine_db) as session:
                    repo.upsert_classification(session, {
                        "comment_id": comment.id,
                        "message_hash": comment.message_hash,
                        "labels_json": hits_to_json(result.hits),
                        "max_severity": max_severity(result.hits),
                        "rationale": "；".join(result.evidence) or None,
                        "engine": "rules",
                        "model_version": RULES_VERSION,
                        "classified_at": now,
                    })
                archived += 1
            elif result.skip_llm:
                archived += 1
            else:
                for_llm.append(comment)
                rule_hits[comment.id] = result.hits
                hash_by_id[comment.id] = comment.message_hash
                if result.evidence:
                    rule_notes[comment.id] = "；".join(result.evidence)[:50]
            progress.advance(task)

    table = Table(show_header=True, header_style="bold")
    table.add_column("標籤")
    table.add_column("命中", justify="right")
    for name, count in counters.most_common():
        if name.startswith("_"):
            continue
        label = Label(name)
        table.add_row(f"{CHINESE_NAME[label]}（{name}）", str(count))
    console.print(table)

    console.print(f"\n規則定案（跳過 LLM）：[bold]{archived}[/bold] 則")
    console.print(f"待 LLM 分類：      [bold]{len(for_llm)}[/bold] 則")
    if n := counters.get("_force_self_harm"):
        console.print(
            f"[red]其中 {n} 則命中自傷風險用語[/red]，須優先覆核並提示關懷專線（規格 §5.7）"
        )
    if n := counters.get("_force_duplicate"):
        console.print(f"[yellow]其中 {n} 則為 24 小時內重複內容[/yellow]，待 LLM 判定是否為洗版")

    if rules_only or not for_llm:
        if dry_run:
            console.print("\n[yellow]dry-run[/yellow]：未寫入資料庫。")
        raise typer.Exit(Exit.OK)

    if not s.anthropic_api_key:
        err_console.print("\n[red]ANTHROPIC_API_KEY 未設定[/red]，無法執行 LLM 分類。")
        raise typer.Exit(Exit.CONFIG)

    # ── LLM 分類（規格 §8.3）──
    with session_scope(engine_db) as session:
        posts_text = {p.id: (p.message or "") for p in session.exec(select(Post)).all()}
        parents = {
            c.id: c.message
            for c in session.exec(
                select(Comment).where(Comment.id.in_([x.parent_id for x in for_llm if x.parent_id]))
            ).all()
        }

    inputs = [
        CommentInput(
            id=c.id, message=c.message, post_id=c.post_id,
            parent_message=parents.get(c.parent_id) if c.parent_id else None,
        )
        for c in for_llm
    ]
    prompt = load_prompt()
    batches = list(build_batches(inputs, posts_text, batch_size=s.classify_batch_size))
    model_name = model or s.classify_model
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)

    if dry_run:
        _estimate_cost(client, model_name, batches, prompt, len(for_llm))
        raise typer.Exit(Exit.OK)

    llm_hits: dict[str, list] = {}
    rationales: dict[str, str] = {}
    failed: list[str] = []
    usage = Counter()
    fatal: str | None = None

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None), TextColumn("{task.completed}/{task.total} 批"),
        console=console,
    ) as progress:
        task = progress.add_task(f"LLM 分類（{model_name}）…", total=len(batches))
        for batch in batches:
            try:
                out = classify_batch(client, batch, model=model_name, prompt=prompt)
            except KeyboardInterrupt:
                err_console.print("\n[yellow]使用者中斷[/yellow]，已完成的批次已寫入。")
                break
            except LLMFatalError as exc:
                fatal = str(exc)
                break
            llm_hits.update(out.hits)
            rationales.update(out.rationales)
            failed.extend(out.failed)
            usage["input"] += out.input_tokens
            usage["output"] += out.output_tokens
            usage["cache_read"] += out.cache_read_tokens
            usage["cache_write"] += out.cache_write_tokens

            # 每批一個交易 → 中斷最多損失一批（規格 §11.1）
            with session_scope(engine_db) as session:
                for comment in batch.comments:
                    if comment.id not in out.hits:
                        continue
                    merged = merge_hits(rule_hits.get(comment.id, []), out.hits[comment.id])
                    repo.upsert_classification(session, {
                        "comment_id": comment.id,
                        "message_hash": hash_by_id[comment.id],
                        "labels_json": hits_to_json(merged),
                        "max_severity": max_severity(merged),
                        "rationale": rationales.get(comment.id) or rule_notes.get(comment.id),
                        "engine": f"rules+llm:{model_name}",
                        "model_version": f"{PROMPT_VERSION}/{RULES_VERSION}",
                        "classified_at": datetime.now(UTC).replace(tzinfo=None),
                    })
            progress.advance(task)

    if fatal:
        err_console.print(f"\n[red]分類中止[/red]：{fatal}")
        err_console.print(
            f"  已完成 {len(llm_hits):,}/{len(for_llm):,} 則並寫入資料庫，未完成者不受影響。"
        )
        err_console.print("  排除問題後重跑 [bold]smc classify[/bold] 即可自動續跑。")

    _report_llm_run(engine_db, usage, model_name, len(llm_hits), failed)
    if fatal:
        raise typer.Exit(Exit.CONFIG)


@app.command()
def report(
    since: str = typer.Option(None, "--since", help="留言時間起（規格 §9.1）"),
    until: str = typer.Option(None, "--until", help="留言時間迄"),
    min_severity: int = typer.Option(
        1, "--min-severity",
        help="需關注留言的嚴重度門檻（預設 1；設為 2 只看較嚴重者）",
    ),
    include_hidden: bool = typer.Option(
        False, "--include-hidden", help="納入已隱藏留言（預設排除，因小編已處理）"
    ),
    output: str = typer.Option(None, "--output", help="輸出檔路徑"),
) -> None:
    """產出人工複核報表（規格附錄 B）。--since/--until 作用於**留言時間**。"""
    s = get_settings()
    if not s.db_path.exists():
        err_console.print("[red]資料庫不存在[/red]，請先執行 smc init 與 smc fetch。")
        raise typer.Exit(Exit.CONFIG)

    since_dt = _parse_date(since, flag="--since")
    until_dt = _parse_date(until, flag="--until")
    naive_since = since_dt.replace(tzinfo=None) if since_dt else None
    naive_until = until_dt.replace(tzinfo=None) if until_dt else None

    engine_db = make_engine(s.db_path)
    init_schema(engine_db)

    with session_scope(engine_db) as session:
        focus = repo.report_rows(
            session, since=naive_since, until=naive_until,
            min_severity=min_severity, include_hidden=include_hidden,
        )
        everything = repo.report_rows(
            session, since=naive_since, until=naive_until,
            min_severity=0, include_hidden=True,
        )
        dup_raw = repo.duplicate_groups(session)
        duplicates = [
            (digest, count, posts, sample, repo.links_for_hash(session, digest))
            for digest, count, posts, sample in dup_raw[:200]
        ]
        dup_counts = {digest: count for digest, count, _, _ in dup_raw}
        label_counts = dict(repo.label_histogram(session, since=naive_since, until=naive_until))
        severity_counts = dict(repo.severity_histogram(session))
        top_posts = repo.top_risk_posts(session)
        # FR-14：所查區間若有未擷取的缺口，結果不可信。
        # 未指定 --since 時以最早貼文為下界 —— 用 DATETIME_MIN 會產生
        # 「西元 1 年到粉專成立前從未擷取」這種正確但無意義的警告。
        oldest_post, _ = repo.post_date_range(session)
        gaps = repo.coverage_gaps(
            session, kind="comments",
            since=naive_since or oldest_post or DATETIME_MIN,
            until=naive_until or datetime.now(UTC).replace(tzinfo=None),
        )
        stats = repo.classification_stats(session)

    if gaps:
        err_console.print("[yellow]⚠ 所查區間內有從未擷取的期間，報表可能不完整：[/yellow]")
        for start, end in gaps[:5]:
            err_console.print(f"    {start:%Y-%m-%d} ～ {end:%Y-%m-%d}")
        err_console.print("  請先執行 smc fetch all 補齊該區間。\n")

    meta = {
        "產生時間": datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M"),
        "查詢區間（留言時間）": f"{naive_since or '最早'} ～ {naive_until or '現在'}",
        "嚴重度門檻": str(min_severity),
        "已隱藏留言": "納入" if include_hidden else "排除（小編已處理）",
        "需關注留言": f"{len(focus):,} 則",
        "區間內留言總數": f"{len(everything):,} 則",
        "全庫留言": f"{stats['total']:,} 則",
        "已分類": f"{stats['classified']:,} 則",
        "未分類": f"{stats['pending']:,} 則",
    }

    wb = build_workbook(
        focus=focus, everything=everything, duplicates=duplicates,
        label_counts=label_counts, severity_counts=severity_counts,
        top_posts=top_posts, meta=meta, gaps=gaps, dup_counts=dup_counts,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    path = Path(output) if output else s.reports_dir / f"留言彙整_{stamp}.xlsx"
    saved = save_workbook(wb, path)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("需關注留言", f"{len(focus):,} 則（嚴重度 ≥ {min_severity}）")
    table.add_row("區間內留言", f"{len(everything):,} 則")
    table.add_row("重複內容組數", f"{len(duplicates):,} 組")
    table.add_row("未分類", f"{stats['pending']:,} 則")
    console.print(table)
    console.print(f"\n[green]已產生[/green] {_display_path(saved)}")
    if stats["pending"]:
        console.print(
            f"[yellow]尚有 {stats['pending']:,} 則未分類[/yellow]，"
            "報表僅涵蓋已分類者。請先執行 smc classify。"
        )


# ───────────────────────── review / status ──────────────────────────
@review_app.command("set")
def review_set(
    comment_id: str = typer.Option(..., "--comment-id"),
    status: str = typer.Option(..., "--status", help="pending/confirmed/false_positive/escalated"),
    note: str = typer.Option(None, "--note"),
) -> None:
    """標記人工複核結果（規格 FR-10）。"""
    _todo("M8", "人工複核回寫")


@app.command()
def status() -> None:
    """顯示設定與資料庫狀態。"""
    s = get_settings()
    table = Table(title="smc 狀態", show_header=True, header_style="bold")
    table.add_column("項目")
    table.add_column("值")

    table.add_row("版本", __version__)
    table.add_row("Page ID", s.fb_page_id or "[red]未設定[/red]")
    table.add_row("Page Token", mask(s.fb_page_access_token))
    table.add_row("API 版本", s.fb_api_version)
    table.add_row("分類模型", s.classify_model)
    table.add_row("Anthropic Key", mask(s.anthropic_api_key))
    salt_state = "[green]已設定[/green]" if s.name_hash_salt else "[red]未設定[/red]"
    table.add_row("姓名雜湊 salt", salt_state)
    table.add_row("儲存明碼姓名", str(s.store_plain_name))
    table.add_row("保存期限", f"{s.retention_months} 個月")
    table.add_row("資料庫", _display_path(s.db_path))

    if s.db_path.exists():
        engine = make_engine(s.db_path)
        init_schema(engine)  # 補建後續版本新增的資料表（如 sync_runs）
        with session_scope(engine) as session:
            n_posts = repo.count_posts(session)
            n_comments = repo.count_comments(session)
            oldest, newest = repo.post_date_range(session)
            pending = len(repo.posts_to_fetch_comments(session))
        table.add_row("貼文數", str(n_posts))
        table.add_row("留言數", str(n_comments))
        if oldest and newest:
            table.add_row("貼文區間", f"{oldest:%Y-%m-%d} ～ {newest:%Y-%m-%d}")
        table.add_row("待抓留言貼文", f"{pending} 則")
    else:
        table.add_row("資料庫狀態", "[yellow]尚未建立（執行 smc init）[/yellow]")
    console.print(table)

    if s.db_path.exists():
        _print_coverage(engine)

    if missing := s.missing_for_classify():
        console.print(f"\n[yellow]分類尚缺設定[/yellow]：{', '.join(missing)}")


#: 每百萬 token 單價（USD）。優惠價至 2026-08-31，之後為標準價。
PRICING = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def _estimate_cost(client, model: str, batches: list, prompt: str, n_comments: int) -> None:
    """以 count_tokens 取樣估算費用。count_tokens 端點不計費。"""
    sample = batches[:COST_SAMPLE_BATCHES]
    console.print(f"\n以 {len(sample)}/{len(batches)} 批取樣量測 token（count_tokens 不計費）…")
    counted = [count_batch_tokens(client, model, b, prompt) for b in sample]
    avg_in = sum(counted) / len(counted)

    prompt_tokens = count_batch_tokens(
        client, model, Batch(comments=[], contexts={}), prompt
    )
    variable_in = max(avg_in - prompt_tokens, 0)

    total_in = variable_in * len(batches)
    # 提示詞：首批寫入快取，其餘讀取
    cached = prompt_tokens * CACHE_WRITE_MULTIPLIER + prompt_tokens * CACHE_READ_MULTIPLIER * (
        len(batches) - 1
    )
    est_out = n_comments * 22  # 依實測：僅嚴重度 ≥1 產生理由

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("待分類留言", f"{n_comments:,} 則")
    table.add_row("批次數", f"{len(batches):,} 批（每批 {len(batches[0].comments)} 則）")
    table.add_row("提示詞", f"{prompt_tokens:,} tokens／批（可快取）")
    table.add_row("每批變動輸入", f"{variable_in:,.0f} tokens（取樣平均）")
    table.add_row("輸入合計", f"{(total_in + cached) / 1e6:.2f} M tokens")
    table.add_row("輸出估計", f"{est_out / 1e6:.2f} M tokens")
    console.print(table)

    if (price := PRICING.get(model)) is None:
        console.print(f"[yellow]未知模型 {model} 的單價，略過費用估算[/yellow]")
        return
    pin, pout = price
    cost = (total_in + cached) / 1e6 * pin + est_out / 1e6 * pout
    console.print(f"\n[bold]估算費用：US${cost:.2f}[/bold]（{model}，US${pin}/{pout} 每 M token）")
    console.print("[dim]輸出 token 為推估值；輸入為 count_tokens 實測取樣外推。[/dim]")


def _report_llm_run(engine_db, usage: Counter, model: str, done: int, failed: list[str]) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("完成分類", f"{done:,} 則")
    table.add_row("輸入 tokens", f"{usage['input']:,}")
    table.add_row("輸出 tokens", f"{usage['output']:,}")
    table.add_row("快取寫入／讀取", f"{usage['cache_write']:,} ／ {usage['cache_read']:,}")
    if price := PRICING.get(model):
        pin, pout = price
        billed_in = (
            usage["input"]
            + usage["cache_write"] * CACHE_WRITE_MULTIPLIER
            + usage["cache_read"] * CACHE_READ_MULTIPLIER
        )
        cost = billed_in / 1e6 * pin + usage["output"] / 1e6 * pout
        table.add_row("實際費用", f"US${cost:.2f}")
    console.print(table)
    if failed:
        console.print(f"\n[yellow]{len(failed)} 則分類失敗[/yellow]，可用 --retry-failed 重試")

    with session_scope(engine_db) as session:
        rows = repo.severity_histogram(session)
    if rows:
        sev = Table(title="嚴重度分布", show_header=True, header_style="bold")
        sev.add_column("嚴重度")
        sev.add_column("則數", justify="right")
        names = {0: "0 無需處理", 1: "1 留意", 2: "2 建議人工回應", 3: "3 需主管即刻處理"}
        for level, count in rows:
            sev.add_row(names.get(level, str(level)), f"{count:,}")
        console.print(sev)


def _fmt_bound(value, *, lower: bool) -> str:
    from smc.util.intervals import DATETIME_MAX, DATETIME_MIN

    if value == DATETIME_MIN:
        return "最早"
    if value == DATETIME_MAX:
        return "現在"
    return f"{value:%Y-%m-%d}"


def _print_coverage(engine) -> None:
    """顯示擷取涵蓋範圍與缺口（規格 FR-14、§16 R9）。"""
    with session_scope(engine) as session:
        covered = repo.covered_intervals(session, "posts")
        runs = repo.list_sync_runs(session, kind="posts", limit=5)
        incomplete = [r for r in runs if r.completed_at is None]

    console.print("\n[bold]貼文擷取涵蓋範圍[/bold]")
    if not covered:
        console.print("  [yellow]無紀錄[/yellow] —— 尚未成功執行過 fetch posts，")
        console.print("  或資料為 sync_runs 上線前所擷取。任何查詢結果都不可信。")
    else:
        for start, end in covered:
            lo = _fmt_bound(start, lower=True)
            hi = _fmt_bound(end, lower=False)
            console.print(f"  [green]✓[/green] {lo} ～ {hi}")
        if len(covered) > 1:
            console.print("  [yellow]⚠ 涵蓋範圍不連續[/yellow]，區段之間為從未擷取的缺口：")
            for (_, gap_start), (gap_end, _) in pairwise(covered):
                console.print(f"    [red]✗[/red] {gap_start:%Y-%m-%d} ～ {gap_end:%Y-%m-%d}")

    if incomplete:
        console.print(
            f"  [yellow]⚠ 有 {len(incomplete)} 次擷取未完成[/yellow]"
            "（不計入涵蓋範圍，需重跑該區間）"
        )
        for run in incomplete:
            lo = f"{run.since:%Y-%m-%d}" if run.since else "最早"
            hi = f"{run.until:%Y-%m-%d}" if run.until else "現在"
            console.print(f"    {run.started_at:%Y-%m-%d %H:%M}  {lo} ～ {hi}  {run.note or ''}")


eval_app = typer.Typer(help="分類品質評估（規格 §8.4）", no_args_is_help=True)
app.add_typer(eval_app, name="eval")

#: personal_attack 的召回率門檻（規格 §1.4、§13）
RECALL_THRESHOLD = {Label.PERSONAL_ATTACK: 0.90}


@eval_app.command("sample")
def eval_sample(
    size: int = typer.Option(200, "--size", help="抽樣則數"),
    output: str = typer.Option("data/goldset.xlsx", "--output", help="輸出檔（.xlsx 或 .csv）"),
    force: bool = typer.Option(False, "--force", help="覆寫既有檔案"),
) -> None:
    """抽樣產生待人工標註的表格。

    預設輸出 Excel（附下拉選單與標籤說明）；副檔名為 .csv 時輸出純文字。
    """
    s = get_settings()
    path = Path(output) if Path(output).is_absolute() else PROJECT_ROOT / output
    if path.exists() and not force:
        err_console.print(f"[red]{_display_path(path)} 已存在[/red]，--force 可覆寫。")
        err_console.print("  覆寫會遺失既有人工標註，請先備份。")
        raise typer.Exit(Exit.ERROR)

    engine_db = make_engine(s.db_path)
    init_schema(engine_db)
    # 過抽已隱藏與規則命中者，否則罕見標籤可能一則都抽不到
    hidden_n = size // 3
    flagged_n = size // 3
    with session_scope(engine_db) as session:
        picked = repo.sample_for_goldset(
            session, hidden=hidden_n, flagged=flagged_n, random_n=size - hidden_n - flagged_n
        )
    rows = [
        {
            "comment_id": c.id,
            "message": " ".join((c.message or "").split())[:200],
            "labels": "",
            "note": "已隱藏" if c.is_hidden else "",
        }
        for c in picked[:size]
    ]
    if path.suffix.lower() == ".xlsx":
        write_goldset_xlsx(path, rows)
        console.print(f"[green]已產生[/green] {_display_path(path)}（{len(rows)} 則）")
        console.print("\n用 Excel 開啟，在 [bold]標籤1／2／3[/bold] 欄用下拉選單挑選。")
        console.print("三欄都留空 = 該則無任何標籤（中性）。")
        console.print("標籤定義與最易混淆的界線見「[bold]標籤說明[/bold]」工作表。")
    else:
        write_goldset_template(path, rows)
        console.print(f"[green]已產生[/green] {_display_path(path)}（{len(rows)} 則）")
        console.print("\n請在 [bold]labels[/bold] 欄填入標籤，多個以分號分隔：")
        console.print("  [dim]org_attack;sarcasm[/dim]")
    console.print(f"\n標註完成後執行：[bold]smc eval run --goldset {output}[/bold]")


@eval_app.command("import")
def eval_import(
    source: str = typer.Option(..., "--source", help="既有人工標註的 Excel 檔"),
    sheet: str = typer.Option("留言總表", "--sheet"),
    output: str = typer.Option("data/goldset.xlsx", "--output"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """將既有的人工標註表匯入為 goldset（規格 §8.4）。

    以留言內容比對資料庫取得 comment_id，省去重新標註。
    """
    s = get_settings()
    src = Path(source).expanduser()
    if not src.exists():
        err_console.print(f"[red]找不到檔案[/red]：{src}")
        raise typer.Exit(Exit.CONFIG)
    path = Path(output) if Path(output).is_absolute() else PROJECT_ROOT / output
    if path.exists() and not force:
        err_console.print(f"[red]{_display_path(path)} 已存在[/red]，--force 可覆寫。")
        raise typer.Exit(Exit.ERROR)

    engine_db = make_engine(s.db_path)
    with session_scope(engine_db) as session:
        lookup: dict[str, str] = {}
        for cid, msg in session.exec(select(Comment.id, Comment.message)).all():
            if msg:
                lookup.setdefault(normalize_message(msg), cid)

    try:
        rows, stats = import_manual_annotations(src, lookup=lookup, sheet=sheet)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(Exit.CONFIG) from exc

    write_goldset_xlsx(path, rows)

    table = Table(show_header=False, box=None, padding=(0, 2))
    for key, value in stats.items():
        table.add_row(key, f"{value:,}")
    table.add_row("寫入 goldset", f"{len(rows):,} 則")
    console.print(table)

    labelled = sum(1 for r in rows if r["labels"])
    console.print(f"\n[green]已產生[/green] {_display_path(path)}")
    console.print(f"  其中有標記者 {labelled} 則，其餘為人工判定無問題（負樣本）")
    console.print("\n執行 [bold]smc eval run[/bold] 進行評估")


@eval_app.command("run")
def eval_run(
    goldset: str = typer.Option("data/goldset.xlsx", "--goldset", help=".xlsx 或 .csv"),
) -> None:
    """比對人工標註與系統分類，輸出各標籤 precision／recall／F1。"""
    s = get_settings()
    path = Path(goldset) if Path(goldset).is_absolute() else PROJECT_ROOT / goldset
    try:
        gold = load_any_goldset(path)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(Exit.CONFIG) from exc

    labelled = {cid: labs for cid, labs in gold.items() if labs}
    if not labelled:
        err_console.print("[red]標註集尚無任何標籤[/red]，請先填寫 labels 欄。")
        raise typer.Exit(Exit.CONFIG)

    engine_db = make_engine(s.db_path)
    with session_scope(engine_db) as session:
        raw_pred = repo.predicted_labels(session)
    predicted = {
        cid: {Label(name) for name in names if name not in ("no_text", "classify_error")}
        for cid, names in raw_pred.items()
    }

    evaluated = gold.keys() & predicted.keys()
    if not evaluated:
        err_console.print("[red]標註集中的留言皆尚未分類[/red]，請先執行 smc classify。")
        raise typer.Exit(Exit.ERROR)

    # 以完整 gold（含人工判定無標籤者）計算 ——
    # 若只用有標籤的部分，系統把「人工認為沒問題」的留言誤標時不會被計為誤報，
    # precision 會被系統性高估。
    coarse = is_coarse_goldset(labelled)
    metrics = (
        compute_coarse_metrics(gold, predicted) if coarse
        else compute_metrics(gold, predicted)
    )
    positives = sum(1 for cid in evaluated if gold[cid])
    table = Table(
        title=f"分類品質（{len(evaluated)} 則可比對，其中人工標記為問題者 {positives} 則）",
        header_style="bold",
    )
    table.add_column("標籤")
    for col in ("人工則數", "系統判定", "Precision", "Recall", "F1"):
        table.add_column(col, justify="right")
    for label, m in sorted(metrics.items(), key=lambda kv: -kv[1].support):
        threshold = RECALL_THRESHOLD.get(label)
        recall = f"{m.recall:.2f}"
        if threshold and m.support:
            recall = f"[{'green' if m.recall >= threshold else 'red'}]{recall}[/]"
        table.add_row(
            f"{m.chinese_name}（{label}）", str(m.support), str(m.predicted),
            f"{m.precision:.2f}", recall, f"{m.f1:.2f}",
        )
    console.print(table)
    console.print(f"\nMacro F1：[bold]{macro_f1(metrics):.3f}[/bold]")

    # 實務上最關鍵的指標：不論標籤為何，是否被送到人工面前
    with session_scope(engine_db) as session:
        severities = {
            cid: sev
            for cid, sev in session.exec(
                select(Classification.comment_id, Classification.max_severity)
            ).all()
        }
    det = detection_recall(gold, severities, threshold=2)
    colour = "green" if det.rate >= 0.90 else ("yellow" if det.rate >= 0.80 else "red")
    console.print(
        f"[bold]偵測率[/bold]（人工認定有問題者中，被標為嚴重度 ≥2 而進入報表的比例）："
        f"[{colour}]{det.rate:.0%}[/{colour}]　{det.flagged}/{det.total}"
    )
    if det.missed:
        console.print(f"[dim]完全漏掉 {len(det.missed)} 則：[/dim]")
        with session_scope(engine_db) as session:
            for cid in det.missed[:8]:
                if cm := session.get(Comment, cid):
                    console.print(f"  [red]✗[/red] {cm.message[:40]}")

    if not coarse and (pairs := confusion_pairs(gold, predicted)):
        console.print("\n[bold]最常見的混淆[/bold]（人工標註 → 系統誤判為）")
        for actual, wrong, n in pairs:
            console.print(
                f"  {CHINESE_NAME[actual]} → {CHINESE_NAME[wrong]}　{n} 次"
            )

    failed = [
        (label, m) for label, m in metrics.items()
        if (t := RECALL_THRESHOLD.get(label)) and m.support and m.recall < t
    ]
    if failed:
        console.print()
        for label, m in failed:
            console.print(
                f"[red]✗ {CHINESE_NAME[label]} 召回率 {m.recall:.2f} "
                f"未達門檻 {RECALL_THRESHOLD[label]:.2f}[/red]（規格 §1.4）"
            )
        console.print("  提示詞需調整後重跑，並將結果記入 docs/eval-log.md（規格 §8.4）。")
        raise typer.Exit(Exit.ERROR)

    clean = len(evaluated) - positives
    skipped = len(gold) - len(evaluated)
    console.print(
        f"\n[dim]負樣本（人工判定無問題）{clean} 則已納入計算誤報率。[/dim]"
    )
    if skipped:
        console.print(
            f"[dim]另有 {skipped} 則因系統尚未分類而未納入評估。[/dim]"
        )


@app.command()
def purge(
    before: str = typer.Option(..., "--before", help="刪除此日期前的留言（YYYY-MM-DD）"),
    yes: bool = typer.Option(False, "--yes", help="略過確認"),
) -> None:
    """清除逾保存期限之資料（規格 §5.6）。"""
    _todo("M8", "資料清除")


if __name__ == "__main__":
    app()
