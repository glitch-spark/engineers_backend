from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="ai",
        key="seed:ai",
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
        role="ai",
        key=key_for("ai", "clearpath-legal"),
        title="AI Engineer, Document Intelligence (Python / LLM APIs)",
        company="Clearpath Legal",
        text="""AI Engineer, Document Intelligence (Python / LLM APIs)
Clearpath Legal · Product AI
Location: London, UK (hybrid, 2 days per week). Right to work in the UK required; visa sponsorship available.

About the role
Clearpath automates contract review for in-house legal teams at 400 companies. The Product AI team builds the features that read, classify and summarise contracts, flag risky clauses and draft redlines. You will take ideas from a prototype to a feature with measured accuracy, in production, with lawyers watching.

What you'll do
- Design LLM pipelines for clause extraction, classification and redline generation using the Anthropic and OpenAI APIs and open-weight models where cost matters.
- Build the evaluation harness: labelled clause sets, precision/recall per clause type, drift monitoring, and human review loops.
- Implement document processing: OCR cleanup, layout-aware chunking for long contracts, table handling, citation back to the source span.
- Ship with the product team in Python (FastAPI, PostgreSQL, Celery) and a React front end; own latency and cost budgets.
- Instrument prompts and models: versioning, A/B comparisons, token spend dashboards.
- Explain model behaviour and limits to legal subject-matter experts and incorporate their feedback.

Requirements
- 3+ years of software engineering in Python with 1+ year building LLM-powered features used in production.
- Experience with retrieval and document processing (chunking strategies, embeddings, rerankers, OCR output).
- You have built an evaluation set and used it to make a ship decision; you can describe its blind spots.
- Solid backend fundamentals: APIs, queues, databases, observability, deployment on AWS or GCP.
- Clear communication with non-engineers; comfortable presenting findings to customers.

Nice to have
- Fine-tuning or distillation of small models for classification tasks.
- Experience with structured outputs, JSON schema constraints and function calling.
- Legal, compliance or other document-heavy domain experience.
- Familiarity with LLM observability tools (LangSmith, Braintrust, Langfuse).

Package
£90,000 – £115,000 base plus equity, private healthcare, pension, 25 days of holiday, and a learning budget.

Clearpath Legal is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ai",
        key=key_for("ai", "beacon-support"),
        title="Applied AI Engineer, Customer Support Automation",
        company="Beacon",
        text="""Applied AI Engineer, Customer Support Automation
Beacon · AI Agents
Location: Remote within the United States; quarterly travel to Denver. US work authorization required.

About Beacon
Beacon is a customer support platform used by 2,000 e-commerce brands. The AI Agents team builds the assistant that resolves tickets end to end: it reads the conversation, looks up orders, issues refunds within policy, and hands off to a human when it should. Today it resolves 38% of tickets; your job is to raise that number without raising the complaint rate.

What you'll do
- Build agent workflows in Python: tool calling against the Beacon API and merchants' systems (Shopify, Stripe, carriers), guardrails, and escalation logic.
- Own the offline and online evaluation loop: replay sets from real conversations, LLM-as-judge with human calibration, resolution-rate and CSAT monitoring per merchant.
- Reduce cost and latency with caching, model routing (small model first, escalate when uncertain) and prompt compression.
- Design the knowledge layer: retrieval over merchant policies and macros, freshness, and citations the agent must show.
- Partner with product on the merchant-facing controls: what the agent may do, confidence thresholds, review queues.
- Monitor production behaviour and run weekly quality reviews with the support operations team.

Requirements
- 3+ years of backend or ML engineering in Python; 1+ year shipping LLM agents or assistants to real users.
- Hands-on experience with tool/function calling, multi-step agent loops and the failure modes of each (loops, hallucinated tools, partial actions).
- Experience with retrieval systems and evaluation of RAG quality.
- Strong product judgement: you can argue for not automating something.
- Comfortable with PostgreSQL, Redis, queues and deploying services on AWS.

Nice to have
- Customer support or e-commerce domain experience.
- Experience with conversation analytics or intent classification at scale.
- Familiarity with the Anthropic and OpenAI SDKs, streaming, and structured outputs.
- Experience fine-tuning models for classification or routing.

Compensation
$165,000 – $200,000 base plus equity and bonus. Medical, dental, vision, 401(k) match, unlimited PTO with a 15-day minimum.

Beacon is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ai",
        key=key_for("ai", "atlas-search"),
        title="Senior AI Engineer, Search & Retrieval",
        company="Atlas Knowledge",
        text="""Senior AI Engineer, Search & Retrieval
Atlas Knowledge · Search Platform
Location: Dublin, Ireland or remote within the EU. Must have the right to work in the EU.

About Atlas
Atlas is an enterprise knowledge assistant deployed inside large companies: it answers employee questions from wikis, tickets, documents and chat history with citations. The Search Platform team owns the retrieval stack that every answer depends on: ingestion connectors, indexing, hybrid search, reranking, and the evaluation framework that tells us when relevance moves.

What you'll do
- Design and operate hybrid retrieval: BM25 and dense vectors (pgvector, OpenSearch), late-interaction models, cross-encoder rerankers.
- Build connectors and incremental ingestion for Confluence, SharePoint, Google Drive, Slack and Jira with permissions preserved at query time.
- Develop evaluation sets per customer (click logs, human judgements, synthetic queries) and run relevance experiments.
- Tune chunking, metadata and query rewriting for long technical documents and multilingual content.
- Own the latency budget: p95 under 800 ms for retrieval across tens of millions of chunks.
- Lead technical design across the team and mentor two engineers.

Requirements
- 5+ years of engineering experience with at least 2 years on search or retrieval systems in production.
- Strong Python; experience with Elasticsearch/OpenSearch or a vector database at scale.
- Practical knowledge of embedding models, rerankers and evaluation metrics (nDCG, MRR, recall@k).
- Experience with permission-aware retrieval and the security implications of enterprise data.
- Solid systems skills: Kubernetes, queues, PostgreSQL, observability.
- Experience running A/B tests or interleaving experiments for relevance.

Nice to have
- Fine-tuning embedding or reranking models on domain data.
- Experience with LLM-based query understanding and answer synthesis.
- Java or Go for the parts of the indexing pipeline written in them.
- Publications or talks on information retrieval.

Package
€95,000 – €125,000 base plus equity, health insurance, pension, 25 days of holiday, and a remote-first setup with a Dublin office.

Atlas Knowledge is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ai",
        key=key_for("ai", "ridgeline-health"),
        title="AI Engineer, Clinical Workflows (Python / FHIR / LLMs)",
        company="Ridgeline Health",
        text="""AI Engineer, Clinical Workflows (Python / FHIR / LLMs)
Ridgeline Health · Clinical AI
Location: Boston, MA (hybrid, 3 days per week). Must be authorized to work in the US; no sponsorship available for this role.

About the team
Ridgeline builds software for care teams in 80 hospitals: visit summaries, prior-authorization drafting, and inbox triage. The Clinical AI team turns language models into features clinicians trust, with evaluation, safety reviews and auditability built in from the first commit.

What you'll do
- Build LLM features in Python: ambient visit summarisation from transcripts, structured extraction into FHIR resources, and draft generation for prior authorizations.
- Design the evaluation pipeline with clinical reviewers: rubric-based grading, error taxonomies, and regression tests that gate releases.
- Implement safety controls: PHI handling, prompt-injection defences, confidence thresholds, and human-in-the-loop review.
- Integrate with EHR systems through FHIR APIs and HL7 feeds; handle messy real-world clinical data.
- Work with security and compliance on HIPAA controls and model vendor agreements (BAAs).
- Monitor cost, latency and quality in production and run post-deployment reviews.

Requirements
- 3+ years of software engineering in Python with experience shipping LLM-based features.
- Experience with evaluation design for generative outputs and with human review workflows.
- Familiarity with healthcare data standards (FHIR, HL7v2, SNOMED/ICD codes) or willingness to learn them quickly.
- Backend fundamentals: FastAPI or Django, PostgreSQL, queues, AWS deployment.
- You take safety and privacy seriously and can explain trade-offs to clinicians and compliance.

Nice to have
- Speech-to-text pipelines or working with transcripts.
- Fine-tuning or distillation for medical text tasks.
- Experience in a HIPAA-regulated environment.
- Clinical background or experience working closely with clinicians.

Compensation
$160,000 – $195,000 base plus equity, comprehensive health benefits, 401(k) match, 20 days of PTO, and a learning budget.

Ridgeline Health is an equal opportunity employer.""",
    ),
)
