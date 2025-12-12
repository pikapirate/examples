# Tensorlake Examples

Example applications demonstrating [Tensorlake's](https://tensorlake.ai) distributed computing capabilities. Each example showcases patterns that would be complex to implement with traditional infrastructure.

## What Makes Tensorlake Different

Tensorlake turns Python functions into distributed, fault-tolerant applications. What you'd normally build with Kafka + Kubernetes + workflow orchestrators, you can write as decorated Python functions.

| Traditional Stack | Tensorlake Equivalent |
|------------------|----------------------|
| Kafka/SQS + workers | `function.map()` |
| Workflow orchestrators | Sequential function calls with durability |
| Kubernetes deployments | `@function(cpu=2, memory=4)` |
| Service mesh + RPC | Functions calling functions |
| Worker pools | Automatic container scaling |

## Examples

| Example | Pattern | What It Demonstrates |
|---------|---------|---------------------|
| [web-scraper](./web-scraper) | **Map-Reduce** | Process N items in parallel, aggregate results |
| [text-analyzer](./text-analyzer) | **Parallel Futures** | Run multiple operations concurrently |
| [document-pipeline](./document-pipeline) | **Multi-Stage Workflow** | Chain processing stages with different resources |
| [event-fanout](./event-fanout) | **Fan-Out with Retries** | Broadcast to multiple channels with independent policies |
| [template-deployer](./template-deployer) | **SDK Internals** | Deploy applications programmatically using SDK internals |

## Quick Start

### Prerequisites

```bash
pip install tensorlake
tensorlake login
```

### Deploy an Example

```bash
cd web-scraper
tensorlake deploy main.py
```

### Invoke

```bash
curl -X POST \
  "https://api.tensorlake.ai/v1/namespaces/<namespace>/applications/scrape_sites/invoke" \
  -H "Authorization: Bearer $TENSORLAKE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '["https://example.com", "https://httpbin.org/html"]'
```

## Example Deep Dive

### 1. Web Scraper (Map-Reduce)

Scrape N URLs in parallel, aggregate word counts:

```python
@application()
@function()
def scrape_sites(urls: List[str]) -> dict:
    # Each URL processed in its own container, in parallel
    word_counts = count_words.map(urls)
    # Results aggregated as they arrive
    return merge_counts.reduce(word_counts, initial_state)

@function(timeout=30, retries=Retries(max_retries=2))
def count_words(url: str) -> dict:
    # Independent container with automatic retries
    ...
```

**What this replaces:** Task queue + worker pool + result aggregation service

---

### 2. Text Analyzer (Parallel Futures)

Run 4 analyses on the same text concurrently:

```python
@application()
@function()
def analyze_text(text: str) -> dict:
    # All 4 start immediately, run in parallel containers
    sentiment = analyze_sentiment.awaitable(text).run()
    stats = compute_statistics.awaitable(text).run()
    keywords = extract_keywords.awaitable(text).run()
    readability = compute_readability.awaitable(text).run()

    Future.wait([sentiment, stats, keywords, readability])
    return {...}
```

**What this replaces:** Multiple microservices + orchestration layer + fan-out logic

---

### 3. Document Pipeline (Multi-Stage Workflow)

Process documents through stages with different resource needs:

```python
@application()
@function()
def process_documents(urls: List[str]) -> List[dict]:
    documents = download.map(urls)           # Stage 1: 1 CPU, 1GB
    parsed = parse_content.map(documents)    # Stage 2: 2 CPU, 2GB
    enriched = enrich.map(parsed)            # Stage 3: 1 CPU, 1GB
    return summarize.map(enriched)           # Stage 4: 2 CPU, 2GB

@function(cpu=2, memory=2)  # Heavy processing stage
def parse_content(doc: dict) -> dict: ...
```

**What this replaces:** Workflow orchestration + multiple K8s deployments + state management

---

### 4. Event Fan-Out (Parallel with Per-Channel Retries)

Broadcast events with independent retry policies:

```python
@function(retries=Retries(max_retries=3), timeout=30)  # External service
def send_webhook(event: Event) -> dict: ...

@function(retries=Retries(max_retries=1), timeout=10)  # Non-critical
def record_metrics(event: Event) -> dict: ...
```

**What this replaces:** Message broker + per-channel consumers + circuit breakers

---

### 5. Template Deployer (SDK Internals)

Deploy Tensorlake applications programmatically:

```python
@application()
@function(timeout=120, secrets=["TENSORLAKE_DEPLOYER_API_KEY"])
def deploy_template(request: DeployRequest) -> DeployResult:
    # Fetch source files in parallel
    main_future = fetch_file.awaitable(template_id, "main.py").run()
    reqs_future = fetch_file.awaitable(template_id, "requirements.txt").run()
    Future.wait([main_future, reqs_future])

    # Generate manifest using SDK internals (same as CLI)
    manifest = generate_manifest(main_future.result(), app_name)

    # Deploy using APIClient
    deploy_to_namespace(manifest, code_zip, namespace)
```

**What this demonstrates:** Using SDK internals to build automation tools. This is the same application that powers one-click template deployment in the Tensorlake dashboard.

## Key Concepts

### Functions Run in Separate Containers

```python
@function(cpu=1, memory=1)
def lightweight_task(): ...

@function(cpu=4, memory=8, gpu="T4")
def heavy_ml_task(): ...
```

Each function gets its own container with specified resources.

### Durability

If step 3 of a 5-step pipeline fails:
- Steps 1-2 don't re-run
- Step 3 retries automatically
- Steps 4-5 run after step 3 succeeds

No checkpointing code needed.

### Parallel Execution

```python
# Map: N items → N parallel containers
results = process.map(items)

# Futures: Start multiple operations immediately
a = func_a.awaitable(x).run()
b = func_b.awaitable(y).run()
Future.wait([a, b])
```

## Resources

- [Tensorlake Documentation](https://docs.tensorlake.ai)
- [Applications Quickstart](https://docs.tensorlake.ai/applications/quickstart)
- [Map-Reduce Guide](https://docs.tensorlake.ai/applications/map-reduce)
- [Futures Guide](https://docs.tensorlake.ai/applications/futures)
- [Join our Slack](https://join.slack.com/t/tensorlakecloud/shared_invite/zt-32fq4nmib-gO0OM5RIar3zLOBm~ZGqKg)

## License

MIT License - see [LICENSE](./LICENSE) for details.
