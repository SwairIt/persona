"""CLI: собрать персональный датасет для «второй копии» → JSONL.

Примеры:
  # только синтетика (стиль), 400 примеров:
  python scripts/build_persona_dataset.py --out finetune/data/persona.jsonl --size 400

  # + реальные пары из твоей истории чатов (персонально под тебя):
  python scripts/build_persona_dataset.py --out finetune/data/persona.jsonl \
      --size 300 --include-history --user-id 2

Выход — два файла: <out> (train) и <out>.val.jsonl (валидация).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Windows-консоль по умолчанию cp1251 — без этого финальный print с «✓» и
# кириллицей падает UnicodeEncodeError уже ПОСЛЕ записи датасета. Переключаем
# stdout/stderr на UTF-8, чтобы команда из README отрабатывала чисто.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — старый Python/перенаправление — не критично
        pass


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Сборка персонального датасета Persona")
    ap.add_argument("--out", default="finetune/data/persona.jsonl", help="путь train JSONL")
    ap.add_argument("--size", type=int, default=300, help="сколько синтетических примеров")
    ap.add_argument("--include-history", action="store_true", help="добавить реальные пары из чатов")
    ap.add_argument("--user-id", type=int, default=0, help="id пользователя для истории/фактов")
    ap.add_argument("--val-split", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    from app.finetune.dataset import build_dataset, real_pairs_from_history, write_jsonl

    real: list = []
    facts: list[str] = []
    if args.include_history and args.user_id:
        real = await real_pairs_from_history(args.user_id)
        print(f"Реальных пар из истории: {len(real)}")
        try:
            from app.chat.user_memory import list_memory  # noqa: PLC0415

            facts = [m["text"] for m in await list_memory(args.user_id, limit=40)]
            print(f"Фактов из памяти: {len(facts)}")
        except Exception as exc:  # noqa: BLE001
            print(f"(память недоступна: {exc})")

    ds = build_dataset(
        real_pairs=real, facts=facts,
        target_synthetic=args.size, val_split=args.val_split, seed=args.seed,
    )
    out = Path(args.out)
    n_train = write_jsonl(ds["train"], out)
    n_val = write_jsonl(ds["val"], out.with_suffix(".val.jsonl"))
    print(f"✓ train: {n_train} → {out}")
    print(f"✓ val:   {n_val} → {out.with_suffix('.val.jsonl')}")


if __name__ == "__main__":
    asyncio.run(_main())
