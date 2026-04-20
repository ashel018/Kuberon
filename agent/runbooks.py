from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunbookMatch:
    title: str
    path: str
    excerpt: str
    score: int


class RunbookLibrary:
    def __init__(self, runbook_dir: str | Path) -> None:
        self.runbook_dir = Path(runbook_dir)
        self._synonyms = {
            "oom": {"memory", "oomkill", "crashloop", "restart"},
            "memory": {"oom", "usage", "limit", "working", "set"},
            "image": {"imagepull", "registry", "tag", "manifest"},
            "service": {"endpoint", "selector", "traffic"},
            "cpu": {"resource", "unschedulable", "throttle"},
            "pending": {"unschedulable", "node", "scheduler"},
        }

    def search(self, query: str, limit: int = 3) -> list[RunbookMatch]:
        tokens = self._expand_tokens(query)
        query_weights = self._term_weights(tokens)

        matches: list[RunbookMatch] = []
        for path in sorted(self.runbook_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            sections = self._chunk_content(path.stem, content)
            best_excerpt = ""
            best_score = 0.0
            for section in sections:
                section_tokens = self._tokenize(section)
                if not section_tokens:
                    continue
                score = self._cosine_like_score(query_weights, self._term_weights(section_tokens))
                if score > best_score:
                    best_score = score
                    best_excerpt = section[:260]
            score = int(best_score * 100)
            if score == 0:
                continue
            matches.append(
                RunbookMatch(
                    title=path.stem.replace("_", " ").title(),
                    path=str(path),
                    excerpt=best_excerpt or self._build_excerpt(content, tokens),
                    score=score,
                )
            )

        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:limit]

    @staticmethod
    def _build_excerpt(content: str, tokens: set[str]) -> str:
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        for line in lines:
            lowered = line.lower()
            if any(token in lowered for token in tokens):
                return line[:260]
        return lines[0][:260] if lines else ""

    def _expand_tokens(self, query: str) -> set[str]:
        base_tokens = self._tokenize(query)
        expanded = set(base_tokens)
        for token in list(base_tokens):
            expanded.update(self._synonyms.get(token, set()))
        return {token for token in expanded if len(token) > 2}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2]

    @staticmethod
    def _term_weights(tokens: set[str] | list[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        if isinstance(tokens, set):
            iterable = list(tokens)
        else:
            iterable = tokens
        for token in iterable:
            counts[token] = counts.get(token, 0) + 1
        if not counts:
            return {}
        max_count = max(counts.values())
        return {token: 0.5 + 0.5 * (count / max_count) for token, count in counts.items()}

    @staticmethod
    def _cosine_like_score(left: dict[str, float], right: dict[str, float]) -> float:
        if not left or not right:
            return 0.0
        shared = set(left) & set(right)
        numerator = sum(left[token] * right[token] for token in shared)
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return numerator / (left_norm * right_norm)

    @staticmethod
    def _chunk_content(stem: str, content: str) -> list[str]:
        sections: list[str] = []
        current: list[str] = [stem.replace("_", " ")]
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and current:
                sections.append("\n".join(current).strip())
                current = [stripped.lstrip("# ").strip()]
                continue
            current.append(stripped)
        if current:
            sections.append("\n".join(current).strip())
        return [section for section in sections if section]
