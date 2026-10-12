from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="staff_swe",
        key=key_for("staff_swe", "ledgerline-architecture"),
        title="Staff Software Engineer, Platform Architecture",
        company="Ledgerline",
        text="""Staff Software Engineer, Platform Architecture
Ledgerline · Engineering
Location: San Francisco, CA or New York, NY (hybrid, 3 days per week). Visa sponsorship available.

About the role
Ledgerline provides accounting automation to 15,000 mid-market companies. Engineering has grown to 140 people and twelve teams in three years; the architecture has not kept up. We are hiring a Staff engineer to own the technical direction of the platform: service boundaries, data ownership, the migration off a shared database, and the standards that let teams move independently.

What you'll do
- Set and communicate the multi-year architecture direction; write the documents and run the forums where decisions get made.
- Lead the decomposition of a Python/Django monolith with a shared PostgreSQL database into well-bounded services with clear data ownership, in increments that ship value along the way.
- Define platform standards: API conventions, event schemas (Kafka), observability, reliability targets, and the paved road teams adopt because it is better.
- Partner with engineering managers and product leads to sequence work and resolve cross-team trade-offs.
- Stay hands-on: prototype the hard parts, review critical pull requests, and lead incident reviews for systemic issues.
- Mentor senior engineers and grow the next generation of technical leads.

Requirements
- 10+ years of software engineering with at least 3 years operating at a staff or principal level across multiple teams.
- Demonstrated experience leading a large architectural change (monolith decomposition, data-ownership migration, platform consolidation) through to measurable outcomes.
- Deep expertise in distributed systems, data modelling and event-driven architectures; strong Python and SQL, with the ability to work in other languages.
- You influence without authority: a track record of decisions adopted across an organisation.
- Excellent written communication; your design documents are read and reused.
- Experience in fintech, accounting or another correctness-critical domain is a plus.

Nice to have
- Experience with Kafka, Debezium or other CDC-based migration approaches.
- Kubernetes, Terraform and AWS at scale.
- Experience defining SLOs and error budgets across an organisation.
- Public talks or writing on architecture.

Compensation
$240,000 – $300,000 base plus significant equity and bonus. Full benefits, 401(k) match, unlimited PTO with a minimum, and a learning budget.

Ledgerline is an equal opportunity employer.""",
    ),
    SeedJd(
        role="staff_swe",
        key=key_for("staff_swe", "cascade-reliability"),
        title="Staff Software Engineer, Reliability & Performance",
        company="Cascade Streaming",
        text="""Staff Software Engineer, Reliability & Performance
Cascade Streaming · Core Infrastructure
Location: Remote within the United States or Canada. Must be authorized to work in the US or Canada.

About Cascade
Cascade is a live video platform used by broadcasters and creators; peak events reach 8 million concurrent viewers. Core Infrastructure owns the delivery pipeline, origin services and the control plane. We are hiring a Staff engineer to own reliability and performance across the platform: the standards, the hardest investigations, and the architectural changes that remove whole classes of incidents.

What you'll do
- Own the platform's reliability strategy: SLOs, capacity models, failure-domain design, and the roadmap of resilience work across six teams.
- Lead investigations of the most complex performance and availability problems across Go services, C++ media components, Kubernetes and the CDN.
- Design systemic improvements: load shedding, graceful degradation, multi-region failover, and traffic engineering for live events.
- Build or specify the tooling the organisation needs: load testing for live traffic patterns, chaos experiments, and incident analytics.
- Run readiness reviews for major events and lead post-incident reviews that produce real change.
- Mentor senior engineers in systems thinking and performance engineering.

Requirements
- 10+ years of engineering experience with at least 3 years at a staff level in large-scale distributed systems.
- Deep expertise in performance engineering: profiling, capacity planning, networking, and the behaviour of systems under saturation.
- Strong Go or C++ and the ability to read and contribute across languages.
- Experience operating Kubernetes, CDNs and multi-region deployments at high scale.
- A track record of reducing incident rates across an organisation, not only within a team.
- Clear, calm communication during incidents and in executive reviews.

Nice to have
- Video delivery, streaming protocols (HLS, DASH, WebRTC) or media systems experience.
- eBPF, kernel networking or high-performance networking experience.
- Experience designing observability platforms at scale.
- Experience with FinOps for compute-heavy workloads.

Compensation
$250,000 – $320,000 base plus equity. Health coverage, 401(k)/RRSP matching, 25 days of PTO, and a home-office budget.

Cascade Streaming is an equal opportunity employer.""",
    ),
    SeedJd(
        role="staff_swe",
        key=key_for("staff_swe", "greenfield-ai-platform"),
        title="Staff Software Engineer, AI Platform",
        company="Greenfield Software",
        text="""Staff Software Engineer, AI Platform
Greenfield Software · Office of the CTO
Location: Remote within Europe; monthly travel to Amsterdam. Must have the right to work in an EU or EEA country, Switzerland or the UK.

About the role
Greenfield is a 1,200-person enterprise software company with LLM features shipping in six products. We have a dozen teams building similar pipelines, evaluation tooling and guardrails in parallel. We are hiring a Staff engineer to define the company's AI platform: what is shared, what is owned by teams, how quality is measured, and how cost and risk are controlled. You will build the first version with a small team and then scale adoption.

What you'll do
- Define the AI platform architecture: model gateway, prompt and version management, evaluation services, retrieval infrastructure, observability and cost governance.
- Lead a founding team of four engineers to build it, while staying hands-on in Python and TypeScript.
- Establish evaluation standards: how a feature proves it is good enough to ship and how regressions are caught.
- Partner with security, legal and procurement on model-provider strategy, data handling and compliance (GDPR, EU AI Act readiness).
- Work with product teams to migrate to the platform; measure adoption and developer experience.
- Advise the CTO and executives on AI investments with evidence rather than hype.

Requirements
- 10+ years of software engineering with 3+ years at a staff level and 2+ years building LLM-based systems in production.
- Hands-on depth in retrieval, evaluation and LLM application architecture; strong Python; comfortable with TypeScript.
- Experience building internal platforms adopted by many teams and the judgement to decide what should not be centralised.
- Experience with observability, cost control and security for multi-tenant services.
- Outstanding communication with executives, security and legal stakeholders as well as engineers.

Nice to have
- Experience self-hosting models (vLLM) and with fine-tuning pipelines.
- Experience with the EU regulatory landscape for AI.
- Prior experience in a platform or developer-experience organisation.
- Public writing or talks on applied AI engineering.

Package
€150,000 – €190,000 base plus equity and bonus, 30 days of holiday, pension, and a travel budget.

Greenfield Software is an equal opportunity employer.""",
    ),
    SeedJd(
        role="staff_swe",
        key=key_for("staff_swe", "orchard-frontend"),
        title="Staff Frontend Engineer, Web Platform",
        company="Orchard Commerce",
        text="""Staff Frontend Engineer, Web Platform
Orchard Commerce · Web Platform
Location: Toronto, ON (hybrid) or remote within Canada or the United States. Must be eligible to work in Canada or the US.

About Orchard
Orchard powers online stores for 40,000 merchants; our storefront and admin apps are among the largest React codebases in the industry. The Web Platform team owns the frameworks, build system, design system and performance tooling that 300 front-end engineers rely on. We are hiring a Staff engineer to set the technical direction of our web platform for the next three years.

What you'll do
- Own the architecture of our front-end platform: the React framework layer (React Server Components, routing, data loading), the build and monorepo tooling (Turborepo, Vite, Rust-based tooling), and the design system.
- Lead large migrations across hundreds of packages with codemods, incremental adoption and clear communication.
- Define performance standards (Core Web Vitals budgets, bundle governance) and build the tooling that enforces them.
- Drive accessibility and internationalisation as platform concerns with measurable conformance.
- Mentor senior engineers across teams; run the front-end architecture forum.
- Stay hands-on: build the hard parts, review critical changes, and prototype new approaches.

Requirements
- 10+ years of front-end engineering with 3+ years at a staff level leading platform work used by many teams.
- Deep expertise in React and its ecosystem, including server rendering, streaming, and data-fetching architectures.
- Experience owning build systems and monorepo tooling at scale; familiarity with JavaScript tooling written in Rust or Go.
- Track record of large, successful migrations and of measurable performance improvements.
- Strong understanding of accessibility, internationalisation and design systems.
- Excellent written communication and the ability to align many teams on a direction.

Nice to have
- Contributions to open-source front-end tooling or frameworks.
- Experience with edge rendering and CDN architectures.
- Node.js backend-for-frontend services and GraphQL.
- Public speaking or writing on front-end architecture.

Compensation
CAD 230,000 – 290,000 or USD 230,000 – 290,000 base plus equity, full benefits, and 5 weeks of vacation.

Orchard Commerce is an equal opportunity employer.""",
    ),
    SeedJd(
        role="staff_swe",
        key=key_for("staff_swe", "summit-data-platform"),
        title="Staff Software Engineer, Data Platform",
        company="Summit Mobility",
        text="""Staff Software Engineer, Data Platform
Summit Mobility · Data Platform
Location: Berlin, Germany (hybrid) or remote within the EU. EU work authorization required; relocation to Berlin supported.

About the role
Summit operates ride-hailing and micromobility in 40 European cities. Our data platform ingests trips, pricing, telematics and payments; it feeds operations dashboards, pricing models and regulatory reporting. We are hiring a Staff engineer to own the platform's architecture and lead its evolution: streaming-first ingestion, a lakehouse with governed data products, and the reliability our pricing models depend on.

What you'll do
- Own the data platform architecture across streaming (Kafka, Flink), lakehouse (Iceberg on S3, Trino, Spark) and orchestration (Airflow, dbt).
- Lead the move from batch pipelines to streaming-first with data contracts, schema evolution and backfill strategies.
- Define governance: data products with owners, quality SLOs, lineage, access control and GDPR-compliant retention.
- Guide four data engineering teams on design; review and unblock their hardest problems.
- Stay hands-on in Python, Java and SQL; prototype and benchmark new components.
- Partner with ML and finance teams on the datasets their models and reports depend on.

Requirements
- 10+ years of data or backend engineering with 3+ years at a staff level leading data platform architecture.
- Deep expertise in streaming systems (Kafka, Flink or Spark Structured Streaming) and lakehouse formats (Iceberg, Delta, Hudi).
- Strong Python and Java (or Scala) and advanced SQL; experience with Spark and Trino at scale.
- Experience establishing data governance and quality practices across an organisation.
- Track record of leading multi-team technical change with measurable outcomes.
- Excellent communication with engineers, analysts and executives.

Nice to have
- Mobility, logistics or marketplace domain experience.
- Kubernetes, Terraform and AWS cost optimisation for data workloads.
- Feature-store and ML-platform integration experience.
- German language skills.

Package
€140,000 – €175,000 base plus equity and bonus, 30 days of vacation, pension, and relocation support.

Summit Mobility is an equal opportunity employer.""",
    ),
)
