"""LLM 分類（規格 §8.3）。

成本設計（依 2026-08-19 實際資料量測）：
  - 依貼文分組批次 —— 貼文上下文的字數與留言本身相當，重複傳送會使成本倍增
  - 提示詞快取 —— 1,502 token 的提示詞若每批重送，580 批就是 87 萬 token
  - 僅在嚴重度 ≥ 1 時要求判斷理由 —— 留言中位數僅 10 字，輸出往往比輸入長，
    而輸出單價是輸入的 5 倍
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

import anthropic
from pydantic import BaseModel

from smc.config import PROJECT_ROOT

from .labels import Label, LabelHit

logger = logging.getLogger(__name__)


class LLMFatalError(Exception):
    """需立即中止整批分類的錯誤。

    與「單批失敗」不同：餘額不足、憑證無效、權限不足這類問題不會因重試而好轉，
    繼續跑只會對剩餘數百批發出注定失敗的請求，並把真正的原因埋在一堆
    「分類失敗」訊息裡。
    """


def _fatal_reason(exc: Exception) -> str | None:
    """判斷是否為需中止的錯誤，是則回傳可讀原因。"""
    if isinstance(exc, anthropic.AuthenticationError):
        return "ANTHROPIC_API_KEY 無效或已撤銷"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "此 API key 無權存取指定模型"
    if isinstance(exc, anthropic.BadRequestError):
        text = str(exc).lower()
        if "credit balance" in text or "insufficient" in text or "billing" in text:
            return "Anthropic 帳戶餘額不足"
    return None

PROMPT_DIR = PROJECT_ROOT / "prompts"
DEFAULT_PROMPT_VERSION = "classify_v3"
#: 附加於提示詞之後的背景知識（規格 §8.3）。由業務人員維護。
CONTEXT_FILE = PROJECT_ROOT / "config" / "context.md"
CONTEXT_LOCAL = PROJECT_ROOT / "config" / "context.local.md"

POST_CONTEXT_CHARS = 200
PARENT_CONTEXT_CHARS = 50
MAX_TOKENS = 8192

#: LLM 可輸出的標籤（不含規則專用的 no_text / classify_error）
LLM_LABELS = tuple(
    str(x) for x in Label if x not in (Label.NO_TEXT, Label.CLASSIFY_ERROR)
)


class _LabelOut(BaseModel):
    label: Literal[LLM_LABELS]  # type: ignore[valid-type]
    severity: int


class _ResultOut(BaseModel):
    index: int
    labels: list[_LabelOut]
    rationale: str


class _BatchOut(BaseModel):
    results: list[_ResultOut]


@dataclass
class CommentInput:
    id: str
    message: str
    post_id: str
    parent_message: str | None = None


@dataclass
class Batch:
    comments: list[CommentInput]
    #: post_id → 貼文內文（截斷）
    contexts: dict[str, str]

    def render(self) -> str:
        lines: list[str] = ["# 貼文脈絡", ""]
        labels = {pid: chr(ord("A") + i) for i, pid in enumerate(self.contexts)}
        for pid, text in self.contexts.items():
            body = " ".join(text.split())[:POST_CONTEXT_CHARS] or "（無文字，圖片或影片貼文）"
            lines.append(f"[貼文 {labels[pid]}] {body}")
        lines += ["", "# 待判讀留言", ""]
        for n, c in enumerate(self.comments, 1):
            tag = f"（貼文 {labels[c.post_id]}）"
            if c.parent_message:
                parent = " ".join(c.parent_message.split())[:PARENT_CONTEXT_CHARS]
                tag += f"（此為回覆，針對：「{parent}」）"
            lines.append(f"{n}. {tag} {c.message}")
        return "\n".join(lines)


@dataclass
class BatchResult:
    hits: dict[str, list[LabelHit]] = field(default_factory=dict)
    rationales: dict[str, str] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def load_prompt(version: str = DEFAULT_PROMPT_VERSION) -> str:
    """載入提示詞，並附加可由業務人員維護的判讀背景。

    背景知識獨立成檔而非寫死在提示詞裡 —— 爭議事件會隨時間變動，
    不應每次更新都要改動經過評估驗證的提示詞本體。
    """
    path = PROMPT_DIR / f"{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"找不到提示詞 {path}")
    prompt = path.read_text(encoding="utf-8")
    source = CONTEXT_LOCAL if CONTEXT_LOCAL.exists() else CONTEXT_FILE
    if source.exists():
        body = source.read_text(encoding="utf-8")
        # 只取分隔線之後的正文，跳過給維護者看的說明
        if "\n---\n" in body:
            body = body.split("\n---\n", 1)[1]
        if body.strip():
            prompt = f"{prompt}\n\n# 判讀背景\n{body.strip()}"
    return prompt


def build_batches(
    comments: list[CommentInput], posts: dict[str, str], *, batch_size: int = 20
) -> Iterator[Batch]:
    """依貼文分組後切批，讓同一則貼文的上下文只需傳送一次。"""
    ordered = sorted(comments, key=lambda c: (c.post_id, c.id))
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        yield Batch(
            comments=chunk,
            contexts={c.post_id: posts.get(c.post_id, "") for c in chunk},
        )


def _system_blocks(prompt: str) -> list[dict]:
    """提示詞固定不變，設為快取前綴（規格 §8.3）。"""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def count_batch_tokens(client: anthropic.Anthropic, model: str, batch: Batch, prompt: str) -> int:
    """以 count_tokens 取得精確輸入 token 數。此端點不計費。"""
    return client.messages.count_tokens(
        model=model,
        system=_system_blocks(prompt),
        messages=[{"role": "user", "content": batch.render()}],
    ).input_tokens


def classify_batch(
    client: anthropic.Anthropic,
    batch: Batch,
    *,
    model: str,
    prompt: str,
    effort: str = "low",
) -> BatchResult:
    """分類一批留言。解析失敗時重試一次，仍失敗則標記為錯誤。"""
    result = BatchResult()
    for attempt in (1, 2):
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "disabled"},
                output_config={"effort": effort},
                system=_system_blocks(prompt),
                messages=[{"role": "user", "content": batch.render()}],
                output_format=_BatchOut,
            )
        except anthropic.APIStatusError as exc:
            if reason := _fatal_reason(exc):
                raise LLMFatalError(reason) from exc
            logger.warning("批次分類失敗（第 %d 次）：%s", attempt, exc)
            if attempt == 2:
                result.failed = [c.id for c in batch.comments]
                return result
            continue

        usage = response.usage
        result.input_tokens += usage.input_tokens
        result.output_tokens += usage.output_tokens
        result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        result.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        parsed = response.parsed_output
        if parsed is None:
            logger.warning("批次結構化輸出解析失敗（第 %d 次）", attempt)
            if attempt == 2:
                result.failed = [c.id for c in batch.comments]
            continue

        by_index = {r.index: r for r in parsed.results}
        for n, comment in enumerate(batch.comments, 1):
            item = by_index.get(n)
            if item is None:
                result.failed.append(comment.id)
                continue
            hits: list[LabelHit] = []
            for raw in item.labels:
                try:
                    hits.append(LabelHit(Label(raw.label), raw.severity))
                except ValueError:
                    # 嚴重度超出該標籤的宣告範圍：夾回合法區間而非整批作廢
                    from .labels import SEVERITY_RANGE

                    label = Label(raw.label)
                    lo, hi = SEVERITY_RANGE[label]
                    hits.append(LabelHit(label, min(max(raw.severity, lo), hi)))
            result.hits[comment.id] = hits
            if item.rationale.strip():
                result.rationales[comment.id] = item.rationale.strip()[:50]
        return result
    return result
