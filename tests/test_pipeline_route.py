"""Tests de app.api.routes.pipeline — status/enable/disable, sin red."""

from app.api.routes import pipeline


async def test_status_refleja_is_enabled(monkeypatch):
    monkeypatch.setattr(pipeline.pipeline_state, "is_enabled", lambda: True)
    assert await pipeline.pipeline_status() == {"enabled": True}

    monkeypatch.setattr(pipeline.pipeline_state, "is_enabled", lambda: False)
    assert await pipeline.pipeline_status() == {"enabled": False}


async def test_enable_llama_a_pipeline_state(monkeypatch):
    llamado = []
    monkeypatch.setattr(pipeline.pipeline_state, "enable", lambda: llamado.append(True))

    resultado = await pipeline.pipeline_enable()

    assert llamado == [True]
    assert resultado == {"enabled": True}


async def test_disable_llama_a_pipeline_state(monkeypatch):
    llamado = []
    monkeypatch.setattr(pipeline.pipeline_state, "disable", lambda: llamado.append(True))

    resultado = await pipeline.pipeline_disable()

    assert llamado == [True]
    assert resultado == {"enabled": False}
