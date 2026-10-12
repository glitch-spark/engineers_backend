from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="frontend",
        key="seed:frontend",
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
        role="frontend",
        key=key_for("frontend", "pelican-travel"),
        title="Senior Frontend Engineer (React / Next.js)",
        company="Pelican Travel",
        text="""Senior Frontend Engineer (React / Next.js)
Pelican Travel · Booking Experience
Location: Lisbon, Portugal (hybrid) or remote within Portugal and Spain. EU work authorization required.

About the team
Pelican sells flights and hotels to 9 million travellers a year. The Booking Experience team owns the search results, the checkout funnel and the post-booking pages, on web and mobile web. Our pages are among the most visited in the company, and a 100 ms regression shows up in revenue the same week.

What you'll do
- Build and own the checkout funnel in React 18 and Next.js with TypeScript, deployed on our own Kubernetes platform.
- Drive performance work: server-side rendering strategy, streaming, bundle budgets, image pipelines, and Core Web Vitals monitoring in the field.
- Run experiments with the growth team and make sure variants are statistically clean and accessible.
- Harden the funnel against the real world: flaky payment providers, slow networks, old Android browsers.
- Define front-end standards with the other senior engineers: linting, testing, component API conventions.
- Mentor mid-level engineers and lead technical design for multi-sprint projects.

Requirements
- 5+ years of front-end engineering, with 3+ years of React in high-traffic production applications.
- Deep understanding of browser performance: rendering, the event loop, caching, and how to measure all of it.
- Experience with SSR frameworks (Next.js or Remix) and with CDNs and edge caching.
- TypeScript proficiency and the discipline to keep a large codebase typed.
- Experience with A/B testing at scale and with analytics instrumentation.
- Accessibility experience with WCAG 2.1 AA in consumer products.

Nice to have
- Travel, e-commerce or marketplace background.
- Experience with payments UIs (3-D Secure, wallets, PCI considerations).
- Familiarity with Node.js BFF services or GraphQL gateways.
- Experience with design systems or Storybook-driven development.

What we offer
€70,000 – €90,000 base plus bonus, private health insurance, 25 days of holiday, travel credits, and a hybrid office in central Lisbon.

Pelican Travel is an equal opportunity employer.""",
    ),
    SeedJd(
        role="frontend",
        key=key_for("frontend", "quill-docs"),
        title="Frontend Engineer, Editor (TypeScript, ProseMirror)",
        company="Quill",
        text="""Frontend Engineer, Editor (TypeScript, ProseMirror)
Quill · Editor Team
Location: Remote, United States or Canada. Must be legally authorized to work in the US or Canada.

About the role
Quill is a collaborative documentation tool used by engineering teams. The Editor team owns the rich-text editor: real-time collaboration, comments, tables, embeds, and the plugin system third parties build on. This is deep front-end engineering: data structures, performance, and browser quirks, not CRUD screens.

What you'll do
- Extend our ProseMirror-based editor in TypeScript: new node types, input rules, keyboard handling, IME and RTL support.
- Work on real-time collaboration with Yjs (CRDTs): conflict handling, presence, offline edits, undo across users.
- Keep the editor fast on 200-page documents: profiling, virtualization, incremental rendering.
- Maintain the public plugin API and its documentation; review community plugins.
- Build the comment and suggestion flows with design, including accessibility for screen readers.
- Write thorough tests: unit tests for transforms, Playwright tests for typing-level behaviour across browsers.

Requirements
- 4+ years of front-end engineering with advanced TypeScript.
- Hands-on experience with a rich-text editor framework (ProseMirror, Tiptap, Lexical, Slate or CodeMirror) in production.
- Strong understanding of the DOM, selection and range APIs, contenteditable pitfalls, and browser differences.
- Experience with real-time collaboration (CRDTs or OT) or a strong desire and the fundamentals to learn it quickly.
- Comfortable with React for the surrounding UI, though most of the work is framework-agnostic.
- Clear technical writing; you will document APIs other developers depend on.

Nice to have
- Open-source contributions to editor or collaboration libraries.
- Experience with WebSockets at scale or with service workers and offline-first apps.
- Familiarity with accessibility testing for complex widgets (ARIA grid, live regions).

Compensation
$160,000 – $200,000 USD base plus equity. Health coverage, 401(k)/RRSP matching, unlimited PTO with a 3-week minimum, a $2,500 equipment budget, and a yearly team offsite.

Quill is an equal opportunity employer.""",
    ),
    SeedJd(
        role="frontend",
        key=key_for("frontend", "helio-analytics"),
        title="Frontend Developer, Data Visualization (Angular / D3)",
        company="Helio Analytics",
        text="""Frontend Developer, Data Visualization (Angular / D3)
Helio Analytics · Dashboards
Location: Toronto, ON (hybrid, 2 days in office). Candidates must be eligible to work in Canada.

About Helio
Helio builds energy-monitoring dashboards for commercial buildings. Facility managers use our product to spot waste, verify savings and report on sustainability targets. The Dashboards team builds the charts, maps and report builders that make 15 years of meter data understandable.

Responsibilities
- Build interactive visualizations with D3 and Canvas/SVG inside an Angular 17 application (standalone components, signals, RxJS).
- Own the report builder: drag-and-drop layouts, saved views, scheduled PDF exports.
- Render large time series smoothly: downsampling, WebGL where it pays off, workers for heavy transforms.
- Collaborate with designers on a visual language for charts that works in light and dark modes and in print.
- Improve accessibility of charts: keyboard navigation, data tables as alternatives, colorblind-safe palettes.
- Write unit tests with Jest and component tests with Cypress; maintain Storybook stories for every chart.

What we're looking for
- 3+ years of front-end development, with at least 2 years in Angular (v12 or later) and TypeScript.
- Real experience building custom charts with D3 or a comparable low-level library, not only configuring a charting package.
- Comfort with RxJS and reactive state management.
- Understanding of rendering performance in the browser and when to use Canvas or WebGL.
- An eye for visual detail and the patience to get alignment and typography right.

Nice to have
- Experience with time-series data, IoT or energy domains.
- Mapbox or deck.gl for geospatial views.
- Familiarity with print and PDF generation from web content.
- Angular Material or a design-system migration.

What we offer
CAD 95,000 – 120,000 base plus bonus, extended health and dental from day one, RRSP matching, 3 weeks of vacation, and a professional development allowance.

Helio Analytics is an equal opportunity employer.""",
    ),
    SeedJd(
        role="frontend",
        key=key_for("frontend", "marrow-health"),
        title="Senior UI Engineer, Patient Portal (React / Accessibility)",
        company="Marrow Health",
        text="""Senior UI Engineer, Patient Portal (React / Accessibility)
Marrow Health · Patient Experience
Location: Remote within the United Kingdom. Right to work in the UK required; we do not sponsor visas for this role.

Why this role exists
Marrow's patient portal is used by 2 million NHS patients to book appointments, read results and message clinicians. Many of our users are older, use assistive technology, or are on low-end phones with poor connectivity. We need a senior engineer who treats accessibility and resilience as core engineering, not a checklist.

What you'll do
- Lead front-end development of the portal in React and TypeScript (Vite, React Router, React Query).
- Own accessibility: WCAG 2.2 AA conformance, audits with assistive technology users, and the component patterns that keep new features compliant.
- Build offline-tolerant flows with service workers and careful error states; make the app usable on a 3G connection.
- Work with clinical safety and information governance on features that touch patient data.
- Establish front-end testing: Testing Library, Playwright with axe checks, visual regression.
- Mentor three engineers and set standards across the front-end guild.

Requirements
- 5+ years of front-end engineering with at least 3 years of React and TypeScript in production.
- Demonstrated accessibility expertise: you have led conformance work and tested with screen readers (NVDA, JAWS, VoiceOver) and switch access.
- Experience building resilient, performance-conscious apps for low-end devices and unreliable networks.
- Comfortable with REST and FHIR-style APIs; you can read an API spec and propose changes.
- Experience in a regulated or safety-critical domain, ideally healthcare or public sector.
- Excellent communication with non-engineers, including clinicians and user researchers.

Nice to have
- GOV.UK Design System or NHS Design System experience.
- Contributions to accessibility tooling or standards work.
- Familiarity with Node.js BFFs and authentication flows (OAuth 2, NHS Login).

Package
£80,000 – £95,000 base, 8% pension, private medical insurance, 27 days of holiday plus bank holidays, and a remote-first setup with quarterly in-person days in Manchester.

Marrow Health is an equal opportunity employer.""",
    ),
)
