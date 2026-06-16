"""Артефакты «второй копии»: валидность Colab-ноутбука + рендер кнопок скачивания."""

from __future__ import annotations

import json

from app.web.routes.integrations_settings import _DOWNLOADS, _REPO_ROOT
from app.web.templates_engine import templates


def test_colab_notebook_is_valid_ipynb() -> None:
    path = _REPO_ROOT / "finetune/persona_colab.ipynb"
    assert path.exists(), "ноутбук должен быть закоммичен"
    nb = json.loads(path.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) >= 10
    # GPU-метаданные + ключевые шаги присутствуют
    assert nb["metadata"].get("accelerator") == "GPU"
    blob = json.dumps(nb, ensure_ascii=False)
    assert "files.upload" in blob          # загрузка датасета
    assert "SFTTrainer" in blob            # обучение
    assert "convert_hf_to_gguf" in blob    # экспорт GGUF
    assert "ollama create" in blob         # запуск на ПК


def test_downloads_mapping_known_keys() -> None:
    assert set(_DOWNLOADS) == {"dataset", "dataset-val", "colab"}
    # ноутбук всегда в репозитории
    rel = _DOWNLOADS["colab"][0]
    assert (_REPO_ROOT / rel).exists()


def _render(dataset_ready: bool) -> str:
    t = templates.env.get_template("integrations_settings.html")
    return t.render(
        request=None, app_version="test", title="t", active_nav="settings",
        counts={"active_reminders": 0}, imported=None, parsed=None,
        dataset_ready=dataset_ready, lang="ru", t=lambda *a, **k: (a[0] if a else ""),
    )


def test_page_shows_download_buttons_when_ready() -> None:
    html = _render(True)
    assert "/settings/integrations/download/colab" in html
    assert "/settings/integrations/download/dataset" in html
    assert "Вторая копия" in html


def test_page_shows_build_hint_when_not_ready() -> None:
    html = _render(False)
    assert "build_persona_dataset.py" in html  # подсказка собрать
    assert "/settings/integrations/download/colab" in html  # ноутбук всё равно доступен
