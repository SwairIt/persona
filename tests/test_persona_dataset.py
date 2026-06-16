"""Сборка персонального датасета «второй копии» (ROADMAP «вторая копия»).

Чистые тесты пайплайна (без БД): формат chat-messages, дедуп, train/val сплит,
запись JSONL. Реальный прогон делается на живой БД скриптом build_persona_dataset.
"""

from __future__ import annotations

import json

from app.finetune.dataset import build_dataset, write_jsonl


def test_identity_baked_into_every_example() -> None:
    ds = build_dataset(real_pairs=[("q", "a")], facts=[], target_synthetic=30,
                       owner_name="Ярослав", seed=1)
    rows = ds["train"] + ds["val"]
    # идентичность (имя Persona + авторство + имя владельца) в КАЖДОМ примере
    for e in rows:
        sysmsg = e["messages"][0]["content"]
        assert "Тебя зовут Persona" in sysmsg
        assert "Твой автор и хозяин" in sysmsg
        assert "Его зовут Ярослав" in sysmsg
        assert "Тебя зовут Ярослав" not in sysmsg  # модель ≠ владелец
    # есть обучающие Q&A про имя/автора/принадлежность
    users = [e["messages"][1]["content"] for e in rows]
    assert "кто тебя создал" in users
    assert "как меня зовут" in users


def test_build_dataset_chat_format() -> None:
    ds = build_dataset(real_pairs=[("привет", "здаров")], facts=["любит кофе"],
                       target_synthetic=20, val_split=0.1, seed=1)
    assert ds["train"] and ds["val"]
    ex = ds["train"][0]
    assert "messages" in ex
    roles = [m["role"] for m in ex["messages"]]
    assert roles == ["system", "user", "assistant"]
    # факт подмешан в системный промпт персоны
    assert any("кофе" in m["content"] for m in ex["messages"] if m["role"] == "system")


def test_build_dataset_dedup() -> None:
    dup = [("один и тот же", "ответ")] * 5
    ds = build_dataset(real_pairs=dup, facts=[], target_synthetic=0, val_split=0.0, seed=1)
    rows = ds["train"] + ds["val"]  # одна строка может уйти в val (max(1,...))
    pairs = [(e["messages"][1]["content"], e["messages"][2]["content"]) for e in rows]
    assert len(pairs) == 1  # дубли схлопнуты


def test_write_jsonl_roundtrip(tmp_path) -> None:
    ds = build_dataset(real_pairs=[("q1", "a1")], facts=[], target_synthetic=10, seed=3)
    out = tmp_path / "persona.jsonl"
    n = write_jsonl(ds["train"], out)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert n == len(lines)
    for ln in lines:
        obj = json.loads(ln)  # каждая строка — валидный JSON
        assert obj["messages"][0]["role"] == "system"
