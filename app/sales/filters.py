"""Фильтр-движок отдела продаж.

Критерий отбора по ТЗ (условие «ИЛИ» без дублей):
    (ЕНСТРУ-код лота/пункта в целевом списке)  ИЛИ  (ключевое слово в наименовании),
    затем отсечение: наименование содержит стоп-слово  ->  мимо;
                     сумма меньше порога             ->  мимо.

Дедупликация по уникальному ID — на стороне вызывающего (один лот, совпавший и по
коду, и по слову, обрабатывается один раз). На goszakup РК «КТРУ» из ТЗ — это
ЕНСТРУ (refEnstruCode), другого классификатора у площадки нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _norm(s) -> str:
    return (str(s) if s is not None else "").strip().lower()


@dataclass
class FilterConfig:
    """Разобранная вкладка «Настройки_Фильтров»."""

    enstru: set[str] = field(default_factory=set)        # целевые коды ЕНСТРУ (как строки)
    keywords: list[str] = field(default_factory=list)    # ключевые слова, нижний регистр
    stop_words: list[str] = field(default_factory=list)  # стоп-слова, нижний регистр
    min_sum: float = 0.0                                 # минимальная плановая сумма, тг
    managers: list[dict] = field(default_factory=list)   # [{name, username, user_id}]

    @classmethod
    def from_columns(
        cls,
        ktru_col=None,
        keywords_col=None,
        stop_col=None,
        min_sum=0,
        managers_col=None,
    ) -> "FilterConfig":
        """Собрать конфиг из «сырых» колонок вкладки (списки значений ячеек).

        Коды ЕНСТРУ держим строками: ведущие нули значимы. Пустые ячейки пропускаем.
        """
        enstru = {_norm(c) for c in (ktru_col or []) if _norm(c)}
        keywords = [_norm(c) for c in (keywords_col or []) if _norm(c)]
        stop_words = [_norm(c) for c in (stop_col or []) if _norm(c)]
        try:
            min_val = float(str(min_sum).replace(" ", "").replace(",", ".")) if min_sum else 0.0
        except (TypeError, ValueError):
            min_val = 0.0
        return cls(
            enstru=enstru,
            keywords=keywords,
            stop_words=stop_words,
            min_sum=min_val,
            managers=parse_managers(managers_col or []),
        )

    def passes(self, name: str, codes, amount) -> bool:
        """Проходит ли запись фильтр. `codes` — коды ЕНСТРУ записи (любая коллекция)."""
        if self.min_sum:
            if amount is None:
                return False
            try:
                if float(amount) < self.min_sum:
                    return False
            except (TypeError, ValueError):
                return False
        n = _norm(name)
        if any(sw in n for sw in self.stop_words):
            return False
        code_hit = bool({_norm(c) for c in (codes or [])} & self.enstru)
        kw_hit = any(kw in n for kw in self.keywords)
        return code_hit or kw_hit


def parse_managers(cells) -> list[dict]:
    """Разобрать колонку менеджеров. Формат ячейки: ``Имя | @username | telegram_user_id``.

    Недостающие части допустимы. Пустые строки пропускаем.
    """
    out: list[dict] = []
    for cell in cells:
        parts = [p.strip() for p in str(cell or "").split("|")]
        if not parts or not parts[0]:
            continue
        name = parts[0]
        username = parts[1].lstrip("@") if len(parts) > 1 else ""
        user_id = parts[2] if len(parts) > 2 else ""
        out.append({"name": name, "username": username, "user_id": user_id})
    return out
