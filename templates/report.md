# {{ title }}
{% if verification_banner %}
> {{ verification_banner }}
{% endif %}
**Severity:** {{ severity }}
**Target:** {{ finding.target }}
**Type:** {{ finding.vuln_type.value }}{% if finding.owasp %} · **OWASP:** {{ finding.owasp.value }}{% endif %}{% if finding.cwe_id %} · **CWE-{{ finding.cwe_id }}**{% endif %}
**Verification:** {{ verification_status }}
**Date:** {{ created_at }}
**Estimated Bounty:** ${{ "%.2f"|format(estimated_bounty) }}

---

## Description

{{ description }}

## Steps to Reproduce

{% for step in steps_to_reproduce %}
{{ loop.index }}. {{ step }}
{% endfor %}

## Evidence

```
{{ finding.evidence }}
```

## Impact

{{ impact }}

## Remediation

{{ remediation }}

{% if references %}
## References

{% for ref in references %}
- {{ ref }}
{% endfor %}
{% endif %}

---

*ROI: Estimated cost ${{ "%.2f"|format(estimated_cost) }}, Expected bounty ${{ "%.2f"|format(estimated_bounty) }}, ROI {{ "%.0f"|format(roi_ratio) }}%*
