"""app.finetune — генерация персонального датасета для «второй копии».

Лёгкий модуль БЕЗ тяжёлых зависимостей (torch/peft импортируются только в
скриптах обучения в каталоге ``finetune/``, не здесь). Отвечает за сборку
датасета в формате messages-JSONL (ShareGPT-совместимый) для дообучения
маленькой локальной модели в твоём тоне.

Честно: дообучение на GTX 1050 Ti (4 ГБ) реально для модели 0.5–1.5B и даёт
КЛОН СТИЛЯ/ХАРАКТЕРА, а не клон интеллекта. См. finetune/README.md.
"""

from app.finetune.dataset import (
    build_dataset,
    persona_system,
    real_pairs_from_history,
    write_jsonl,
)

__all__ = ["build_dataset", "persona_system", "real_pairs_from_history", "write_jsonl"]
