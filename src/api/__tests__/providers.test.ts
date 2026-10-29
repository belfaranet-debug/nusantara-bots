/**
 * Tests for the BYO provider (providers.ts) — BYOK-only version.
 * No built-in model presets: the only provider is Custom.
 * Pure data + lookup helpers — no native modules, no network.
 */
import { PROVIDERS, getModelsForProvider, getProvider, inferProviderId } from '../providers';

describe('PROVIDERS (BYOK saja)', () => {
  it('only lists the Custom provider', () => {
    expect(PROVIDERS).toHaveLength(1);
    expect(PROVIDERS[0].id).toBe('custom');
    expect(PROVIDERS[PROVIDERS.length - 1].id).toBe('custom');
  });

  it('Custom provider has an empty base URL, model list and default', () => {
    const custom = getProvider('custom');
    expect(custom.name).toBe('Custom (OpenAI-compatible)');
    expect(custom.baseUrl).toBe('');
    expect(custom.models).toEqual([]);
    expect(custom.defaultModel).toBe('');
    expect(getModelsForProvider('custom')).toEqual([]);
  });

  it('getProvider falls back to Custom for unknown ids', () => {
    expect(getProvider('nope').id).toBe('custom');
    expect(getProvider(undefined).id).toBe('custom');
    expect(getProvider(null).id).toBe('custom');
  });

  it('inferProviderId always returns custom (no presets to match)', () => {
    expect(inferProviderId('https://api.openai.com/v1')).toBe('custom');
    expect(inferProviderId('https://api.contoh.com/v1')).toBe('custom');
    expect(inferProviderId('')).toBe('custom');
    expect(inferProviderId(undefined)).toBe('custom');
  });
});
