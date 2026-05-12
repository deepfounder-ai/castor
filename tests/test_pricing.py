"""Unit tests for the pricing module (cost tracking)."""
import pytest


def test_bundled_fallback_has_gpt4o_mini():
    import pricing
    assert "gpt-4o-mini" in pricing._BUNDLED_FALLBACK
    assert pricing._BUNDLED_FALLBACK["gpt-4o-mini"]["input"] > 0


def test_local_provider_zero_cost(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("lmstudio:llama-3", "input") == 0.0
    assert pricing.get_price("ollama:qwen2.5", "output") == 0.0
    assert pricing.get_price("local:any-model", "input") == 0.0


def test_compute_cost_local_zero(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("ollama:llama-3", 1000, 500) == 0.0


def test_get_price_unknown_model_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.get_price("totally-fake-model-9000", "input") is None


def test_compute_cost_unknown_returns_none(qwe_temp_data_dir):
    import pricing
    assert pricing.compute_cost("totally-fake-model-9000", 1000, 500) is None
