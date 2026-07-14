# Email classifier eval harness

A small but growing gold-labeled corpus + a pytest runner that exercises
the live classifier against it.

## Run

```bash
OPENAI_API_KEY=... pytest tests/email_eval -v -s
```

Without `OPENAI_API_KEY` set the tests skip so non-LLM CI still passes.

## When a misfire is reported

1. Add the email to `cases.json` with the correct `expected_label`
   (and `expected_company` / `expected_stage_round` when relevant).
2. Run the harness — if it fails, fix the classifier (prompt, few-shot,
   rule, blocklist).
3. Re-run until green. The case stays as a regression test forever.

## Accuracy floors

- Overall ≥ 90% (raise as the corpus grows)
- Noise precision ≥ 95% (a false positive here surfaces marketing in
  the Pipeline, the most painful misfire)
- applied / offer / rejection ≥ 90% (high-stakes labels)

Lower the floors only when there is a real reason — almost always
the right move is to add a case + fix the classifier.
