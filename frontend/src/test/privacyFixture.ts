import type { PrivacyPolicy } from '../api/types'
import { fixtures } from '../api/fixtures'

export const policyFixture: PrivacyPolicy = {
  version: 'SYNTHETIC-POLICY-v1',
  text: 'SYNTHETIC POLICY. Do not enter real personal data.',
  checksum: 'a'.repeat(64),
  withdrawal_instructions:
    'Synthetic only. Use the agreed test contact channel.',
  consent_version: fixtures.consent_text.version,
  synthetic: true,
  collection_enabled: true,
}
