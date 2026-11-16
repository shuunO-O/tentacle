"""Explicit client proxies must not modify the process environment or other clients."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.protocol import ModelConfig
from config.manager import ConfigManager


@pytest.mark.parametrize('proxy', [None, '', 'http://windows.test:7897'])
def test_gemini_proxy_environment_policy(monkeypatch, proxy):
    import httpx
    from dialects.gemini import GeminiClient

    captured = {}
    monkeypatch.setattr(httpx.AsyncClient, '__init__', lambda self, **kw: captured.update(kw))
    before = dict(os.environ)
    GeminiClient('test', 'https://gemini.test/v1beta', proxy=proxy)
    assert captured['proxy'] == (proxy or None)
    assert captured['trust_env'] is (proxy is None)
    assert dict(os.environ) == before


@pytest.mark.parametrize('api_style', ['chat', 'responses', 'interactions', 'gemini_generate'])
@pytest.mark.parametrize('proxy', ['', 'http://windows.test:7897'])
def test_agent_model_proxy_routing(monkeypatch, api_style, proxy):
    import agent.loop as agent_loop

    captured = {}
    http_client = SimpleNamespace()
    def http_factory(**kw):
        captured['http'] = kw
        return http_client
    def model_factory(**kw):
        captured['model'] = kw
        return SimpleNamespace()
    monkeypatch.setattr(agent_loop, 'DefaultAsyncHttpxClient', http_factory)
    monkeypatch.setattr(agent_loop, 'AsyncOpenAI', model_factory)
    monkeypatch.setattr(agent_loop, 'GeminiClient', model_factory)
    cfg = ModelConfig('test', 'https://model.test', 'test', api_style=api_style, proxy=proxy)
    agent_loop.AgentLoop(cfg, AsyncMock())
    if api_style in ('interactions', 'gemini_generate'):
        assert captured['model']['proxy'] == proxy
        assert 'http' not in captured
    else:
        assert captured['http'] == {'proxy': proxy or None, 'trust_env': False}
        assert captured['model']['http_client'] is http_client


def test_config_loads_per_model_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigManager, '_instance', None)
    monkeypatch.setenv('TEST_PROXY_KEY', 'test')
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('''config_version: 2
providers:
  p_google:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: TEST_PROXY_KEY
    proxy: http://windows.test:7897
  p_local:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: TEST_PROXY_KEY
    proxy: ""
  p_default:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: TEST_PROXY_KEY
models:
  m_google: {provider: p_google, name: test}
  m_local: {provider: p_local, name: test}
  m_default: {provider: p_default, name: test}
roles:
  google: {model: m_google}
  local: {model: m_local}
  default: {model: m_default}
''')
    manager = ConfigManager(str(cfg))
    assert manager.get_model('google').proxy == 'http://windows.test:7897'
    assert manager.get_model('local').proxy == ''
    assert manager.get_model('default').proxy is None


def test_config_loads_adc_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigManager, '_instance', None)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'welcome-credit-project')
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('''config_version: 2
providers:
  gcp:
    type: google_vertex
    base_url: https://aiplatform.googleapis.com/v1beta1
    auth: adc
    location: global
models:
  gemini:
    provider: gcp
    name: gemini-test
    api_style: interactions
roles:
  cloud:
    model: gemini
''')
    model = ConfigManager(str(cfg)).get_model('cloud')
    assert model.api_key == ''
    assert model.project == 'welcome-credit-project'
    assert model.location == 'global'
