"""Built-in sample job descriptions, one per role, inserted once on boot.

They are realistic enough to exercise the judge: each has required vs. nice-to-have sections, a years
figure, a core stack, and a location / work-authorization line (which the judge must ignore). Inserted
with `$setOnInsert` under a stable `seedKey`, so a user's edits or archiving are never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.models.prompt_eval import EvalJobDescription, EvalRole


@dataclass(frozen=True)
class SeedJd:
    role: EvalRole
    title: str
    company: str
    text: str


def seed_key(role: str) -> str:
    return f"seed:{role}"


SEED_JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="fullstack",
        title="Senior Full-Stack Engineer (React / Node / PostgreSQL)",
        company="Lumen Health",
        text="""Senior Full-Stack Engineer (React / Node / PostgreSQL)
Lumen Health · Product Engineering
Location: Austin, TX (hybrid, 2 days in office) or remote within the United States. Must be authorized to work in the US without sponsorship.

About the team
Lumen Health builds scheduling and intake software used by more than 900 outpatient clinics. The Scheduling team owns the patient-facing booking flow, the provider calendar, and the integrations that keep both in sync with clinic EHR systems. We ship to production several times a day behind feature flags and we own what we ship.

What you'll do
- Design and build features end to end across a React 18 + TypeScript front end and a Node.js (NestJS) back end on PostgreSQL.
- Own the provider availability engine: time-zone rules, recurring schedules, buffer times, and overbooking policies.
- Model and evolve schemas, write and tune SQL, and keep p95 latency of booking endpoints under 300 ms.
- Build and maintain integrations with EHR and calendar providers (HL7/FHIR, Google and Microsoft calendars) with retries, idempotency keys, and audit logs.
- Raise the bar on testing: unit tests with Jest, API tests with Supertest, and Playwright end-to-end suites in CI.
- Review code, mentor two mid-level engineers, and lead technical design for projects of 4–8 weeks.
- Participate in a light on-call rotation (one week in six) for the services the team owns.

What we're looking for
- 5+ years of professional software engineering experience, with at least 3 years building production web applications across the stack.
- Strong TypeScript, with real depth in React (hooks, state management, performance) and Node.js services.
- Hands-on experience designing relational schemas and writing performant SQL on PostgreSQL; comfortable reading query plans.
- Experience building and operating REST or GraphQL APIs with authentication, rate limiting, and observability (logs, metrics, traces).
- You have shipped and supported software in a regulated or high-stakes domain and can talk about how you handled incidents.
- Clear written communication: design docs, pull request descriptions, and incident write-ups.

Nice to have
- Healthcare integrations (FHIR, HL7v2) or experience with HIPAA controls.
- AWS (ECS or EKS, RDS, SQS) and infrastructure as code with Terraform.
- Experience with event-driven architectures (Kafka, SNS/SQS) or CQRS.
- Familiarity with accessibility standards (WCAG 2.1 AA).

Compensation and benefits
$165,000 – $205,000 base depending on experience, plus equity. Medical, dental and vision, 401(k) with 4% match, 20 days PTO, a $1,500 learning budget, and a home-office stipend.

Lumen Health is an equal opportunity employer. We do not discriminate on the basis of race, religion, color, national origin, gender, sexual orientation, age, marital status, veteran status, or disability status.""",
    ),
    SeedJd(
        role="frontend",
        title="Frontend Engineer, Design Systems (TypeScript / React)",
        company="Northwind Commerce",
        text="""Frontend Engineer, Design Systems
Northwind Commerce · Platform UX
Location: Remote (Europe, within ±2h of CET). Candidates must already have the right to work in the EU or UK; we cannot sponsor visas for this role.

About the team
Northwind powers checkout and storefront experiences for 3,000+ merchants. The Design Systems team builds "Compass", the component library and token system that every product team at Northwind ships with. Our work is measured by how fast other teams can build accessible, consistent interfaces without talking to us.

What you'll do
- Build and maintain React components in TypeScript with a strong focus on accessibility (WCAG 2.2 AA), keyboard interaction, and internationalization (RTL, pluralization).
- Own the design-token pipeline: from Figma variables through Style Dictionary to CSS custom properties and platform outputs.
- Write component documentation and interactive examples in Storybook; maintain visual regression tests with Chromatic.
- Partner with product designers on API design for new components; run office hours for consuming teams.
- Improve performance of the library: bundle size budgets, tree-shaking, SSR compatibility with Next.js.
- Lead migrations across the monorepo (Turborepo, pnpm) with codemods when a breaking change is unavoidable.

Requirements
- 4+ years of front-end engineering experience with at least 2 years building shared UI components or a design system used by multiple teams.
- Expert TypeScript and React, including hooks, context, refs, portals and render performance profiling.
- Deep CSS knowledge: layout (grid, flexbox, container queries), theming, and CSS-in-JS trade-offs; experience with Tailwind or vanilla-extract is a plus.
- Practical accessibility experience: ARIA patterns, focus management, screen-reader testing with NVDA or VoiceOver.
- Testing experience with Jest or Vitest, Testing Library, and Playwright.
- Experience working in a monorepo with CI pipelines and semantic versioning / changesets.

Nice to have
- Contributions to open-source UI libraries (Radix, React Aria, Headless UI, MUI).
- Figma plugin development or experience with Figma's REST API.
- Experience with micro-frontends or module federation.

What we offer
€75,000 – €95,000 depending on location and experience, stock options, 30 days of holiday, a €2,000 annual conference and learning budget, and quarterly team offsites.

We are committed to building a diverse team and welcome applications from people of all backgrounds.""",
    ),
    SeedJd(
        role="backend",
        title="Backend Engineer, Payments Platform (Go / Postgres / Kafka)",
        company="Fjord Payments",
        text="""Backend Engineer, Payments Platform
Fjord Payments · Core Ledger
Location: Oslo or Stockholm (hybrid) or remote in the Nordics. Applicants must hold a valid work permit for Norway or Sweden.

About the team
Fjord processes card and account-to-account payments for marketplaces in 14 countries. The Core Ledger team owns the double-entry ledger, settlement, and the reconciliation jobs that make sure every øre is accounted for. Correctness is non-negotiable; we would rather be slow than wrong, and then we make it fast.

What you'll do
- Design and implement services in Go that handle authorization, capture, refunds, and payouts with exactly-once semantics.
- Model money movement in PostgreSQL with careful transaction boundaries, isolation levels, and idempotency.
- Build event pipelines on Kafka: outbox pattern, schema evolution with Protobuf, consumer lag monitoring.
- Harden the platform: rate limits, circuit breakers, backpressure, and graceful degradation when a PSP is down.
- Write runbooks and improve observability with OpenTelemetry, Prometheus and Grafana; take part in the on-call rotation.
- Work with compliance on PCI DSS controls, audit trails and data retention.

What we're looking for
- 5+ years of backend engineering experience, with at least 2 years in Go in production (we also consider strong Java/Kotlin or Rust engineers willing to switch).
- Solid relational database skills: PostgreSQL transactions, locking, indexing, and query performance under high write load.
- Experience with Kafka or an equivalent log-based system (Pulsar, Kinesis) including exactly-once or idempotent consumers.
- You understand distributed-systems failure modes (partial failure, retries, timeouts, clock skew) and have designed for them.
- Track record of operating services you built: SLOs, incident response, post-mortems.
- Comfortable with Kubernetes, Docker, and CI/CD (GitHub Actions or GitLab CI).

Nice to have
- Payments or banking domain knowledge: card schemes, SEPA, ISO 20022, 3-D Secure, chargebacks.
- gRPC and API design for internal platforms.
- Terraform and AWS (EKS, RDS, MSK).
- Formal methods or property-based testing (TLA+, Go rapid).

Benefits
NOK 950,000 – 1,250,000 per year depending on experience, pension at 7%, private health insurance, 6 weeks of vacation, and a generous parental leave top-up. Relocation support is available for hires moving to Oslo.

Fjord Payments is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ai",
        title="AI Engineer, Applied Agents (Python / RAG / tool use)",
        company="Harbor Analytics",
        text="""AI Engineer, Applied Agents
Harbor Analytics · Applied AI
Location: New York, NY (hybrid, 3 days per week) — remote candidates in the Eastern time zone considered. US work authorization required; we are unable to sponsor H-1B at this time.

About the team
Harbor builds analytics copilots for supply-chain teams. The Applied AI group turns LLM capabilities into reliable product features: agents that answer questions over customer data, draft reports, and trigger workflows in third-party systems. We care about measured quality, not demos.

What you'll do
- Design and ship LLM-powered features end to end: retrieval over structured and unstructured data, tool/function calling, multi-step agent loops, and guardrails.
- Build evaluation harnesses (golden sets, LLM-as-judge, regression suites) and use them to decide what ships.
- Own prompt and context engineering for production flows; manage prompt versions, caching and cost.
- Implement RAG pipelines: chunking strategies, embeddings, hybrid search (BM25 + vectors), reranking, citations.
- Integrate with Anthropic and OpenAI APIs and at least one open-weight model served via vLLM; design fallbacks across providers.
- Instrument everything: token usage, latency, failure modes, hallucination rate; build dashboards the team actually reads.
- Work closely with product and data engineering to define what "correct" means for each feature.

Requirements
- 3+ years of software engineering experience in Python, with at least 1 year building LLM-based features that reached real users.
- Hands-on experience with retrieval systems (pgvector, Pinecone, Weaviate, Elasticsearch or similar) and with embedding models.
- Experience with structured outputs, function/tool calling, and agent frameworks (LangGraph, the OpenAI Agents SDK, or hand-rolled loops) — and opinions on when not to use them.
- You have built or maintained an evaluation pipeline and can explain how you measured a feature before and after a change.
- Strong backend fundamentals: async Python (FastAPI), PostgreSQL, queues, and deploying services on AWS or GCP.
- Clear communicator who can explain model behaviour and trade-offs to non-engineers.

Nice to have
- Fine-tuning or distillation experience (LoRA, DPO) and familiarity with PyTorch.
- Experience with observability tools for LLM apps (LangSmith, Braintrust, Arize, OpenTelemetry).
- Background in supply chain, logistics, or B2B analytics.
- Publications or open-source contributions in the LLM space.

Compensation
$170,000 – $215,000 base plus equity and an annual bonus. Full health coverage, 401(k) match, unlimited PTO with a 15-day minimum, and a $3,000 learning budget.

Harbor Analytics is proud to be an equal opportunity workplace.""",
    ),
    SeedJd(
        role="ml",
        title="Machine Learning Engineer, Recommendations (PyTorch / Spark)",
        company="Streamline Media",
        text="""Machine Learning Engineer, Recommendations
Streamline Media · Personalization
Location: London, UK (hybrid) or remote within the UK. Must have the right to work in the UK; visa sponsorship is available for exceptional candidates.

About the team
Streamline is a video streaming service with 40 million monthly viewers. The Personalization team owns the recommendation models behind the home page, autoplay, and push notifications. A one-percent lift in watch time moves the business; we A/B test everything and ship models weekly.

What you'll do
- Train and deploy ranking and retrieval models (two-tower retrieval, sequence models, multi-task rankers) in PyTorch.
- Build feature pipelines on Spark and Airflow over petabyte-scale event data; own feature freshness and point-in-time correctness.
- Run online experiments, analyse results with the data science team, and make the ship / no-ship call.
- Serve models at low latency (p99 < 50 ms) with Triton or TorchServe behind a feature store (Feast or an in-house equivalent).
- Monitor models in production: drift, calibration, popularity bias, and cold-start coverage.
- Collaborate with product on objectives beyond clicks: completion rate, diversity, and long-term retention.

What we're looking for
- 4+ years of experience building ML systems in production, including at least 2 years on recommendation, search or ads ranking.
- Strong PyTorch (or TensorFlow with willingness to switch) and a solid grasp of modern recommender architectures.
- Experience with large-scale data processing in Spark (PySpark or Scala) and workflow orchestration (Airflow, Dagster).
- Statistics for experimentation: power analysis, variance reduction (CUPED), multiple comparisons.
- Production engineering skills: Python, Docker, Kubernetes, CI/CD, and model serving infrastructure.
- MSc or PhD in computer science, statistics or a related field, or equivalent practical experience.

Nice to have
- Reinforcement learning or bandit approaches for recommendations.
- Experience with GPU training at scale (DDP, mixed precision) and cost optimisation.
- Familiarity with Kafka / Flink for near-real-time features.
- Publications at RecSys, KDD, WWW or similar.

Package
£95,000 – £125,000 base plus bonus and equity, private medical, 28 days holiday plus bank holidays, pension at 6%, and a hybrid working allowance.

We welcome applicants from all backgrounds and are happy to discuss flexible working.""",
    ),
    SeedJd(
        role="llm",
        title="LLM Engineer, Evaluation & Fine-Tuning",
        company="Cobalt Labs",
        text="""LLM Engineer, Evaluation & Fine-Tuning
Cobalt Labs · Model Platform
Location: San Francisco, CA (in office 4 days per week). We can sponsor visas for this role. Relocation assistance provided.

About the team
Cobalt Labs builds domain-specialised language models for legal and financial document work. The Model Platform team owns the training and evaluation stack that every model we ship goes through: data pipelines, fine-tuning jobs, eval suites, and the serving layer. Our customers trust our models with contracts worth millions; evaluation rigour is the product.

What you'll do
- Build and maintain evaluation suites: task-specific benchmarks, human-preference data collection, LLM-as-judge pipelines with calibration against human labels, and regression tracking across model versions.
- Run supervised fine-tuning and preference optimisation (SFT, DPO, RLHF variants) on open-weight models (Llama, Qwen, Mistral) using PyTorch, Hugging Face TRL, and DeepSpeed or FSDP on multi-node GPU clusters.
- Design synthetic-data generation and filtering pipelines; measure their effect on downstream evals.
- Optimise inference: quantisation (AWQ, GPTQ), speculative decoding, KV-cache strategies, and serving with vLLM or TensorRT-LLM.
- Partner with research on ablations and write up results clearly for the whole company.
- Own cost and throughput budgets for training and eval runs.

Requirements
- 3+ years of experience in ML engineering, with at least 1.5 years working directly on LLM training, fine-tuning or evaluation.
- Deep PyTorch and Hugging Face ecosystem knowledge (transformers, datasets, PEFT, TRL, accelerate).
- Experience running distributed training on multi-GPU / multi-node setups and debugging it (NCCL, memory, throughput).
- Strong Python engineering: typed code, tests, reproducible experiments, and experiment tracking (Weights & Biases or MLflow).
- You have designed an evaluation that changed a decision and can explain its blind spots.
- Comfortable reading recent papers and turning them into working code within days.

Nice to have
- Experience with RAG evaluation, long-context models, or structured-output constraints.
- CUDA or Triton kernel work.
- Familiarity with safety evaluations and red-teaming.
- Experience in legal, financial or other regulated document domains.

Compensation
$190,000 – $260,000 base plus meaningful equity, full benefits, catered lunches, a $5,000 annual learning budget and a conference travel allowance.

Cobalt Labs is an equal opportunity employer and values diversity at our company.""",
    ),
    SeedJd(
        role="data",
        title="Senior Data Engineer (Airflow / dbt / Snowflake)",
        company="Meridian Logistics",
        text="""Senior Data Engineer
Meridian Logistics · Data Platform
Location: Remote, Canada or United States. Occasional travel (2–3 times a year) to our Toronto headquarters. Candidates must be legally authorized to work in Canada or the US.

About the team
Meridian runs freight operations across North America, moving 250,000 shipments a month. The Data Platform team owns the pipelines, warehouse and semantic layer behind operational dashboards, pricing models, and customer-facing reporting. Our users are dispatchers, finance analysts and the ML team, and all of them notice when data is late.

What you'll do
- Design, build and operate batch and streaming pipelines with Airflow, dbt and Snowflake, ingesting from PostgreSQL, Kafka, SFTP partners and third-party APIs.
- Own data modelling for core domains (shipments, carriers, invoices): dimensional models, slowly changing dimensions, and clear contracts with producers.
- Establish data quality and observability: tests in dbt, freshness SLAs, anomaly detection, and lineage (OpenLineage or similar).
- Improve warehouse performance and cost: clustering, query optimisation, materialisation strategies, and credit monitoring.
- Build and maintain the ingestion framework in Python; write infrastructure as code with Terraform on AWS.
- Mentor junior engineers and set standards for code review, documentation and on-call.

Requirements
- 5+ years of data engineering experience with at least 2 years owning production pipelines on a modern stack (Airflow or Dagster, dbt, Snowflake or BigQuery or Redshift).
- Advanced SQL and strong Python; you can profile a slow model and fix it.
- Experience with streaming or CDC (Kafka, Debezium, Kinesis) and with handling late or out-of-order data.
- Solid understanding of data modelling (Kimball, Data Vault or similar) and of designing for analysts and ML consumers.
- Experience running infrastructure as code and CI/CD for data (dbt Cloud or GitHub Actions, Terraform).
- Able to communicate clearly with non-technical stakeholders about data definitions and trade-offs.

Nice to have
- Logistics, transportation or supply-chain domain experience.
- Experience with Spark or Flink for larger-than-warehouse workloads.
- Semantic layer tooling (Cube, Looker LookML, dbt metrics) and BI administration.
- Data governance: PII handling, access controls, retention.

Compensation
CAD 140,000 – 175,000 (or USD equivalent) depending on experience, plus annual bonus, extended health and dental, RRSP/401(k) matching, 4 weeks vacation, and a home-office budget.

Meridian Logistics is committed to employment equity and encourages applications from all qualified candidates.""",
    ),
    SeedJd(
        role="test",
        title="Senior QA Automation Engineer (Playwright / API testing)",
        company="Atlas Fintech",
        text="""Senior QA Automation Engineer
Atlas Fintech · Quality Engineering
Location: Dublin, Ireland (hybrid, 2 days in office) or fully remote within Ireland. EU work authorization required.

About the team
Atlas provides business banking to 60,000 small companies across Europe. The Quality Engineering team owns the automated test strategy for our web app, mobile apps and public API, and embeds with product squads to keep release trains moving without regressions. Payments go out at 09:00; a missed bug has a real cost.

What you'll do
- Design and maintain the end-to-end test framework in TypeScript with Playwright, covering web flows across Chromium, WebKit and Firefox, with parallel execution in CI (GitHub Actions).
- Build API test suites for our REST and GraphQL services (Postman/Newman or a code-first framework), including contract tests with Pact.
- Own test data strategy: seeded environments, synthetic accounts, and masking of production-like data.
- Introduce and tune quality gates: flakiness tracking, test impact analysis, and coverage reports product teams can act on.
- Pair with developers to shift testing left: component tests, test plans for new features, and exploratory testing sessions before major releases.
- Lead incident retrospectives from the quality angle and turn findings into automated checks.
- Mentor two QA engineers and run the team's guild on test automation practices.

Requirements
- 5+ years in software quality engineering with at least 3 years building and maintaining automation frameworks in TypeScript or JavaScript.
- Strong hands-on Playwright experience (or Cypress with proven Playwright migration); you know how to make a suite fast and non-flaky.
- API testing experience including authentication flows, idempotency, and negative testing; comfortable reading service logs and database rows to diagnose failures.
- CI/CD experience: parallelisation, sharding, artifacts, and reporting (Allure, Playwright HTML reports).
- Solid understanding of test strategy: the test pyramid, risk-based testing, and when manual exploratory testing is the right tool.
- Experience working in an agile product team and advocating for quality without being a gate.

Nice to have
- Mobile automation (Appium, Detox or Maestro) for iOS and Android.
- Performance testing with k6 or Gatling; security testing basics (OWASP ZAP).
- Experience in fintech, banking or another regulated domain with audit requirements.
- Familiarity with accessibility testing (axe, screen readers).

What we offer
€80,000 – €100,000 base plus bonus and equity, private health insurance, pension contribution, 25 days of annual leave, a learning budget, and a wellbeing allowance.

Atlas Fintech is an equal opportunity employer and welcomes applicants from all backgrounds.""",
    ),
)


async def seed_eval_jds(coll=None) -> int:
    """Insert any seed JD that is not there yet; never overwrite. Returns the number inserted."""
    if coll is None:
        coll = EvalJobDescription.get_motor_collection()
    inserted = 0
    now = datetime.utcnow()
    for seed in SEED_JDS:
        result = await coll.update_one(
            {"seedKey": seed_key(seed.role)},
            {
                "$setOnInsert": {
                    "role": seed.role,
                    "title": seed.title,
                    "company": seed.company,
                    "text": seed.text,
                    "seedKey": seed_key(seed.role),
                    "createdBy": None,
                    "archived": False,
                    "archivedAt": None,
                    "createdAt": now,
                    "updatedAt": now,
                }
            },
            upsert=True,
        )
        if getattr(result, "upserted_id", None) is not None:
            inserted += 1
    return inserted


def seed_by_role(role: str) -> Optional[SeedJd]:
    return next((s for s in SEED_JDS if s.role == role), None)
