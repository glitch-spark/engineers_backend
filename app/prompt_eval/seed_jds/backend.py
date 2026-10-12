from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="backend",
        key="seed:backend",
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
        role="backend",
        key=key_for("backend", "granite-cloud"),
        title="Senior Backend Engineer, API Platform (Java / Spring)",
        company="Granite Cloud",
        text="""Senior Backend Engineer, API Platform (Java / Spring)
Granite Cloud · Developer Platform
Location: Seattle, WA (hybrid) or remote in the US. Must be authorized to work in the United States.

About the team
Granite provides infrastructure APIs (object storage, DNS, compute) to 40,000 developers. The API Platform team owns the public API gateway, authentication, rate limiting, API keys and the SDK generation pipeline. Every product at Granite ships through what we build.

What you'll do
- Design and build gateway services in Java 21 and Spring Boot, running on Kubernetes across three regions.
- Own authentication and authorization for the public API: API keys, OAuth 2 clients, scopes, and audit logging.
- Build rate limiting and quota systems that are fair, fast (sub-millisecond decisions) and explainable to customers.
- Keep API contracts honest: OpenAPI as the source of truth, backward-compatibility checks in CI, generated SDKs in five languages.
- Operate what you build: SLOs, dashboards, runbooks, and a weekly on-call rotation shared across the team.
- Write design documents and drive them to decisions with other teams.

Requirements
- 6+ years of backend engineering with at least 4 years in Java and Spring (Boot, Security, WebFlux or MVC).
- Experience building high-throughput, low-latency services (tens of thousands of requests per second) and the measurements to prove it.
- Strong grasp of authentication and authorization protocols (OAuth 2, OIDC, JWT) and their failure modes.
- Experience with Kubernetes, Helm, and observability (Prometheus, Grafana, OpenTelemetry).
- Comfortable with PostgreSQL and a cache or KV store (Redis, DynamoDB).
- Clear written communication; you have authored designs other teams implemented.

Nice to have
- Envoy, Kong or another gateway/proxy internals experience.
- gRPC and Protobuf schema evolution.
- Experience publishing SDKs or maintaining a public API with a deprecation policy.
- Go, for the parts of the data plane written in it.

Compensation
$175,000 – $215,000 base plus equity and bonus. Medical, dental, vision, 401(k) match, 20 days PTO, and a $2,000 learning budget.

Granite Cloud is an equal opportunity employer.""",
    ),
    SeedJd(
        role="backend",
        key=key_for("backend", "tidewater-games"),
        title="Backend Engineer, Game Services (C# / .NET / Azure)",
        company="Tidewater Games",
        text="""Backend Engineer, Game Services (C# / .NET / Azure)
Tidewater Games · Online Services
Location: Montreal, QC (hybrid, 2 days per week). Open to candidates eligible to work in Canada; relocation assistance available within Canada.

About the studio
Tidewater makes a live-service co-op game with 3 million monthly players. The Online Services team owns matchmaking, player progression, the in-game store, and the telemetry pipeline. Launch days and seasonal events are our Super Bowl; the services must hold.

What you'll do
- Build and operate services in C# (.NET 8, ASP.NET Core) on Azure (AKS, Cosmos DB, Service Bus, Redis).
- Own matchmaking and session services: low-latency, horizontally scalable, resilient to regional outages.
- Design player data models and migrations for progression and inventory with strong consistency where it matters.
- Load-test before every major release (k6, Azure Load Testing) and fix what breaks.
- Build tooling for live-ops: feature flags, event scheduling, store configuration, and safe rollbacks.
- Share an on-call rotation with five other engineers; participate in launch war rooms.

What we're looking for
- 4+ years of backend development in C# and .NET, with services in production at meaningful scale.
- Experience with cloud infrastructure, ideally Azure (AKS, Cosmos DB or SQL, Service Bus) or equivalent on AWS/GCP.
- Understanding of distributed systems trade-offs: consistency, idempotency, retries, and backpressure.
- Experience with caching and message queues and the patterns around them (outbox, sagas).
- Comfortable with performance profiling and memory diagnostics in .NET.
- Collaborative and calm during incidents.

Nice to have
- Games industry experience or familiarity with game backends (PlayFab, Nakama, Photon, Steamworks, console platform services).
- gRPC, Protobuf and binary serialization.
- Kubernetes operations and infrastructure as code (Bicep, Terraform).
- French language skills.

Compensation
CAD 115,000 – 145,000 base plus bonus, comprehensive health benefits, RRSP matching, 4 weeks vacation, and a game-launch bonus program.

Tidewater Games is an equal opportunity employer.""",
    ),
    SeedJd(
        role="backend",
        key=key_for("backend", "verdant-marketplace"),
        title="Backend Developer (Python / Django / PostgreSQL)",
        company="Verdant Market",
        text="""Backend Developer (Python / Django / PostgreSQL)
Verdant Market · Order Management
Location: Amsterdam, Netherlands (hybrid). EU/EEA citizens or holders of a Dutch work permit; we can apply for the highly skilled migrant visa for strong candidates.

About Verdant
Verdant is an online marketplace for independent food producers, delivering to 300,000 households in the Benelux. The Order Management team owns the order lifecycle: cart, checkout, fulfilment slots, substitutions, refunds, and the integrations with our warehouses and delivery partners.

What you'll do
- Build and maintain services in Python 3.12 with Django and Django REST Framework on PostgreSQL, Celery and RabbitMQ.
- Model the order lifecycle as explicit state machines with an audit trail; make partial fulfilment and refunds correct by construction.
- Integrate with warehouse management and last-mile delivery APIs; handle failures with retries, dead-letter queues and alerting.
- Improve throughput for peak evenings (orders spike 10x before cut-off): query optimisation, connection pooling, async tasks.
- Write tests with pytest and factory_boy; keep the test suite under ten minutes.
- Take part in code review and in the team's rotation for production support.

Requirements
- 3+ years of Python backend development with Django (or Flask/FastAPI with a strong desire to work in Django).
- Good PostgreSQL knowledge: transactions, locking, indexes, migrations on live tables.
- Experience with task queues (Celery, RQ, Dramatiq) and message brokers.
- Experience integrating third-party APIs with proper error handling and observability.
- Ability to write clear English documentation and participate in design discussions.

Nice to have
- E-commerce, marketplace or logistics domain experience.
- Docker, Kubernetes and GitLab CI.
- Event-driven architectures (Kafka) and the outbox pattern.
- Dutch language skills.

What we offer
€65,000 – €85,000 base, 8% holiday allowance, 26 days of vacation, pension contribution, a bike plan, and weekly groceries from our producers.

Verdant Market is an equal opportunity employer.""",
    ),
    SeedJd(
        role="backend",
        key=key_for("backend", "northstar-telemetry"),
        title="Senior Backend Engineer, Ingestion (Rust / Kafka / ClickHouse)",
        company="Northstar Telemetry",
        text="""Senior Backend Engineer, Ingestion (Rust / Kafka / ClickHouse)
Northstar Telemetry · Ingest
Location: Remote (Americas or Europe, within 4 hours of UTC). Candidates must be able to work as an employee or contractor in their country of residence.

About us
Northstar is an observability platform ingesting 20 million events per second from customers' applications. The Ingest team owns everything between the customer's SDK and the storage layer: edge receivers, parsing, enrichment, sampling and the Kafka pipelines that feed ClickHouse.

What you'll do
- Design and build ingestion services in Rust (Tokio, Axum, rdkafka) with a focus on throughput, memory safety and predictable latency.
- Own the Kafka layer: partitioning strategy, consumer groups, exactly-once semantics where needed, lag monitoring.
- Implement sampling and rate limiting that protect the platform without surprising customers.
- Optimise ClickHouse ingestion: batching, schemas, materialized views, and backpressure from storage to the edge.
- Improve resilience: multi-region failover, replay from object storage, chaos exercises.
- Mentor engineers moving from Go or Java to Rust; review code with a focus on correctness.

Requirements
- 6+ years of backend engineering with 2+ years of Rust in production (or expert-level Go/C++ with substantial Rust side experience).
- Deep experience with Kafka or a comparable log system at high volume.
- Experience with a columnar analytics store (ClickHouse, Druid, Pinot) or with building storage-adjacent systems.
- Strong grasp of systems performance: profiling, allocation behaviour, zero-copy parsing, async runtimes.
- You have run services with SLOs and on-call and can talk about incidents you led.
- Comfortable with Kubernetes and Terraform on AWS or GCP.

Nice to have
- Experience with OpenTelemetry, the OTLP protocol or observability products.
- Contributions to open-source Rust or Kafka ecosystems.
- Experience with S3-based tiered storage or Parquet.

Compensation
$180,000 – $230,000 USD (or local equivalent) plus equity. Remote-first with a $3,000 home-office and travel budget, 25 days of PTO, and health coverage via local providers.

Northstar Telemetry is an equal opportunity employer.""",
    ),
)
