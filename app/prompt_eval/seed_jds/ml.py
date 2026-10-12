from app.prompt_eval.seed_jds._base import SeedJd, key_for

JDS: tuple[SeedJd, ...] = (
    SeedJd(
        role="ml",
        key="seed:ml",
        title="Machine Learning Engineer, Recommendations (PyTorch / Spark)",
        company="Streamline Media",
        text="""Machine Learning Engineer, Recommendations
Streamline Media · Personalization
Location: London, UK (hybrid) or remote within the UK. Must have the right to work in the UK; visa sponsorship is available for exceptional candidates.

About the team
Streamline is a video streaming service with 40 million monthly viewers. The Personalization team owns the recommendation models behind the home page, autoplay, and push notifications. A one-percent lift in watch time moves the business; we A/B test everything and ship models weekly.

What you'll do
- Train and deploy ranking and retrieval models (two-tower retrieval, sequence models, multi-task rankers) in PyTorch.
- Build feature pipelines on Spark and Airflow over petabyte-scale event data; own feature freshness and point-in-time correctness.
- Run online experiments, analyse results with the data science team, and make the ship / no-ship call.
- Serve models at low latency (p99 < 50 ms) with Triton or TorchServe behind a feature store (Feast or an in-house equivalent).
- Monitor models in production: drift, calibration, popularity bias, and cold-start coverage.
- Collaborate with product on objectives beyond clicks: completion rate, diversity, and long-term retention.

What we're looking for
- 4+ years of experience building ML systems in production, including at least 2 years on recommendation, search or ads ranking.
- Strong PyTorch (or TensorFlow with willingness to switch) and a solid grasp of modern recommender architectures.
- Experience with large-scale data processing in Spark (PySpark or Scala) and workflow orchestration (Airflow, Dagster).
- Statistics for experimentation: power analysis, variance reduction (CUPED), multiple comparisons.
- Production engineering skills: Python, Docker, Kubernetes, CI/CD, and model serving infrastructure.
- MSc or PhD in computer science, statistics or a related field, or equivalent practical experience.

Nice to have
- Reinforcement learning or bandit approaches for recommendations.
- Experience with GPU training at scale (DDP, mixed precision) and cost optimisation.
- Familiarity with Kafka / Flink for near-real-time features.
- Publications at RecSys, KDD, WWW or similar.

Package
£95,000 – £125,000 base plus bonus and equity, private medical, 28 days holiday plus bank holidays, pension at 6%, and a hybrid working allowance.

We welcome applicants from all backgrounds and are happy to discuss flexible working.""",
    ),
    SeedJd(
        role="ml",
        key=key_for("ml", "copperline-fraud"),
        title="Machine Learning Engineer, Fraud Detection",
        company="Copperline Payments",
        text="""Machine Learning Engineer, Fraud Detection
Copperline Payments · Risk
Location: Remote within the US or hybrid in Atlanta, GA. Must be authorized to work in the United States.

About the team
Copperline processes card payments for 60,000 online merchants. The Risk team owns the models that score every transaction in under 50 ms, the rules engine merchants configure, and the feedback loops from chargebacks and manual review. Fraudsters adapt weekly; so do we.

What you'll do
- Train and ship fraud models (gradient-boosted trees, sequence models for account behaviour, graph features for linked entities) in Python with XGBoost, LightGBM and PyTorch.
- Build real-time features on Flink and Kafka with point-in-time correct offline equivalents in Spark.
- Own model monitoring: drift, calibration, approval-rate and chargeback-rate dashboards, and automated retraining.
- Run champion/challenger experiments and quantify the revenue impact of threshold changes with the analytics team.
- Serve models with low latency (Triton or a custom Go/Python service) and keep them safe to roll back.
- Work with manual-review analysts to turn their intuition into features and labels.

Requirements
- 4+ years building ML systems in production, with at least 2 years on fraud, risk, abuse or anomaly detection.
- Strong Python, SQL and experience with gradient-boosted models and feature engineering at scale.
- Experience with streaming features (Flink, Spark Streaming, Kafka) and feature stores.
- Comfort with model serving, latency budgets and CI/CD for models.
- Statistics for evaluation under label delay and selection bias.
- Clear communication with risk operations and finance stakeholders.

Nice to have
- Graph neural networks or entity-resolution experience.
- Experience with explainability requirements (reason codes, adverse action notices).
- PCI DSS or other regulated-environment experience.
- Go or Java for low-latency serving.

Compensation
$170,000 – $210,000 base plus bonus and equity. Health, dental, vision, 401(k) match, 20 days PTO.

Copperline Payments is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ml",
        key=key_for("ml", "farsight-vision"),
        title="Computer Vision Engineer (PyTorch / Edge Deployment)",
        company="Farsight Robotics",
        text="""Computer Vision Engineer (PyTorch / Edge Deployment)
Farsight Robotics · Perception
Location: Munich, Germany (onsite 4 days per week). EU citizens or holders of a German work permit; relocation support provided.

About Farsight
Farsight builds autonomous inventory robots for warehouses. The Perception team owns detection, tracking and reading of labels and barcodes from camera streams on the robot, running on NVIDIA Jetson hardware. Models must be accurate in bad lighting, fast on constrained compute, and stable across 300 customer sites.

What you'll do
- Train object-detection, segmentation and OCR models in PyTorch; curate datasets and run active learning on field data.
- Optimise for edge inference: quantisation, pruning, TensorRT, ONNX, and measuring latency and power on device.
- Build the data engine: labelling workflows, synthetic data, hard-example mining, and dataset versioning.
- Evaluate models against site-specific test sets and own the release process for perception models.
- Work with the robotics team on multi-camera calibration and sensor fusion inputs.
- Travel occasionally to customer sites to understand failures first-hand.

Requirements
- 3+ years of applied computer vision with PyTorch, with models deployed in production.
- Experience with edge deployment and inference optimisation (TensorRT, ONNX Runtime, OpenVINO or similar).
- Strong Python and comfortable C++ for inference code and integrations.
- Experience with dataset tooling and annotation pipelines at scale.
- MSc or PhD in computer science, robotics or related, or equivalent experience.

Nice to have
- OCR or barcode decoding experience.
- ROS 2, camera drivers, or robotics middleware.
- Experience with model monitoring in the field and fleet-wide rollouts.
- German language skills.

What we offer
€85,000 – €110,000 base, equity, 30 days of vacation, a JobRad bike, and relocation assistance.

Farsight Robotics is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ml",
        key=key_for("ml", "tempo-forecasting"),
        title="Senior ML Engineer, Demand Forecasting (Python / Spark / MLOps)",
        company="Tempo Retail",
        text="""Senior ML Engineer, Demand Forecasting (Python / Spark / MLOps)
Tempo Retail · Supply Chain Science
Location: Remote within the UK or Ireland. Right to work in the UK or Ireland required.

About the role
Tempo is a grocery retailer with 900 stores. Supply Chain Science owns the demand forecasts that drive replenishment for 40,000 products per store, every day. A one-percent improvement in forecast accuracy reduces waste by millions of pounds a year. You will own the forecasting platform from features to deployment.

What you'll do
- Build and improve hierarchical demand-forecasting models (gradient boosting, global deep models, probabilistic forecasts) in Python.
- Engineer features from promotions, weather, holidays, store events and pricing on Spark and Databricks.
- Own the MLOps platform: training pipelines on Airflow, model registry with MLflow, backtesting frameworks, and daily batch inference at scale.
- Design evaluation: rolling-origin backtests, metrics that reflect business impact (waste, availability), and bias analysis by category.
- Work with replenishment and planning teams to translate forecasts into orders and safety stock.
- Mentor two data scientists on engineering practices.

Requirements
- 5+ years of ML engineering or applied data science, with at least 2 years on time-series forecasting in production.
- Strong Python and SQL; experience with Spark or Databricks on large datasets.
- Experience with MLOps tooling (Airflow or Dagster, MLflow, feature stores) and with CI/CD for ML.
- Solid statistics: probabilistic forecasting, evaluation of hierarchical forecasts, uncertainty quantification.
- Experience communicating results to operations stakeholders.

Nice to have
- Retail, grocery or supply-chain domain experience.
- Deep learning for forecasting (Temporal Fusion Transformer, N-BEATS, DeepAR).
- Azure or AWS data platform experience.
- Optimisation experience (inventory models, OR tooling).

Package
£85,000 – £105,000 base plus bonus, pension, private medical, 25 days of holiday, and staff discount.

Tempo Retail is an equal opportunity employer.""",
    ),
    SeedJd(
        role="ml",
        key=key_for("ml", "sonic-speech"),
        title="ML Engineer, Speech Recognition (PyTorch / Streaming ASR)",
        company="Sonic Labs",
        text="""ML Engineer, Speech Recognition (PyTorch / Streaming ASR)
Sonic Labs · Speech
Location: San Francisco, CA (hybrid) or remote in the US or Canada. Visa sponsorship available.

About Sonic
Sonic provides real-time transcription and voice APIs to contact centres and meeting products. The Speech team trains and ships the streaming ASR models behind the API: multilingual, speaker-aware, and fast enough for live captions. We serve 400 million minutes of audio a month.

What you'll do
- Train and fine-tune streaming ASR models (Conformer, RNN-T, Whisper-style encoder-decoders) in PyTorch on multi-GPU clusters.
- Build data pipelines for audio: alignment, augmentation, pseudo-labelling, and filtering at the scale of hundreds of thousands of hours.
- Optimise inference for streaming: chunked attention, quantisation, batching, GPU serving with Triton or custom CUDA kernels.
- Evaluate rigorously: WER by domain, language and accent; latency and endpointing metrics; bias analysis.
- Collaborate with the platform team on model rollout, canarying and rollback across regions.
- Publish internally and externally when results warrant it.

Requirements
- 3+ years of ML engineering with speech, audio or sequence models, including models shipped to production.
- Strong PyTorch and experience with distributed training (DDP, FSDP) and mixed precision.
- Experience with inference optimisation for latency-sensitive workloads.
- Solid software engineering: Python, tests, reproducible experiments, experiment tracking.
- MSc or PhD in a relevant field or equivalent industry experience.

Nice to have
- Speaker diarization, voice activity detection, or text-to-speech experience.
- CUDA or Triton kernel development.
- Multilingual or low-resource language work.
- Experience with Kubernetes-based GPU serving.

Compensation
$185,000 – $240,000 base plus equity. Full health coverage, 401(k), unlimited PTO with a minimum, and a conference budget.

Sonic Labs is an equal opportunity employer.""",
    ),
)
