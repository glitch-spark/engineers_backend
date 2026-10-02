"""Built-in skills with aliases, for the rule-based extraction fallback and for variant matching.

Canonical name → aliases. Names listed in CASE_SENSITIVE are matched case-sensitively because their
lowercase forms are ordinary English words ("go", "r", "c").
"""
from functools import lru_cache

from app.job_apply.text import term_regex

SKILLS: dict[str, tuple[str, ...]] = {
    # Languages
    "Python": ("Python3",),
    "Java": ("Java 8", "Java 11", "Java 17"),
    "JavaScript": ("JS", "ECMAScript", "ES6"),
    "TypeScript": ("TS",),
    "Go": ("Golang",),
    "Rust": (),
    "C": (),
    "C++": ("CPP", "C/C++"),
    "C#": ("CSharp", "C Sharp"),
    "Ruby": (),
    "PHP": (),
    "Kotlin": (),
    "Swift": (),
    "Objective-C": ("ObjC",),
    "Scala": (),
    "Elixir": (),
    "Erlang": (),
    "Clojure": (),
    "Haskell": (),
    "R": (),
    "MATLAB": (),
    "Perl": (),
    "Dart": (),
    "Lua": (),
    "Bash": ("Shell scripting", "Shell"),
    "PowerShell": (),
    "SQL": (),
    "PL/SQL": ("PLSQL",),
    "T-SQL": ("TSQL",),
    "Solidity": (),
    "HTML": ("HTML5",),
    "CSS": ("CSS3",),
    "Sass": ("SCSS",),
    "GraphQL": (),
    # Frontend
    "React": ("ReactJS", "React.js"),
    "React Native": (),
    "Next.js": ("NextJS",),
    "Vue.js": ("Vue", "VueJS"),
    "Nuxt.js": ("Nuxt",),
    "Angular": ("AngularJS",),
    "Svelte": ("SvelteKit",),
    "Redux": ("Redux Toolkit",),
    "Tailwind CSS": ("Tailwind", "TailwindCSS"),
    "Material UI": ("MUI",),
    "Webpack": (),
    "Vite": (),
    "jQuery": (),
    "Storybook": (),
    "Three.js": ("ThreeJS",),
    "D3.js": ("D3",),
    "Flutter": (),
    "Electron": (),
    # Backend frameworks
    "Node.js": ("Node", "NodeJS"),
    "Express": ("Express.js", "ExpressJS"),
    "NestJS": ("Nest.js",),
    "Django": (),
    "Flask": (),
    "FastAPI": (),
    "Spring Boot": ("Spring", "Spring Framework"),
    "Hibernate": (),
    "Ruby on Rails": ("Rails", "RoR"),
    "Laravel": (),
    "Symfony": (),
    ".NET": ("dotnet", ".NET Core", "ASP.NET", "ASP.NET Core"),
    "Entity Framework": ("EF Core",),
    "Phoenix": (),
    "gRPC": (),
    "REST API": ("REST", "RESTful", "RESTful API"),
    "Microservices": ("Microservice architecture",),
    "WebSockets": ("WebSocket", "Socket.IO"),
    "OAuth": ("OAuth2", "OAuth 2.0", "OIDC", "OpenID Connect"),
    "JWT": (),
    # Databases and storage
    "PostgreSQL": ("Postgres", "psql"),
    "MySQL": (),
    "MariaDB": (),
    "SQLite": (),
    "SQL Server": ("MSSQL", "Microsoft SQL Server"),
    "Oracle": ("Oracle DB",),
    "MongoDB": ("Mongo",),
    "Redis": (),
    "Cassandra": (),
    "DynamoDB": (),
    "Elasticsearch": ("Elastic", "ELK", "OpenSearch"),
    "Neo4j": (),
    "CouchDB": (),
    "Firebase": ("Firestore",),
    "Supabase": (),
    "Snowflake": (),
    "BigQuery": (),
    "Redshift": (),
    "Databricks": (),
    "ClickHouse": (),
    "Pinecone": (),
    "pgvector": (),
    "Prisma": (),
    "SQLAlchemy": (),
    "Sequelize": (),
    "TypeORM": (),
    # Cloud
    "AWS": ("Amazon Web Services",),
    "GCP": ("Google Cloud", "Google Cloud Platform"),
    "Azure": ("Microsoft Azure",),
    "AWS Lambda": ("Lambda",),
    "Amazon S3": ("S3",),
    "Amazon EC2": ("EC2",),
    "Amazon ECS": ("ECS", "Fargate"),
    "Amazon EKS": ("EKS",),
    "Amazon SQS": ("SQS",),
    "Amazon SNS": ("SNS",),
    "Amazon RDS": ("RDS", "Aurora"),
    "CloudFormation": (),
    "AWS CDK": ("CDK",),
    "Cloud Run": (),
    "Heroku": (),
    "Vercel": (),
    "Netlify": (),
    "Cloudflare": ("Cloudflare Workers",),
    "Serverless": ("Serverless Framework",),
    # DevOps
    "Docker": ("Containerization",),
    "Kubernetes": ("K8s", "k8s"),
    "Helm": (),
    "Terraform": (),
    "Pulumi": (),
    "Ansible": (),
    "Chef": (),
    "Puppet": (),
    "Jenkins": (),
    "GitHub Actions": (),
    "GitLab CI": ("GitLab CI/CD",),
    "CircleCI": (),
    "Argo CD": ("ArgoCD", "Argo"),
    "CI/CD": ("Continuous Integration", "Continuous Delivery", "Continuous Deployment"),
    "Git": ("GitHub", "GitLab", "Bitbucket"),
    "Linux": ("Unix", "Ubuntu", "RHEL"),
    "Nginx": (),
    "Prometheus": (),
    "Grafana": (),
    "Datadog": (),
    "New Relic": (),
    "Splunk": (),
    "Sentry": (),
    "PagerDuty": (),
    "OpenTelemetry": ("OTel",),
    "Istio": (),
    "Vault": ("HashiCorp Vault",),
    "Infrastructure as Code": ("IaC",),
    "Site Reliability Engineering": ("SRE",),
    # Messaging and data engineering
    "Kafka": ("Apache Kafka",),
    "RabbitMQ": (),
    "Apache Spark": ("Spark", "PySpark"),
    "Hadoop": ("HDFS",),
    "Airflow": ("Apache Airflow",),
    "dbt": (),
    "Flink": ("Apache Flink",),
    "Kinesis": (),
    "Pub/Sub": ("PubSub",),
    "ETL": ("ELT", "Data pipelines"),
    "Data Warehousing": ("Data Warehouse",),
    "Pandas": (),
    "NumPy": (),
    "Jupyter": ("Jupyter Notebook",),
    "Tableau": (),
    "Power BI": ("PowerBI",),
    "Looker": ("LookML",),
    "Excel": ("Microsoft Excel",),
    # AI / ML
    "Machine Learning": ("ML",),
    "Deep Learning": (),
    "LLM": ("LLMs", "Large Language Models", "Large Language Model"),
    "Generative AI": ("GenAI", "Gen AI"),
    "NLP": ("Natural Language Processing",),
    "Computer Vision": (),
    "PyTorch": (),
    "TensorFlow": (),
    "Keras": (),
    "scikit-learn": ("sklearn", "scikit learn"),
    "XGBoost": (),
    "Hugging Face": ("HuggingFace", "Transformers"),
    "LangChain": (),
    "LlamaIndex": (),
    "OpenAI API": ("OpenAI", "GPT-4", "ChatGPT"),
    "RAG": ("Retrieval-Augmented Generation", "Retrieval Augmented Generation"),
    "Prompt Engineering": (),
    "Vector Databases": ("Vector DB", "Vector Database"),
    "MLOps": (),
    "MLflow": (),
    "Kubeflow": (),
    "SageMaker": ("Amazon SageMaker",),
    "Vertex AI": (),
    "Fine-tuning": ("Fine tuning",),
    # Testing and quality
    "Unit Testing": ("Unit tests",),
    "Integration Testing": ("Integration tests",),
    "Test-Driven Development": ("TDD",),
    "Jest": (),
    "Vitest": (),
    "Mocha": (),
    "Cypress": (),
    "Playwright": (),
    "Selenium": (),
    "pytest": (),
    "JUnit": (),
    "Postman": (),
    "Load Testing": ("Performance testing", "k6", "JMeter"),
    # Mobile
    "iOS": (),
    "Android": (),
    "SwiftUI": (),
    "Jetpack Compose": (),
    "Xcode": (),
    # Architecture and practices
    "System Design": (),
    "Distributed Systems": (),
    "Event-Driven Architecture": ("Event-driven", "Event Sourcing"),
    "Domain-Driven Design": ("DDD",),
    "CQRS": (),
    "Design Patterns": (),
    "Object-Oriented Programming": ("OOP", "Object Oriented"),
    "Functional Programming": (),
    "Data Structures": ("Algorithms",),
    "API Design": (),
    "Caching": (),
    "Scalability": (),
    "Performance Optimization": ("Performance tuning",),
    "Security": ("Application security", "AppSec"),
    "OWASP": (),
    "Agile": ("Scrum", "Kanban"),
    "Jira": (),
    "Confluence": (),
    "Figma": (),
    "Accessibility": ("WCAG", "a11y"),
    "SEO": (),
    "Web3": ("Blockchain", "Ethereum"),
    "Stripe": (),
    "Twilio": (),
    "Salesforce": (),
    "SAP": (),
    "Shopify": (),
    "WordPress": (),
    "Unity": (),
    "Unreal Engine": ("Unreal",),
    "Embedded Systems": ("Embedded", "Firmware"),
    "RTOS": (),
    "Networking": ("TCP/IP",),
}

SOFT_SKILLS = frozenset(
    s.lower()
    for s in (
        "communication",
        "communication skills",
        "teamwork",
        "collaboration",
        "leadership",
        "problem solving",
        "problem-solving",
        "ownership",
        "mentoring",
        "mentorship",
        "stakeholder management",
        "time management",
        "adaptability",
        "critical thinking",
        "attention to detail",
        "presentation skills",
        "interpersonal skills",
        "self-motivated",
        "curiosity",
        "empathy",
    )
)

CASE_SENSITIVE = frozenset({
    "Go", "R", "C", "Rust", "Swift", "Spring", "Express", "Phoenix", "Unity", "Chef", "Puppet", "Vault", "Rails",
    "Elastic", "Lambda", "Argo", "Shell", "Spark", "Node", "Mongo", "Oracle", "Looker", "ML", "TS", "JS",
    "Transformers", "Embedded", "Unreal", "Next",
})


def _index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for canon, aliases in SKILLS.items():
        for name in (canon, *aliases):
            idx.setdefault(name.lower(), canon)
    return idx


_INDEX = _index()


def canonical(term: str) -> str | None:
    return _INDEX.get((term or "").strip().lower())


def aliases_for(term: str) -> tuple[str, ...]:
    """Other names of the same skill (canonical included when `term` is an alias)."""
    canon = canonical(term)
    if canon is None:
        return ()
    t = term.strip().lower()
    return tuple(n for n in (canon, *SKILLS[canon]) if n.lower() != t)


def is_soft(term: str) -> bool:
    return (term or "").strip().lower() in SOFT_SKILLS


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[str, list]]:
    out = []
    for canon, aliases in SKILLS.items():
        pats = [term_regex(n, n in CASE_SENSITIVE) for n in (canon, *aliases)]
        out.append((canon, pats))
    return out


def find_skills(text: str) -> list[str]:
    """Canonical names of every built-in skill mentioned in `text`, in dictionary order."""
    return [canon for canon, pats in _patterns() if any(p.search(text or "") for p in pats)]
