"""Загрузка постоянной стратегии для всех AI-агентов Mr SEO."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_FILE = ROOT / "carpathy" / "strategy_brain.md"


def strategy_context() -> str:
    try:
        return STRATEGY_FILE.read_text(encoding="utf-8")
    except OSError:
        return "Стратегия недоступна: не менять архитектуру и не создавать новые URL."
