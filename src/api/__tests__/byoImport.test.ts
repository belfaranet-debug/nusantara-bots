/**
 * Tests for the one-time BYO import (byoImport.ts).
 *
 * The file system and the save step are injected as fakes — no
 * native code, no real files. No credential ever leaves the test.
 * The real 'expo-file-system/legacy' and '../byo' modules are
 * mocked only so importing the module under test is safe in Node.
 */
import {
  BYO_IMPORT_FILENAME,
  parseByoImport,
  tryImportByoConfig,
  type ByoImportFs,
} from '../byoImport';
import type { ByoConfig } from '../types';

jest.mock(
  'expo-file-system/legacy',
  () => ({
    documentDirectory: 'file:///mock-docs/',
    getInfoAsync: jest.fn(),
    readAsStringAsync: jest.fn(),
    deleteAsync: jest.fn(),
  }),
  { virtual: true },
);
jest.mock('../byo', () => ({ saveByo: jest.fn() }));

type FakeFs = ByoImportFs & {
  getInfoAsync: jest.Mock;
  readAsStringAsync: jest.Mock;
  deleteAsync: jest.Mock;
};

function makeFs(overrides: Partial<FakeFs> = {}): FakeFs {
  return {
    documentDirectory: 'file:///mock-docs/',
    getInfoAsync: jest.fn(async () => ({ exists: false })),
    readAsStringAsync: jest.fn(async () => ''),
    deleteAsync: jest.fn(async () => undefined),
    ...overrides,
  };
}

const VALID_JSON = JSON.stringify({
  baseUrl: 'https://api.contoh.com/v1',
  model: 'model-contoh',
  credential: 'test-key-123',
});

describe('parseByoImport', () => {
  it('accepts a valid config object', () => {
    expect(parseByoImport(VALID_JSON)).toEqual({
      baseUrl: 'https://api.contoh.com/v1',
      model: 'model-contoh',
      credential: 'test-key-123',
    });
  });

  it('trims the base URL but keeps the credential as-is', () => {
    const cfg = parseByoImport(
      JSON.stringify({ baseUrl: 'https://x.example/v1/ ', model: ' m ', credential: ' k ' }),
    );
    expect(cfg?.baseUrl).toBe('https://x.example/v1/');
    expect(cfg?.model).toBe('m');
    expect(cfg?.credential).toBe(' k ');
  });

  it('rejects malformed JSON, arrays, null and non-objects', () => {
    expect(parseByoImport('not json')).toBeNull();
    expect(parseByoImport('')).toBeNull();
    expect(parseByoImport('[1,2]')).toBeNull();
    expect(parseByoImport('null')).toBeNull();
    expect(parseByoImport('"str"')).toBeNull();
  });

  it('rejects missing or empty fields', () => {
    expect(parseByoImport('{}')).toBeNull();
    expect(parseByoImport(JSON.stringify({ baseUrl: 'https://x', model: 'm' }))).toBeNull();
    expect(parseByoImport(JSON.stringify({ baseUrl: 'https://x', model: 'm', credential: '' }))).toBeNull();
    expect(parseByoImport(JSON.stringify({ baseUrl: '  ', model: 'm', credential: 'k' }))).toBeNull();
    expect(parseByoImport(JSON.stringify({ baseUrl: 'https://x', model: '', credential: 'k' }))).toBeNull();
    expect(parseByoImport(JSON.stringify({ baseUrl: 1, model: 'm', credential: 'k' }))).toBeNull();
  });
});

describe('tryImportByoConfig', () => {
  it('ignores a missing document directory', async () => {
    const fs = makeFs({ documentDirectory: null });
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('absent');
    expect(save).not.toHaveBeenCalled();
    expect(fs.deleteAsync).not.toHaveBeenCalled();
  });

  it('ignores a missing file without calling save or delete', async () => {
    const fs = makeFs();
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('absent');
    expect(fs.getInfoAsync).toHaveBeenCalledWith('file:///mock-docs/' + BYO_IMPORT_FILENAME);
    expect(save).not.toHaveBeenCalled();
    expect(fs.deleteAsync).not.toHaveBeenCalled();
  });

  it('rejects invalid JSON without calling save or delete', async () => {
    const fs = makeFs({
      getInfoAsync: jest.fn(async () => ({ exists: true })),
      readAsStringAsync: jest.fn(async () => 'this is not json'),
    });
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('invalid');
    expect(save).not.toHaveBeenCalled();
    expect(fs.deleteAsync).not.toHaveBeenCalled();
  });

  it('rejects a valid JSON file missing the credential', async () => {
    const fs = makeFs({
      getInfoAsync: jest.fn(async () => ({ exists: true })),
      readAsStringAsync: jest.fn(async () => JSON.stringify({ baseUrl: 'https://x', model: 'm' })),
    });
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('invalid');
    expect(save).not.toHaveBeenCalled();
    expect(fs.deleteAsync).not.toHaveBeenCalled();
  });

  it('ignores a read error without crashing', async () => {
    const fs = makeFs({
      getInfoAsync: jest.fn(async () => ({ exists: true })),
      readAsStringAsync: jest.fn(async () => {
        throw new Error('read failed');
      }),
    });
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('invalid');
    expect(save).not.toHaveBeenCalled();
  });

  it('saves a valid config and deletes the file', async () => {
    const fs = makeFs({
      getInfoAsync: jest.fn(async () => ({ exists: true })),
      readAsStringAsync: jest.fn(async () => VALID_JSON),
    });
    const save = jest.fn(async (_cfg: ByoConfig) => undefined);
    expect(await tryImportByoConfig(fs, save)).toBe('imported');
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith({
      baseUrl: 'https://api.contoh.com/v1',
      model: 'model-contoh',
      credential: 'test-key-123',
    });
    expect(fs.deleteAsync).toHaveBeenCalledTimes(1);
    expect(fs.deleteAsync).toHaveBeenCalledWith('file:///mock-docs/' + BYO_IMPORT_FILENAME);
  });

  it('keeps the file when saving fails so a later launch can retry', async () => {
    const fs = makeFs({
      getInfoAsync: jest.fn(async () => ({ exists: true })),
      readAsStringAsync: jest.fn(async () => VALID_JSON),
    });
    const save = jest.fn(async (_cfg: ByoConfig) => {
      throw new Error('secure store unavailable');
    });
    expect(await tryImportByoConfig(fs, save)).toBe('invalid');
    expect(fs.deleteAsync).not.toHaveBeenCalled();
  });
});
