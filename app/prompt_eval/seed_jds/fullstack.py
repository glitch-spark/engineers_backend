from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="fullstack",
        key="seed:fullstack",
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
        role="fullstack",
        key=key_for("fullstack", "orbit-crm"),
        title="Full-Stack Engineer (Next.js / Python)",
        company="Orbit CRM",
        text="""Full-Stack Engineer (Next.js / Python)
Orbit CRM · Growth Engineering
Location: Berlin, Germany (hybrid, 3 days per week). Applicants need an existing right to work in Germany; relocation within the EU is supported.

About us
Orbit is a CRM for small B2B sales teams, used by 12,000 companies. The Growth Engineering team owns onboarding, the free-trial funnel, billing and the in-product experiments that move activation. We ship small and often, and we look at the numbers the next morning.

What you'll do
- Build features across the stack: a Next.js 14 front end (App Router, TypeScript, Tailwind) and a Python back end (FastAPI, SQLAlchemy, PostgreSQL).
- Own the self-serve billing flow with Stripe: subscriptions, proration, dunning, invoices, and the webhooks that keep our data right.
- Design A/B experiments with product and analytics, instrument them with Segment and Amplitude, and write up the results.
- Keep the onboarding funnel fast: Core Web Vitals budgets, server components, edge caching on Vercel.
- Write tests you would trust at 2 a.m.: Vitest for units, Playwright for the funnel, pytest for the API.
- Review pull requests from four teammates and help the two juniors on the team grow.

Requirements
- 4+ years of experience building and shipping web products, with real ownership of both front-end and back-end code.
- Strong TypeScript and React; production experience with Next.js or a comparable SSR framework.
- Solid Python with an async web framework (FastAPI, Starlette or Django with ASGI) and relational databases.
- You have worked with a payments provider in production and understand idempotency and webhook retries.
- Product sense: you ask what a feature is for and can suggest a cheaper way to learn the same thing.
- Good written English; German is a plus but not required.

Nice to have
- Experience with growth or experimentation tooling (feature flags, LaunchDarkly, Statsig, GrowthBook).
- Familiarity with event pipelines (Segment, Kafka) and warehouse tables for product analytics.
- Accessibility and localization experience (we ship in 6 languages).
- Prior work at a B2B SaaS company with a self-serve motion.

What we offer
€78,000 – €98,000 base plus virtual stock options, 28 days of vacation, a public-transport pass, a €1,500 learning budget, and a hybrid setup with a well-equipped office in Kreuzberg.

Orbit is an equal opportunity employer.""",
    ),
    SeedJd(
        role="fullstack",
        key=key_for("fullstack", "kestrel-logistics"),
        title="Software Engineer, Full Stack (Ruby on Rails / React)",
        company="Kestrel Logistics",
        text="""Software Engineer, Full Stack (Ruby on Rails / React)
Kestrel Logistics · Shipper Platform
Location: Chicago, IL (onsite 3 days a week) or remote in Central or Eastern time zones. We are not able to offer visa sponsorship for this position.

The role
Kestrel runs a freight marketplace that matches shippers with carriers across North America. The Shipper Platform team owns the web app shippers use to quote, book and track loads, and the APIs that larger customers integrate with. You will own features end to end: from the Rails model to the React screen to the dashboard that tells you whether it worked.

Responsibilities
- Build and maintain features in a large Ruby on Rails monolith (Rails 7, PostgreSQL, Sidekiq) and a React + TypeScript front end.
- Design REST endpoints used by enterprise integrations; keep them backward compatible and documented with OpenAPI.
- Improve quote and tracking performance: query tuning, caching with Redis, background jobs, and sensible pagination.
- Pair with product and design on weekly discovery; prototype quickly and throw prototypes away without regret.
- Participate in the team's on-call rotation (one week in five, business hours only for most alerts).
- Mentor newer engineers through code review and pairing.

What we're looking for
- 3+ years of professional experience with Ruby on Rails in production, plus 2+ years with React.
- Comfortable with PostgreSQL: schema design, indexes, migrations on large tables, and reading EXPLAIN output.
- Experience with background job systems and queue-based workflows (Sidekiq, Resque or similar).
- Testing habits: RSpec, factory-based fixtures, and front-end tests with Testing Library.
- You communicate clearly in writing and are comfortable talking directly with customers' engineers.

Nice to have
- Logistics, supply chain or marketplace experience.
- GraphQL (we are evaluating it for the next version of the API).
- Infrastructure familiarity: AWS ECS, Terraform, Datadog.
- Experience migrating parts of a monolith into services and opinions about when not to.

Compensation
$140,000 – $170,000 base plus annual bonus and equity. Health, dental and vision, 401(k) match, 18 days PTO plus company holidays, and commuter benefits.

Kestrel Logistics is an equal opportunity employer.""",
    ),
    SeedJd(
        role="fullstack",
        key=key_for("fullstack", "bluefin-edtech"),
        title="Senior Full Stack Developer (Vue / Laravel)",
        company="Bluefin Learning",
        text="""Senior Full Stack Developer (Vue / Laravel)
Bluefin Learning · Classroom Products
Location: Remote, Australia or New Zealand. Candidates must hold working rights in Australia or New Zealand.

About Bluefin
Bluefin builds classroom software used by 4,000 schools: lesson planning, assessment and parent communication. The Classroom Products team owns the teacher-facing web app and the APIs behind it. School terms set our rhythm; a bad release in week one of term is felt by thousands of teachers.

What you will do
- Build teacher-facing features in Vue 3 (Composition API, TypeScript, Pinia) and a Laravel 10 back end on MySQL and Redis.
- Own the assessment module: rubric editing, grading workflows, and exports to school information systems.
- Lead the migration of our remaining Vue 2 and Blade screens to the component library, with codemods where they pay off.
- Improve observability and reliability: structured logging, Sentry, Laravel Horizon queues, and sensible alerts.
- Partner with a product manager and a designer; run short discovery sessions with teachers.
- Set technical direction for a team of five and review most of the team's pull requests.

Requirements
- 6+ years of web development experience, at least 3 of them with Laravel (or a comparable PHP framework) in production.
- Strong Vue (2 and 3) and TypeScript; you have led a front-end migration or a design-system adoption.
- Solid MySQL skills: schema evolution on large tables, query performance, and data integrity.
- Experience with queues, caching and third-party integrations (OAuth, SAML, webhooks).
- You write clear technical proposals and can disagree constructively.
- Experience in education technology or another domain with strict data-privacy expectations.

Nice to have
- AWS (ECS or Elastic Beanstalk, RDS, SQS, CloudFront) and infrastructure as code.
- Experience with accessibility requirements in government or education procurement (WCAG 2.1 AA).
- Mobile web performance work on low-end devices.

Package
AUD 150,000 – 180,000 inclusive of superannuation, employee share scheme, 5 weeks of annual leave, and a fully remote setup with quarterly meetups in Melbourne.

Bluefin is an equal opportunity employer and values diversity in its team.""",
    ),
    SeedJd(
        role="fullstack",
        key=key_for("fullstack", "sable-fintech"),
        title="Full-Stack Engineer, Core Banking Experience (TypeScript / NestJS)",
        company="Sable",
        text="""Full-Stack Engineer, Core Banking Experience (TypeScript / NestJS)
Sable · Member Experience
Location: New York, NY or remote in the United States. Must be authorized to work in the US; H-1B transfers considered.

Who we are
Sable is a mobile-first bank for immigrants and international students. The Member Experience team owns the web and mobile account surfaces: onboarding, transfers, cards, and support tooling. We work with a regulated partner bank, so correctness and auditability are part of every feature.

What you'll do
- Ship features across a TypeScript monorepo: React (web), React Native (mobile) and NestJS services on PostgreSQL, deployed on AWS with Kubernetes.
- Build the transfers experience: domestic ACH, international wires via a partner API, scheduled transfers, and limits.
- Design APIs with strong typing end to end (tRPC or OpenAPI-generated clients) and shared validation with Zod.
- Implement audit trails, feature flags and kill switches so a bad release can be contained in minutes.
- Work with compliance on KYC flows and with support on internal tools that reduce ticket volume.
- Take part in an on-call rotation for the services you own.

What we're looking for
- 4+ years of professional software engineering in TypeScript across front end and back end.
- Experience with React and at least one of React Native or a comparable mobile framework.
- Experience building services with NestJS, Express or Fastify, and PostgreSQL in production.
- You have worked in a regulated environment (fintech, health, insurance) or can describe how you would design for auditability.
- Strong testing discipline (Jest, Playwright or Detox) and comfort with CI/CD on GitHub Actions.
- Clear communicator; you write good incident reports.

Nice to have
- Payments experience: ACH, wires, card issuing (Marqeta, Lithic), or partner-bank integrations.
- Kubernetes, Terraform, and observability with Datadog or Honeycomb.
- Spanish, Hindi or Mandarin (our members are multilingual).

Compensation
$150,000 – $185,000 base plus equity, full medical, dental and vision, 401(k), 20 days of PTO, and a home-office stipend.

Sable is an equal opportunity employer.""",
    ),
)
