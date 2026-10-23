# Devpost submission text (copy each section into the matching field)

## Project name
AWS Cloud & Log Intelligence Agent

## Elevator pitch / tagline (≤ 60 chars)
Paste an AWS error. Get a grounded fix in seconds.

## Project overview (the "Project overview" step)
Debugging AWS is slow: an `AccessDenied` could be an IAM policy, a bucket policy, an SCP or a KMS key — and the answer is scattered across docs, runbooks and old incident logs. The AWS Cloud & Log Intelligence Agent turns a plain-English question (plus an optional log snippet or IAM/security-group config) into a structured diagnosis: severity, probable causes, ordered remediation steps and optimization tips — each answer citing the exact documentation chunks it was grounded in.

## Inspiration
Every on-call engineer has lost an hour to an error message that was one setting away from fixed. We wanted an assistant that knows *your* runbooks and past incidents, not just generic AWS docs, and that can be taught new material in seconds.

## What it does
- `POST /query` — ask about an AWS error or architecture; optionally attach logs or a config.
- Extracts AWS error codes, ARNs, services, request IDs and Lambda memory pressure from logs before the LLM sees them.
- Lints configs for risky patterns (wildcard IAM actions, public principals, `0.0.0.0/0`, public RDS, disabled encryption, disabled S3 Block Public Access).
- `POST /ingest` — upload markdown runbooks, `.log`, `.json` or `.yaml` files (or raw text) and they are searchable immediately.
- Returns structured JSON with severity, causes, steps, optimization tips and source citations.

## How we built it
- **FastAPI** backend with CORS, validation (Pydantic v2) and clear error handling.
- **RAG pipeline:** markdown-aware chunking → **BGE** embeddings (`BAAI/bge-small-en-v1.5` via HuggingFaceEmbeddings, with the BGE query instruction) → **ChromaDB** with cosine similarity and deterministic chunk IDs for idempotent ingestion.
- **LLM layer:** **Amazon Bedrock (Converse API)** with Anthropic/OpenAI as optional providers; JSON-schema-style prompting with validation of the model output.
- **Offline fallback engine** so the agent still answers from detected signals + retrieved docs if the LLM is unavailable.

## Challenges we ran into
- Getting reliable structured output from an LLM — solved with a strict schema prompt, JSON extraction and validation, plus a deterministic fallback.
- Retrieval quality on logs vs. prose — solved with different chunking strategies per document type and BGE query instructions.
- Keeping it demo-safe: the app degrades gracefully instead of failing on model or credential errors.

## Accomplishments that we're proud of
- A complete, runnable backend (query, ingest, health) with a seeded knowledge base.
- Grounded answers with visible source citations and relevance scores.
- Zero-downtime degradation: the agent never returns a blank screen during a demo.

## What we learned
Pre-extracting verifiable facts (error codes, ARNs, config findings) and handing them to the model alongside retrieved docs gives noticeably more specific, less hallucinated answers than retrieval alone.

## What's next
- Live CloudWatch Logs Insights ingestion.
- Bedrock Knowledge Bases / OpenSearch as a managed vector store.
- Proper IAM / CloudFormation / Terraform parsing.
- Strands / AgentCore tool use so the agent can run read-only AWS checks itself.
- Authentication and per-team knowledge bases.

## Built with
python, fastapi, amazon-bedrock, boto3, langchain, chromadb, sentence-transformers, huggingface, bge-embeddings, pydantic, docker, rag

## "Try it out" links
- GitHub repo: <PASTE PUBLIC REPO URL>
- Demo video: <PASTE YOUTUBE/VIMEO URL>

## Demo video script (≈ 2 minutes)
1. (0:00) Problem: AWS errors are slow to debug — show a real Lambda `AccessDeniedException` log.
2. (0:15) Open `/docs`, `POST /query` with the question + log snippet → show severity, causes, steps and source citations.
3. (0:55) `POST /ingest` a new runbook (e.g. an ALB 502 guide) → ask a question about it → show the new source cited.
4. (1:25) Paste a risky security-group/IAM JSON → show detected config findings.
5. (1:45) Show `/health` (Bedrock model active) and the architecture diagram from the README.
6. (1:55) Wrap up: what's next.
