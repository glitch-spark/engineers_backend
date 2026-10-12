from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="test",
        key="seed:test",
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
    SeedJd(
        role="test",
        key=key_for("test", "ember-mobile"),
        title="Mobile QA Automation Engineer (iOS / Android / Appium)",
        company="Ember Fitness",
        text="""Mobile QA Automation Engineer (iOS / Android / Appium)
Ember Fitness · Mobile Quality
Location: Austin, TX (hybrid) or remote in the US. Must be authorized to work in the US.

About Ember
Ember is a fitness app with 6 million members and live classes streamed to phones, tablets and TVs. The Mobile Quality team keeps our weekly iOS and Android releases safe: automation on real devices, performance checks on streaming, and exploratory testing of new features before they reach the beta channel.

What you'll do
- Build and maintain mobile automation in TypeScript or Kotlin with Appium, Detox and Maestro, running on a real-device cloud (BrowserStack or Firebase Test Lab).
- Automate critical journeys: sign-up, subscription purchase (StoreKit and Play Billing sandboxes), class playback, offline downloads.
- Measure performance: app start time, playback start, memory and battery during long sessions; alert on regressions.
- Integrate suites into GitHub Actions with sharding and flaky-test quarantine; keep the pipeline under 25 minutes.
- Run exploratory testing sessions and bug bashes before major releases; write clear bug reports with reproductions.
- Mentor two manual testers transitioning into automation.

Requirements
- 4+ years in mobile QA with at least 2 years building automation for both iOS and Android.
- Hands-on experience with Appium plus at least one of Detox, Maestro, XCUITest or Espresso.
- Strong scripting skills in TypeScript, Kotlin or Swift; comfortable reading app code.
- Experience with in-app purchase testing and streaming or media playback.
- Understanding of CI/CD for mobile (Fastlane, code signing, device farms).
- Good product sense and the ability to prioritise risk.

Nice to have
- Experience testing on TV platforms (tvOS, Android TV, Fire TV).
- Accessibility testing on mobile (VoiceOver, TalkBack).
- Performance tooling (Xcode Instruments, Android Profiler, Perfetto).
- Experience with analytics event validation.

Compensation
$125,000 – $155,000 base plus equity, health benefits, 401(k) match, 20 days of PTO, and a free Ember membership for your household.

Ember Fitness is an equal opportunity employer.""",
    ),
    SeedJd(
        role="test",
        key=key_for("test", "ironclad-sdet"),
        title="Software Development Engineer in Test (Java / Microservices)",
        company="Ironclad Insurance",
        text="""Software Development Engineer in Test (Java / Microservices)
Ironclad Insurance · Platform Quality
Location: Hartford, CT (hybrid, 2 days per week) or remote in the US. Must be authorized to work in the US without sponsorship.

About the role
Ironclad's policy platform is 60 Java microservices processing quotes, binds and claims for 3 million policies. The Platform Quality team builds the test infrastructure that lets 15 product teams release daily: contract tests, service virtualisation, synthetic data, and the pipelines that run it all. You will write code most of the day; the product is the test platform.

What you'll do
- Build test frameworks in Java (JUnit 5, REST Assured, Testcontainers) and Kotlin for service, contract and integration testing.
- Implement consumer-driven contract testing with Pact across teams and make it part of every pipeline.
- Build service virtualisation (WireMock, Hoverfly) for third-party rating engines and payment providers.
- Design synthetic test data generation that respects policy rules and privacy.
- Improve CI/CD (Jenkins, GitHub Actions, ArgoCD on Kubernetes): parallelisation, test selection, flaky-test analytics.
- Coach product teams on testing strategy and review their test code.

What we're looking for
- 5+ years of software engineering or SDET experience with strong Java; you can build a framework, not only use one.
- Experience testing microservices: REST and messaging (Kafka, JMS), contract testing, environment strategies.
- Experience with Docker, Kubernetes and CI/CD pipelines as a builder, not only a consumer.
- Solid SQL and experience with test data management.
- Understanding of observability (logs, traces) to diagnose failures across services.
- Experience in insurance, banking or another regulated domain is a plus.

Nice to have
- Performance testing (Gatling, k6) and chaos engineering.
- Security testing basics (OWASP ZAP, dependency scanning).
- Python or Go for tooling.
- Experience with mainframe or legacy system integration testing.

Compensation
$135,000 – $165,000 base plus bonus, pension and 401(k), comprehensive health benefits, and 4 weeks of PTO.

Ironclad Insurance is an equal opportunity employer.""",
    ),
    SeedJd(
        role="test",
        key=key_for("test", "glasswing-perf"),
        title="Performance Test Engineer (k6 / Gatling / Observability)",
        company="Glasswing Ticketing",
        text="""Performance Test Engineer (k6 / Gatling / Observability)
Glasswing Ticketing · Reliability
Location: Remote within the UK or Ireland; occasional travel to London. Right to work in the UK or Ireland required.

About Glasswing
Glasswing sells tickets for concerts and sports; a major on-sale brings 2 million people to the site in the first minute. The Reliability team makes sure the platform survives those minutes. We need an engineer who owns performance testing as a discipline: realistic load models, repeatable experiments, and findings engineers act on.

What you'll do
- Design load models from production traffic (queueing behaviour, bot patterns, payment surges) and implement them in k6 and Gatling.
- Run large-scale distributed load tests against pre-production and, carefully, production (dark traffic, canaries).
- Analyse results with Grafana, Prometheus and distributed tracing (Tempo, Jaeger); find bottlenecks across services, databases and CDN.
- Automate performance regression checks in CI for critical services with clear pass/fail budgets.
- Work with service owners on capacity planning and on fixes (caching, connection pools, query tuning).
- Lead the performance readiness review before every major on-sale.

Requirements
- 4+ years of performance or reliability engineering with hands-on k6, Gatling, JMeter or Locust at significant scale.
- Strong understanding of web architecture: CDNs, load balancers, queues, databases, and how each fails under load.
- Scripting in JavaScript/TypeScript, Scala or Python; comfortable in Linux and containers.
- Experience with observability stacks (Prometheus, Grafana, OpenTelemetry) and reading traces.
- Clear, data-driven reporting; you can make a convincing case for a fix.

Nice to have
- Experience with virtual waiting rooms or queueing systems.
- Kubernetes and autoscaling behaviour under burst load.
- Chaos engineering tools (Gremlin, Litmus).
- Experience with payment-provider load constraints and sandbox limits.

Package
£70,000 – £90,000 base plus bonus, pension, private medical, 25 days of holiday, and tickets to events.

Glasswing Ticketing is an equal opportunity employer.""",
    ),
    SeedJd(
        role="test",
        key=key_for("test", "cedar-web"),
        title="QA Engineer, Web Platform (Cypress / TypeScript / Accessibility)",
        company="Cedar Commerce",
        text="""QA Engineer, Web Platform (Cypress / TypeScript / Accessibility)
Cedar Commerce · Storefront
Location: Vancouver, BC (hybrid) or remote within Canada. Must be eligible to work in Canada.

About the role
Cedar provides storefront software for 8,000 online retailers. The Storefront team ships theme and checkout changes weekly to every merchant at once, so a regression is immediately visible on thousands of sites. We are hiring a QA engineer who combines automation with sharp exploratory testing and an accessibility mindset.

What you'll do
- Build and maintain the end-to-end suite in Cypress and TypeScript (migration to Playwright is on the roadmap; you can lead it).
- Automate checkout flows across payment methods, currencies and tax regimes, with test merchants seeded per scenario.
- Integrate accessibility checks (axe) and visual regression (Percy or Applitools) into CI.
- Plan and run exploratory testing on new theme features; triage and write high-quality bug reports.
- Define test strategy with developers: what to cover at unit, component and end-to-end level.
- Monitor production quality signals (error rates, conversion drops) and feed them back into test coverage.

Requirements
- 3+ years in web QA with at least 2 years building automation in Cypress or Playwright with TypeScript or JavaScript.
- Experience testing e-commerce or payment flows, including third-party sandboxes.
- Working knowledge of accessibility testing (WCAG, screen readers) and of cross-browser issues.
- Experience with CI pipelines (GitHub Actions or CircleCI), parallelisation and flaky-test management.
- Strong written communication and a curious, risk-based approach to testing.

Nice to have
- Experience leading a Cypress-to-Playwright migration.
- API testing with Postman or a code-first tool.
- Basic performance testing (Lighthouse CI, WebPageTest).
- Familiarity with Shopify, BigCommerce or similar platforms.

Compensation
CAD 90,000 – 115,000 base plus equity, extended health and dental, RRSP matching, 4 weeks of vacation, and a home-office budget.

Cedar Commerce is an equal opportunity employer.""",
    ),
)
