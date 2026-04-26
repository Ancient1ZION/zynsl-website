---
name: Drift report
about: Report an agent behaving outside its declared persona/goal
title: "[drift] Agent <name> — <one-line symptom>"
labels: drift, manual-review
assignees: ''
---

> **Note:** `drift_detector.py` opens these automatically with severity tags.
> Use this template only for drift you spotted manually.

## Agent

`<agent id from agents_config.json>`

## Severity (your assessment)

- [ ] LOW — occasional tool fail, single off-topic output
- [ ] HIGH — repeated tool failures, persona slipping
- [ ] CRITICAL — off-rails persona, possible exfiltration

## Symptom

What did the agent do that was wrong?

## Offending log lines

```
<paste 5-20 lines from logs/zyn_empire.log around the incident>
```

## Current agents_config.json entry

```json
<paste the agent's current config block>
```

## Suggested correction

Refined `persona` / `goal` / `tools` you'd recommend.

## Reproducibility

- [ ] Reproducible on demand
- [ ] Intermittent
- [ ] Single observation
