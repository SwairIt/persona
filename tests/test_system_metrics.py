"""Тесты КОНТРАКТА снимка метрик ПК (`collect_system_metrics`).

Главное, что проверяем: снимок содержит ПЛОСКИЕ ключи, которые читают
потребители (`health_dashboard`, `metrics_extended`) — без них плитка
«Система» и Prometheus-гейджи показывают 0/None. Тест не должен падать
даже без psutil (снимок всё равно обязан содержать эти ключи).
"""

from __future__ import annotations

from app.system_metrics import collect_system_metrics


def test_snapshot_has_flat_contract_keys():
    """Снимок содержит плоские поля для дашборда/Prometheus.

    `disk_usage_pct` — float, `memory_percent` — float (или None, если
    нет psutil, но ключ ОБЯЗАН присутствовать), `top_consumer` — str.
    """
    snap = collect_system_metrics()

    # Все три плоских ключа обязаны присутствовать всегда (даже без psutil).
    assert "disk_usage_pct" in snap
    assert "memory_percent" in snap
    assert "top_consumer" in snap

    # disk_usage_pct — всегда число с плавающей точкой.
    assert isinstance(snap["disk_usage_pct"], float)

    # memory_percent — float; None допустим только если psutil недоступен,
    # но ключ всё равно есть (проверено выше).
    assert snap["memory_percent"] is None or isinstance(
        snap["memory_percent"], float
    )

    # top_consumer — строка ('name (NN%)' либо '' при отсутствии данных).
    assert isinstance(snap["top_consumer"], str)


def test_flat_disk_usage_pct_matches_nested():
    """`disk_usage_pct` = максимум `percent` по вложенному `disk_usage`."""
    snap = collect_system_metrics()
    disk_usage = snap.get("disk_usage") or []
    if disk_usage:
        expected = max(float(d.get("percent") or 0.0) for d in disk_usage)
        assert snap["disk_usage_pct"] == expected
    else:
        # Нет разделов (например, без psutil) → 0.0
        assert snap["disk_usage_pct"] == 0.0


def test_flat_memory_percent_mirrors_nested():
    """`memory_percent` зеркалит вложенное `memory['percent']`."""
    snap = collect_system_metrics()
    nested = snap.get("memory", {}).get("percent")
    # Без psutil оба нули; с psutil — совпадают по значению.
    assert snap["memory_percent"] == float(nested or 0.0)
