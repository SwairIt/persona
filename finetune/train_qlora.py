"""QLoRA-дообучение «второй копии» под GTX 1050 Ti (4 ГБ, Pascal).

ЧЕСТНО про железо: 1050 Ti = 4 ГБ VRAM, архитектура Pascal (нет bf16, нет
flash-attn). Реально тут дообучается модель 0.5–1.5B в 4-bit (QLoRA). Это
КЛОН СТИЛЯ/характера, не интеллекта. Если 4 ГБ не хватит — учи на бесплатном
Colab T4 (16 ГБ) тем же скриптом, а запускай локально (см. export_gguf.md).

Запуск:
  pip install -r finetune/requirements.txt
  python finetune/train_qlora.py --data finetune/data/persona.jsonl \
      --model Qwen/Qwen2.5-1.5B-Instruct --out finetune/out/persona-lora

OOM? → --model Qwen/Qwen2.5-0.5B-Instruct  и/или --max-seq 512.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows-консоль по умолчанию cp1251 и давится на не-ASCII (напр. «→») при печати
# справки/логов. Переключаем потоки в UTF-8, чтобы --help и вывод не падали.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — старый Python/перенаправление
        pass


def _read_jsonl(path: str):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="QLoRA-дообуч персоны (1050 Ti)")
    ap.add_argument("--data", default="finetune/data/persona.jsonl")
    ap.add_argument("--val", default="")
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct",
                    help="база; на 4ГБ OOM → Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--out", default="finetune/out/persona-lora")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--max-seq", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--lora-r", type=int, default=16)
    args = ap.parse_args()

    # Тяжёлые импорты — внутри main, чтобы модуль/скрипт не требовал torch при
    # простом --help или импорте окружением.
    import torch  # noqa: PLC0415
    from datasets import Dataset  # noqa: PLC0415
    from peft import LoraConfig  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
    )
    from trl import SFTConfig, SFTTrainer  # noqa: PLC0415

    if not torch.cuda.is_available():
        print("⚠ CUDA не найдена. На CPU дообучение будет крайне медленным. "
              "Лучше Colab T4 (бесплатно).")

    # 4-bit NF4 + double-quant, compute fp16 (Pascal не умеет bf16).
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model, quantization_config=bnb, device_map="auto",
        attn_implementation="eager",  # НЕ flash-attn — Pascal не поддерживает
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.05,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    def _fmt(rows):
        # messages → строка через родной chat-template модели
        texts = [tok.apply_chat_template(r["messages"], tokenize=False,
                                         add_generation_prompt=False) for r in rows]
        return Dataset.from_dict({"text": texts})

    train_ds = _fmt(_read_jsonl(args.data))
    eval_ds = _fmt(_read_jsonl(args.val)) if args.val and Path(args.val).exists() else None

    cfg = SFTConfig(
        output_dir=args.out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        fp16=True, bf16=False,
        gradient_checkpointing=True,
        logging_steps=10,
        save_strategy="epoch",
        max_seq_length=args.max_seq,
        optim="paged_adamw_8bit",  # экономит память на Pascal
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        report_to="none",
        dataset_text_field="text",
    )
    trainer = SFTTrainer(
        model=model, args=cfg, train_dataset=train_ds,
        eval_dataset=eval_ds, peft_config=lora,
    )
    print(f"▶ Дообучение {args.model} (QLoRA r={args.lora_r}) на {len(train_ds)} примерах…")
    trainer.train()
    trainer.save_model(args.out)
    tok.save_pretrained(args.out)
    print(f"✓ LoRA-адаптер сохранён в {args.out}. Дальше — finetune/export_gguf.md")


if __name__ == "__main__":
    main()
