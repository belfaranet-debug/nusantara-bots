/**
 * Model provider untuk pengaturan BYO (Bring Your Own Key).
 *
 * BYOK saja: tidak ada preset model bawaan. Satu-satunya provider
 * adalah Custom — pengguna mengisi sendiri Base URL, nama model,
 * dan API key dari provider OpenAI-compatible pilihannya.
 *
 * Credential tidak pernah ada di file ini: credential diisi user
 * di Settings dan hidup hanya di secure storage (lihat byo.ts).
 * Panggilan BYO sendiri (baseUrl + "/chat/completions", Bearer
 * auth, SSE via XHR) tidak berubah.
 */

export interface Provider {
  /** Stable id — selalu "custom" di versi BYOK saja ini. */
  id: string;
  /** Display name shown in Settings. */
  name: string;
  /** Provider base URL (no trailing slash). Empty for Custom. */
  baseUrl: string;
  /** Models offered by this provider. Empty for Custom (free text). */
  models: string[];
  /** Model selected by default when this provider is picked. */
  defaultModel: string;
}

export const PROVIDERS: Provider[] = [
  {
    id: 'custom',
    name: 'Custom (OpenAI-compatible)',
    baseUrl: '',
    models: [],
    defaultModel: '',
  },
];

/** Find a provider by id; falls back to the Custom provider. */
export function getProvider(id: string | null | undefined): Provider {
  return PROVIDERS.find((p) => p.id === id) ?? PROVIDERS[PROVIDERS.length - 1];
}

/** Model list for a provider id (empty array for Custom). */
export function getModelsForProvider(id: string | null | undefined): string[] {
  return getProvider(id).models;
}

/**
 * Guess the provider from a saved base URL. Di versi BYOK saja
 * ini tidak ada preset base URL yang dicocokkan — selalu Custom.
 * Dipakai untuk memulihkan state picker dari config BYO tersimpan
 * (yang tidak punya field provider sendiri).
 */
export function inferProviderId(_baseUrl: string | null | undefined): string {
  return 'custom';
}
