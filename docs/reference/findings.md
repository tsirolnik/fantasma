# Finding data reference

Generated from [models/finding.py](../../models/finding.py). The complete
[JSON schema](finding.schema.json) includes enums and nested definitions.
Regenerate with `python scripts/generate_reference.py`.

Acceptance and independent verification are separate; see [runtime](runtime.md).

| Field | Schema type/reference | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | integer | no | `-1` |  |
| `target` | string | yes | `(not declared)` |  |
| `vuln_type` | #/$defs/VulnType | yes | `(not declared)` |  |
| `owasp` | [{'$ref': '#/$defs/OwaspCategory'}, {'type': 'null'}] | no | `null` |  |
| `cwe_id` | [{'type': 'integer'}, {'type': 'null'}] | no | `null` |  |
| `severity` | #/$defs/Severity | no | `"info"` |  |
| `title` | string | no | `""` |  |
| `description` | string | no | `""` |  |
| `evidence` | string | no | `""` |  |
| `url` | string | no | `""` |  |
| `poc` | string | no | `""` |  |
| `impact` | string | no | `""` |  |
| `composed_of` | array | no | `(not declared)` |  |
| `chain_narrative` | string | no | `""` |  |
| `reproducible` | boolean | no | `false` |  |
| `verification_status` | #/$defs/VerificationStatus | no | `"unverified"` |  |
| `verification_details` | string | no | `""` |  |
| `confidence_score` | number | no | `0.0` |  |
| `estimated_bounty` | number | no | `0.0` |  |
| `ai_severity` | [{'$ref': '#/$defs/Severity'}, {'type': 'null'}] | no | `null` |  |
| `ai_exploitability` | string | no | `""` |  |
| `ai_false_positive_likelihood` | number | no | `0.0` |  |
| `ai_bounty_estimate` | number | no | `0.0` |  |

Consult the JSON schema for nested definitions and validation details.
