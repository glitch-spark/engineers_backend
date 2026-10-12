from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="llm",
        key="seed:llm",
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
        role="llm",
        key=key_for("llm", "lantern-inference"),
        title="LLM Inference Engineer (CUDA / vLLM / Kubernetes)",
        company="Lantern AI",
        text="""LLM Inference Engineer (CUDA / vLLM / Kubernetes)
Lantern AI · Inference Platform
Location: Remote within the US; optional office in Austin, TX. US work authorization required.

About the team
Lantern serves open-weight language models as an API for 3,000 developers. The Inference Platform team owns the serving stack: model loading, batching, KV-cache management, speculative decoding, multi-tenant scheduling and the GPU fleet across three clouds. Every millisecond and every GPU-hour is our product.

What you'll do
- Operate and extend vLLM, SGLang or TensorRT-LLM deployments; contribute patches upstream when we hit limits.
- Implement serving optimisations: continuous batching, paged attention tuning, prefix caching, speculative decoding, quantisation (FP8, AWQ, GPTQ).
- Build the scheduler that places requests across models and GPUs to meet latency SLOs at the lowest cost.
- Write CUDA or Triton kernels for hot paths when existing kernels fall short.
- Run capacity planning and autoscaling across H100 and MI300 fleets on Kubernetes.
- Benchmark relentlessly and publish numbers the rest of the company trusts.

Requirements
- 4+ years of systems or ML infrastructure engineering with 1+ year on LLM inference specifically.
- Deep familiarity with transformer inference internals: attention, KV cache, batching, decoding strategies.
- Strong Python and C++; experience writing or profiling GPU kernels (CUDA, Triton).
- Experience operating GPU workloads on Kubernetes at scale.
- Comfort with Linux performance tooling and distributed systems fundamentals.

Nice to have
- Contributions to vLLM, TensorRT-LLM, SGLang, llama.cpp or similar.
- Experience with multi-node inference (tensor and pipeline parallelism).
- Familiarity with model formats (safetensors, GGUF) and conversion pipelines.
- Background in HPC or networking (RDMA, NCCL).

Compensation
$200,000 – $270,000 base plus equity. Health coverage, 401(k), unlimited PTO, and a GPU budget for experiments.

Lantern AI is an equal opportunity employer.""",
    ),
    SeedJd(
        role="llm",
        key=key_for("llm", "argent-safety"),
        title="LLM Safety & Evaluation Engineer",
        company="Argent",
        text="""LLM Safety & Evaluation Engineer
Argent · Model Assurance
Location: London, UK (hybrid) or remote within the UK and EU. Must have the right to work in the UK or an EU member state.

About Argent
Argent builds assistants for financial advisers and is regulated by the FCA. The Model Assurance team makes sure every model we deploy behaves within policy: no unsuitable advice, no fabricated figures, graceful refusals, and evidence we can show a regulator. This role sits between research, engineering and compliance.

What you'll do
- Design and maintain evaluation suites for assistant behaviour: factuality against source documents, policy adherence, refusal quality, and robustness to prompt injection and jailbreaks.
- Build LLM-as-judge pipelines calibrated against human review; track agreement and drift over time.
- Run red-team exercises with adversarial prompt generation and document findings with reproducible cases.
- Implement guardrails: classifiers, output filters, structured-output constraints, and the monitoring that catches regressions in production.
- Partner with compliance to turn regulatory requirements into testable behaviours and audit reports.
- Work with the modelling team on fine-tuning data that fixes measured failures.

Requirements
- 3+ years of engineering experience in Python with 1+ year focused on LLM evaluation, safety or red-teaming.
- Experience building evaluation datasets and judge pipelines with calibration against humans.
- Understanding of adversarial techniques against LLM applications and practical mitigations.
- Statistics for evaluation: confidence intervals, inter-annotator agreement, sample sizing.
- Clear writing; you will author reports read by non-engineers and regulators.

Nice to have
- Experience in a regulated industry (financial services, health, legal).
- Fine-tuning or RLHF/DPO experience.
- Familiarity with safety benchmarks and frameworks (HELM, OWASP LLM Top 10, NIST AI RMF).
- Experience with classification models for content moderation.

Package
£85,000 – £110,000 base plus equity, pension, private medical, 25 days of holiday, and a learning budget.

Argent is an equal opportunity employer.""",
    ),
    SeedJd(
        role="llm",
        key=key_for("llm", "meridian-code"),
        title="Senior LLM Engineer, Code Models (Post-training & Data)",
        company="Meridian Code",
        text="""Senior LLM Engineer, Code Models (Post-training & Data)
Meridian Code · Model Team
Location: Seattle, WA or San Francisco, CA (hybrid, 3 days per week). Visa sponsorship available.

About Meridian
Meridian builds an AI coding assistant used by 200,000 developers. The Model Team post-trains open-weight and partner models for code completion, editing and repository-level reasoning. Our edge is data: execution-verified training signals from real repositories and a careful evaluation stack.

What you'll do
- Own post-training pipelines: SFT, preference optimisation (DPO, GRPO-style RL with execution feedback) on multi-node GPU clusters using PyTorch, TRL and custom code.
- Build data engines for code: repository mining, deduplication, license filtering, test-based verification, and synthetic task generation.
- Design evaluation: benchmark suites (SWE-bench style, HumanEval+, internal repo tasks), contamination checks, and online metrics from the product.
- Improve long-context behaviour and tool use (file edits, running tests, searching repos).
- Work with the inference team on model formats, quantisation and latency trade-offs.
- Write up findings; present to the research review weekly.

Requirements
- 5+ years of ML or software engineering experience with 2+ years on LLM training or post-training.
- Deep PyTorch experience and familiarity with distributed training (FSDP, DeepSpeed) and the Hugging Face stack.
- Experience with code-generation models or program synthesis, or strong software engineering plus demonstrated LLM training work.
- Experience designing evaluation pipelines and data-quality filters at scale.
- Strong systems skills: Python, Linux, Kubernetes or Slurm, data pipelines on object storage.

Nice to have
- Publications in ML or software engineering venues.
- Experience with RL from execution or compiler feedback.
- Rust or Go for high-throughput data tooling.
- Contributions to open-source model training frameworks.

Compensation
$210,000 – $280,000 base plus significant equity, full benefits, 401(k) match, and a research travel budget.

Meridian Code is an equal opportunity employer.""",
    ),
    SeedJd(
        role="llm",
        key=key_for("llm", "harborview-platform"),
        title="LLM Platform Engineer (Python / Observability / Prompt Management)",
        company="Harborview Software",
        text="""LLM Platform Engineer (Python / Observability / Prompt Management)
Harborview Software · AI Platform
Location: Remote, Canada or United States (Eastern or Central time). Must be legally authorized to work in Canada or the US.

About the role
Harborview is a 900-person SaaS company with a dozen product teams shipping LLM features. The AI Platform team builds the internal platform those teams use: a gateway to model providers, prompt and version management, evaluation tooling, cost controls and observability. You will make the right thing the easy thing for every team in the company.

What you'll do
- Build and operate the LLM gateway in Python (FastAPI) and Go: routing across Anthropic, OpenAI and self-hosted models, retries and fallbacks, rate limits, budgets per team.
- Design prompt management: versioning, templating, environment promotion, and a review workflow tied to evaluations.
- Build the evaluation service: datasets, judge models, regression runs in CI, and dashboards product teams act on.
- Instrument everything with OpenTelemetry: traces across prompt chains, token accounting, latency and cost attribution.
- Run office hours and write guides; drive adoption through good defaults rather than mandates.
- Own reliability: SLOs for the gateway, on-call, incident reviews.

Requirements
- 4+ years of platform or backend engineering in Python (Go or TypeScript a plus) with 1+ year building LLM infrastructure or features.
- Experience with observability tooling (OpenTelemetry, Datadog, Grafana) and with multi-tenant services.
- Familiarity with the Anthropic and OpenAI APIs, streaming, tool calling and structured outputs.
- Experience building internal developer platforms or shared libraries used by many teams.
- Strong communication; you enjoy helping other engineers ship.

Nice to have
- Experience with LLM observability products (Langfuse, LangSmith, Braintrust, Arize) or building equivalents.
- Kubernetes, Terraform, AWS or GCP.
- Security experience: secrets management, data classification, prompt-injection defences.
- Experience with self-hosted model serving (vLLM).

Compensation
CAD 150,000 – 185,000 or USD 150,000 – 185,000 depending on location, plus bonus and equity, full benefits, and 4 weeks of vacation.

Harborview Software is an equal opportunity employer.""",
    ),
)
