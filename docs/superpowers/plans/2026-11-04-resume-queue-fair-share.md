# Resume queue fair share

**Goal:** a user's first resumes start right away even while another user has a large backlog.

**Why:** the queue was one global FIFO with 3 slots (see `app/resume_queue.py`), so a 500-job batch made later users wait hours.

**Plan**
1. `_claim_one` picks the user with the fewest running jobs (ties: oldest waiting job), then takes that user's oldest job. Generator-page jobs still go first. Tests: `test_claims_share_slots_fairly_across_users`, and `test_new_users_job_starts_when_a_batch_holds_every_slot` (one user's batch holds every slot, a second user queues one job, the next freed slot goes to the second user).
2. `RESUME_LLM_CONCURRENCY` default 3 -> 10 (about 1,500 resumes/hr at a 24 s median run). LLM slots cost no extra spend per resume; Chromium renders stay capped by `RESUME_RENDER_CONCURRENCY` (default 2).
3. Railway sets `RESUME_LLM_CONCURRENCY=10` and `RESUME_RENDER_CONCURRENCY=3` (neither was set before, so production ran the code defaults). At 3 slots the 8 vCPU / 8 GB container peaked at 1.65 GB and 0.29 vCPU, so there is room. With 10 slots at a 24 s median, two renderers would each need to finish in under 4.8 s to keep up, hence 3. If runs sit at the render step, raise it; if the LLM provider returns 429s, lower `RESUME_LLM_CONCURRENCY`.

**Measured:** with 1,000 queued ~20 KB postings from 5 users, `_claim_one` takes about 21 ms (p95 25 ms) against about 2 ms for the old single read, so no index change is needed.
