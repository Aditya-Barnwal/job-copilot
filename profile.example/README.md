# profile.example/ — fake profile for CI and new setups

This directory mirrors the git-ignored `profile/` with an **obviously fake**
person ("Alex Example", `example.com`). It is committed so CI can validate the
schema and so the tests have data that isn't yours. Never put real data here;
`scripts/check_pii.py` fails CI on real emails or phone numbers.

| File | Real counterpart |
|---|---|
| `resume_facts.yaml` | `profile/resume_facts.yaml`: résumé facts with ids, sources, metrics and status (schema: `jobcopilot/facts.py`) |
| `answers.yaml` | `profile/answers.yaml`: screening-answers bank (filled with fake values here; the real template starts empty) |
| `base_resumes/bases.yaml` | `profile/base_resumes/bases.yaml`: layout per base résumé (the PDFs aren't committed) |

```sh
python -m jobcopilot.facts check profile.example/resume_facts.yaml
```

To start a profile from scratch, copy this directory to `profile/` and replace
every value. Set each fact to `unconfirmed` until you've checked it.
