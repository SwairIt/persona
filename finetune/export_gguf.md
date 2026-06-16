# Экспорт «второй копии» в GGUF и подключение в Persona (Ollama)

После `train_qlora.py` у тебя есть LoRA-адаптер в `finetune/out/persona-lora`.
Чтобы запускать локально на 1050 Ti через Ollama — сливаем LoRA в базу,
конвертируем в GGUF Q4_K_M и заводим Ollama-модель.

## 1. Слить LoRA в базовую модель

```python
# finetune/merge.py (запусти разово)
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer
m = AutoPeftModelForCausalLM.from_pretrained("finetune/out/persona-lora")
m = m.merge_and_unload()
m.save_pretrained("finetune/out/persona-merged")
AutoTokenizer.from_pretrained("finetune/out/persona-lora").save_pretrained("finetune/out/persona-merged")
```

## 2. Конвертировать в GGUF + квантовать (llama.cpp)

```bash
git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp
pip install -r requirements.txt
python convert_hf_to_gguf.py ../finetune/out/persona-merged --outfile persona-f16.gguf
# квант Q4_K_M — комфортно влезает в 4 ГБ 1050 Ti:
./llama-quantize persona-f16.gguf persona-q4.gguf Q4_K_M
```

## 3. Завести модель в Ollama

`finetune/Modelfile`:
```
FROM ./persona-q4.gguf
PARAMETER temperature 0.7
PARAMETER num_ctx 4096
SYSTEM """Ты — Persona, личный ИИ-друг этого человека. Тёплый, прямой, на «ты», на его стороне, по-русски. (тот же характер, что в обучении.)"""
```

```bash
ollama create persona-mini -f finetune/Modelfile
ollama run persona-mini   # проверь живьём
```

## 4. Подключить в Persona

В `/settings/llm` выбери провайдер **ollama**, модель `persona-mini`. Готово —
в чате появится индикатор нагрузки 🖥 (это локальная модель), а ответы пойдут
твоим тоном с твоего железа, без облака.

> Реалистично: `persona-mini` (0.5–1.5B) хорош в ТОНЕ и простом общении. Для
> сложных задач/инструментов держи параллельно мощную модель (облако или
> большую локальную) — переключай в один клик в `/settings/llm`.
