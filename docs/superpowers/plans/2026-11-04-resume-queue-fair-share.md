# Resume queue fair share

**Goal:** a user's first resumes start right away even while another user has a large backlog.

**Why:** the queue was one global FIFO with 3 slots (see `app/resume_queue.py`), so a 500-job batch made later users wait hours.

**Plan**
1. `_claim_one` picks the user with the fewest running jobs (ties: oldest waiting job), then takes that user's oldest job. Generator-page jobs still go first. Test: `test_claims_share_slots_fairly_across_users`.
2. `RESUME_LLM_CONCURRENCY` default 3 -> 10 (prod already ran ~10). LLM slots cost no extra spend per resume; Chromium renders stay capped by `RESUME_RENDER_CONCURRENCY` (2).
3. Set `RESUME_LLM_CONCURRENCY=10` on Railway (env var overrides the default).
