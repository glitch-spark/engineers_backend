from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="data",
        key="seed:data",
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
        role="data",
        key=key_for("data", "brightwater-utilities"),
        title="Data Engineer (Python / Spark / Databricks)",
        company="Brightwater Utilities",
        text="""Data Engineer (Python / Spark / Databricks)
Brightwater Utilities · Data Platform
Location: Denver, CO (hybrid, 2 days per week). Must be authorized to work in the United States.

About the team
Brightwater supplies water and power to 1.4 million customers. The Data Platform team ingests smart-meter readings (300 million per day), outage events, billing and customer data into a lakehouse that serves operations, regulators and the data science team. Reliability and lineage matter: our numbers end up in rate cases.

What you'll do
- Build batch and streaming pipelines with PySpark on Databricks (Delta Lake, Unity Catalog), Kafka and Auto Loader.
- Model meter and billing data into well-documented gold tables; own data contracts with source teams.
- Implement data quality checks (Great Expectations or Delta constraints), freshness SLAs and lineage.
- Optimise cost and performance: partitioning, Z-ordering, cluster policies, job scheduling.
- Build the ingestion framework in Python with tests and CI/CD (GitHub Actions, Terraform on Azure).
- Support analysts and scientists with reliable datasets and clear documentation.

Requirements
- 3+ years of data engineering experience with Spark (PySpark) in production.
- Strong SQL and Python; experience with Delta Lake or a comparable lakehouse format.
- Experience with streaming ingestion (Kafka, Event Hubs, Kinesis) and late-arriving data.
- Understanding of data modelling for analytics and of data quality practices.
- Comfortable with cloud platforms (Azure preferred) and infrastructure as code.
- Experience in a regulated or utility environment is a plus.

Nice to have
- dbt on Databricks, Airflow or Databricks Workflows.
- Time-series or IoT data experience.
- Power BI semantic models.
- Data governance tooling (Purview, Collibra).

Compensation
$120,000 – $150,000 base plus bonus, pension plan, medical and dental, 4 weeks of PTO, and tuition reimbursement.

Brightwater Utilities is an equal opportunity employer.""",
    ),
    SeedJd(
        role="data",
        key=key_for("data", "sprocket-analytics"),
        title="Analytics Engineer (dbt / BigQuery / Looker)",
        company="Sprocket",
        text="""Analytics Engineer (dbt / BigQuery / Looker)
Sprocket · Data
Location: Remote within Europe. Must be eligible to work in an EU or EEA country, Switzerland or the UK.

About Sprocket
Sprocket is a subscription-box company shipping to 11 countries. The Data team is small (six people) and owns everything from ingestion to dashboards. We are hiring an analytics engineer to own the modelling layer so that finance, marketing and operations trust the same numbers.

What you'll do
- Own the dbt project on BigQuery: staging, intermediate and mart models, tests, documentation, and the CI that runs them.
- Model subscription metrics (MRR, churn, cohorts, LTV) and marketing attribution with finance and growth.
- Build and maintain the Looker semantic layer (LookML) and self-serve explores; retire ad-hoc dashboards.
- Manage ingestion with Fivetran and Airbyte; write custom Python extractors where needed.
- Set up data quality monitoring (dbt tests, Elementary) and alerting on freshness.
- Teach analysts SQL best practices and review their models.

Requirements
- 3+ years in analytics engineering or data engineering with deep dbt experience in production.
- Advanced SQL on BigQuery (or Snowflake/Redshift) including performance and cost awareness.
- Experience with a BI semantic layer (Looker, Lightdash, Metabase models) and dashboard design.
- Understanding of subscription business metrics and marketing attribution.
- Python for scripting and extractors.
- Clear communication with non-technical stakeholders.

Nice to have
- Airflow or Dagster orchestration.
- Experience with reverse ETL (Hightouch, Census).
- E-commerce or subscription domain background.
- Terraform for data infrastructure.

What we offer
€60,000 – €80,000 base, equity, 27 days of holiday, a remote work budget, and a monthly Sprocket box.

Sprocket is an equal opportunity employer.""",
    ),
    SeedJd(
        role="data",
        key=key_for("data", "quasar-streaming"),
        title="Senior Data Engineer, Streaming (Kafka / Flink / Java)",
        company="Quasar Adtech",
        text="""Senior Data Engineer, Streaming (Kafka / Flink / Java)
Quasar Adtech · Real-Time Data
Location: New York, NY (hybrid, 3 days per week) or remote in the US Eastern time zone. US work authorization required.

About the team
Quasar runs a demand-side platform that bids on 2 million ad impressions per second. The Real-Time Data team owns the event pipelines that count impressions, clicks and conversions, enforce budgets within seconds, and feed the ML models that set bids. Exactly-once counting is a feature customers pay for.

What you'll do
- Design and operate streaming pipelines on Apache Flink and Kafka in Java (and some Scala), with Avro/Protobuf schemas and a schema registry.
- Build exactly-once aggregation for budgets and pacing with low end-to-end latency.
- Own the Kafka platform: multi-region clusters, tiered storage, consumer lag SLOs, and capacity planning.
- Land events into the lakehouse (Iceberg on S3, Trino) for analytics and model training with consistent semantics.
- Improve observability and failure handling: replay, backfills, dead-letter handling, and incident runbooks.
- Mentor engineers and lead design reviews for the data platform.

Requirements
- 6+ years of data or backend engineering with at least 3 years on streaming systems (Flink, Kafka Streams, Spark Structured Streaming) in production.
- Strong Java; comfortable with Scala or Kotlin.
- Deep Kafka knowledge: partitioning, exactly-once semantics, compaction, operations at scale.
- Experience with lakehouse formats (Iceberg, Delta, Hudi) and query engines (Trino, Spark).
- Experience operating systems with strict latency and correctness requirements.
- Clear technical writing and design leadership.

Nice to have
- Adtech or high-frequency event-processing domain experience.
- Kubernetes and Terraform on AWS.
- Stream processing with Python (PyFlink) for ML feature pipelines.
- Experience with OpenTelemetry and cost attribution for data pipelines.

Compensation
$185,000 – $225,000 base plus bonus and equity. Full health benefits, 401(k) match, 20 days of PTO.

Quasar Adtech is an equal opportunity employer.""",
    ),
    SeedJd(
        role="data",
        key=key_for("data", "cobblestone-gov"),
        title="Data Engineer, Public Health Data Platform (Python / Airflow / Postgres)",
        company="Cobblestone Public Health",
        text="""Data Engineer, Public Health Data Platform (Python / Airflow / Postgres)
Cobblestone Public Health · Data Services
Location: Ottawa, ON (hybrid) or remote within Canada. Must be eligible to work in Canada; security screening required.

About the role
Cobblestone builds the data platform behind provincial public-health reporting: immunisation registries, surveillance feeds and lab results flow through pipelines we build, into dashboards and reports used by epidemiologists and the public. Data arrives in every format imaginable; correctness and privacy are not negotiable.

Responsibilities
- Build and operate ingestion and transformation pipelines in Python with Airflow, PostgreSQL and dbt; some sources are HL7, CSV over SFTP, or REST APIs.
- Implement de-identification, access controls and audit logging consistent with privacy legislation (PHIPA, PIPEDA).
- Design dimensional models for surveillance reporting and keep definitions consistent across jurisdictions.
- Establish data quality monitoring, reconciliation against source systems, and documented lineage.
- Containerise and deploy on Kubernetes (OpenShift) with GitLab CI and Terraform.
- Work with epidemiologists to translate reporting requirements into data products.

Requirements
- 3+ years of data engineering experience with Python and SQL in production.
- Experience with Airflow (or comparable orchestration) and dbt or similar transformation tooling.
- Strong PostgreSQL skills and solid data modelling fundamentals.
- Experience handling sensitive personal data and implementing privacy controls.
- Comfortable with Linux, containers and CI/CD.
- Bilingual (English/French) is an asset.

Nice to have
- Healthcare data standards (HL7v2, FHIR) or public-health reporting experience.
- Experience with OpenShift or other Kubernetes distributions.
- Power BI or Apache Superset.
- Experience passing government security clearance processes.

Compensation
CAD 100,000 – 125,000 base, defined-benefit pension, comprehensive health benefits, 4 weeks of vacation, and flexible hours.

Cobblestone Public Health is an equal opportunity employer.""",
    ),
)
