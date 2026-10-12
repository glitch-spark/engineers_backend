from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="senior_swe",
        key=key_for("senior_swe", "aster-payroll"),
        title="Senior Software Engineer (Backend-leaning, Go / PostgreSQL)",
        company="Aster Payroll",
        text="""Senior Software Engineer (Backend-leaning, Go / PostgreSQL)
Aster Payroll · Core Payroll
Location: Remote within the United States. Must be authorized to work in the US; we are unable to sponsor visas at this time.

About the role
Aster runs payroll for 30,000 small businesses. The Core Payroll team owns the calculation engine, tax filings and the ledger that makes sure every paycheck is right to the cent. We are hiring a senior engineer who can own a complex domain end to end, lead projects that span quarters, and raise the engineering bar for a team of eight.

What you'll do
- Design and build services in Go on PostgreSQL that compute pay, withholdings and employer taxes across 50 states, with full audit trails.
- Lead multi-quarter projects such as multi-state tax support or a new ledger model: write the design, split the work, and see it through to launch.
- Build integrations with government filing systems and banking partners; handle their failure modes gracefully.
- Set and uphold standards for testing (property-based tests for calculations, golden files for filings), observability and incident response.
- Mentor engineers through design reviews and pairing; help hire.
- Share on-call for the services you own and lead post-incident reviews.

Requirements
- 6+ years of professional software engineering, with a track record of owning complex systems in production.
- Strong backend experience in Go (or Java/Kotlin/C# with willingness to work in Go) and relational databases.
- Experience designing for correctness: idempotency, transactions, money handling, auditability.
- You have led projects spanning several engineers and months and can describe what you would do differently.
- Clear writing and the ability to simplify a messy domain for others.
- Experience in payroll, fintech, insurance or another correctness-critical domain is a plus.

Nice to have
- Event sourcing or ledger design experience.
- Kubernetes, Terraform and AWS.
- Experience integrating with government or banking APIs (ACH, IRS e-file).
- Front-end familiarity (React) for occasional full-stack work.

Compensation
$170,000 – $205,000 base plus equity and bonus. Medical, dental, vision, 401(k) match, 20 days PTO, and a home-office stipend.

Aster Payroll is an equal opportunity employer.""",
    ),
    SeedJd(
        role="senior_swe",
        key=key_for("senior_swe", "halcyon-product"),
        title="Senior Software Engineer, Product (Full Stack, TypeScript)",
        company="Halcyon",
        text="""Senior Software Engineer, Product (Full Stack, TypeScript)
Halcyon · Workspace
Location: London, UK (hybrid, 2 days per week) or remote within the UK. Right to work in the UK required; visa sponsorship available.

About Halcyon
Halcyon is a project-management product used by 25,000 teams. The Workspace team owns the core experience: boards, timelines, documents and the real-time collaboration under them. We want a senior engineer who can take a vague product goal, shape it with design, and ship it across the stack with quality.

What you'll do
- Build features end to end in TypeScript: React front end (Vite, Zustand, React Query), Node.js services (Fastify), PostgreSQL and Redis.
- Own real-time collaboration features (WebSockets, CRDT-backed documents) and their performance on large workspaces.
- Lead discovery with product and design; prototype, measure, iterate.
- Drive technical quality: architecture decisions, test strategy, performance budgets, accessibility.
- Mentor mid-level engineers and lead a project team of three to four people.
- Participate in a light on-call rotation.

Requirements
- 6+ years of software engineering with at least 4 years of TypeScript across front end and back end.
- Strong React skills and experience with Node.js services and PostgreSQL in production.
- Experience leading product projects from ambiguous goals to launch, working closely with designers.
- Experience with real-time features (WebSockets, collaboration) or performance work on complex UIs.
- Strong communication and a habit of writing things down.

Nice to have
- CRDT or OT experience (Yjs, Automerge).
- Experience with design systems and component libraries.
- Observability and performance tooling (Sentry, Datadog, Lighthouse).
- B2B SaaS experience with enterprise customers.

Package
£95,000 – £120,000 base plus equity, private medical, pension, 27 days of holiday, and a learning budget.

Halcyon is an equal opportunity employer.""",
    ),
    SeedJd(
        role="senior_swe",
        key=key_for("senior_swe", "pinecone-infra"),
        title="Senior Software Engineer, Infrastructure (Kubernetes / Go)",
        company="Pinecrest Health",
        text="""Senior Software Engineer, Infrastructure (Kubernetes / Go)
Pinecrest Health · Platform Engineering
Location: Minneapolis, MN (hybrid) or remote within the US. US work authorization required.

About the team
Pinecrest operates clinical software for 200 hospitals on a private cloud and AWS. Platform Engineering runs the Kubernetes platform, CI/CD, secrets, observability and the internal developer portal that 120 engineers use daily. We are hiring a senior engineer to lead the platform's next phase: multi-cluster, better self-service, and tighter security without slowing teams down.

What you'll do
- Design and operate the Kubernetes platform (EKS and on-prem), including networking, ingress, service mesh and policy (OPA/Kyverno).
- Build internal tooling in Go: operators, CLIs, and services that make deployments and environments self-serve.
- Own CI/CD (GitHub Actions, ArgoCD) and the golden paths teams follow; measure deployment frequency and lead time.
- Improve observability (Prometheus, Grafana, OpenTelemetry, Loki) and run SLO reviews with product teams.
- Lead security work: secrets management (Vault), image scanning, SBOMs, least-privilege IAM.
- Mentor engineers, lead design reviews, and take part in the platform on-call rotation.

Requirements
- 6+ years of software or infrastructure engineering with at least 3 years operating Kubernetes in production.
- Strong Go (or Python with Go willingness) and experience building operators or platform services.
- Deep understanding of cloud networking, IAM and infrastructure as code (Terraform).
- Experience with GitOps and CI/CD at scale, and with observability stacks.
- You have led platform initiatives across many teams and can show adoption, not only delivery.
- Experience in a regulated environment (HIPAA, SOC 2) is a plus.

Nice to have
- Service mesh (Istio, Linkerd) or Cilium experience.
- Backstage or another internal developer portal.
- Cost optimisation at scale (FinOps).
- On-prem virtualisation (VMware, Proxmox) or bare-metal Kubernetes.

Compensation
$160,000 – $195,000 base plus bonus, excellent health benefits, 401(k) match, 4 weeks of PTO, and a learning budget.

Pinecrest Health is an equal opportunity employer.""",
    ),
    SeedJd(
        role="senior_swe",
        key=key_for("senior_swe", "solano-mobile"),
        title="Senior Software Engineer, Mobile (React Native / Native Modules)",
        company="Solano Bank",
        text="""Senior Software Engineer, Mobile (React Native / Native Modules)
Solano Bank · Mobile Banking
Location: Madrid, Spain (hybrid, 2 days per week) or remote within Spain. EU work authorization required.

About the role
Solano is a digital bank with 2.5 million customers in Spain and Portugal. The Mobile Banking team owns the app: accounts, payments, cards, and the security features behind them. We are looking for a senior engineer who is strong in React Native and comfortable in native code, and who can lead a feature team of five.

What you'll do
- Build and own features in React Native with TypeScript, with native modules in Swift and Kotlin where needed (biometrics, secure storage, NFC).
- Lead the app's performance and reliability: startup time, crash-free rate above 99.9%, offline resilience.
- Implement strong customer authentication and secure flows in line with PSD2 and our security team's standards.
- Own release engineering: CI with Fastlane and GitHub Actions, phased rollouts, feature flags, and store compliance.
- Set engineering standards for the mobile guild: architecture, testing (Jest, Detox), code review.
- Mentor engineers and coordinate with backend teams on API contracts.

Requirements
- 6+ years of software engineering with 3+ years of React Native in production and real native experience on iOS or Android.
- Strong TypeScript and understanding of the React Native architecture (bridge/JSI, Hermes, new architecture).
- Experience with mobile security practices: secure storage, certificate pinning, biometrics, jailbreak detection.
- Experience leading feature teams and shipping through app-store review processes.
- Good communication in English; Spanish is a plus.

Nice to have
- Banking or fintech experience; familiarity with PSD2/SCA.
- Experience with native-only projects (Swift/SwiftUI or Kotlin/Compose).
- Accessibility on mobile.
- Observability for mobile (Sentry, Firebase Crashlytics, Datadog RUM).

Package
€75,000 – €95,000 base plus bonus, private health insurance, 25 days of vacation, flexible compensation, and a hybrid setup.

Solano Bank is an equal opportunity employer.""",
    ),
    SeedJd(
        role="senior_swe",
        key=key_for("senior_swe", "wren-data-products"),
        title="Senior Software Engineer, Data Products (Python / Services)",
        company="Wren Insights",
        text="""Senior Software Engineer, Data Products (Python / Services)
Wren Insights · Product Engineering
Location: Remote within Canada or the United States; team in the Pacific time zone. Must be legally authorized to work in Canada or the US.

About Wren
Wren sells market intelligence to consumer brands: dashboards and APIs built on billions of retail data points. The Product Engineering team turns data pipelines into products: APIs, exports, alerts and the services that keep them fast and correct. We need a senior engineer who is at home in Python services and comfortable with data at scale, and who can lead projects with the data engineering and front-end teams.

What you'll do
- Design and build Python services (FastAPI, SQLAlchemy, Celery) over PostgreSQL, ClickHouse and S3 that serve customer-facing APIs and exports.
- Own API design and versioning for enterprise customers; keep contracts documented and backward compatible.
- Optimise query performance for analytical workloads and build caching strategies that hold up under customer peaks.
- Lead cross-team projects: a new alerting product, a self-serve export builder, usage-based billing.
- Raise the bar on testing, observability (OpenTelemetry, Datadog) and operational readiness.
- Mentor engineers and run design reviews.

Requirements
- 6+ years of software engineering with strong Python in production services.
- Experience with analytical databases or large datasets (ClickHouse, BigQuery, Snowflake, Spark) and with API performance work.
- Experience leading projects across teams, from design document to launch and follow-up.
- Solid understanding of distributed systems basics: queues, retries, idempotency, caching.
- Experience with AWS, Docker and Kubernetes.
- Strong written communication.

Nice to have
- Experience with usage-based billing or entitlement systems.
- TypeScript/React familiarity for full-stack work.
- Data-engineering tooling (Airflow, dbt) familiarity.
- B2B SaaS with enterprise customers.

Compensation
USD 160,000 – 195,000 or CAD 165,000 – 200,000 base plus equity, full benefits, and 4 weeks of vacation.

Wren Insights is an equal opportunity employer.""",
    ),
)
