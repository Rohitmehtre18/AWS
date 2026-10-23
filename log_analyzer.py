"""
log_analyzer.py
===============
Turns (question + optional log/config text + retrieved docs) into a structured
troubleshooting answer.

Pipeline
--------
1. ``extract_signals``      - regex pass over logs: AWS error codes, ARNs, request IDs, services
2. ``scan_config``          - quick security / cost lint of IAM / SG / RDS style configs
3. ``LLMClient``            - Amazon Bedrock, Anthropic or OpenAI (selected from env vars)
4. ``LogAnalyzer.analyze``  - builds the prompt, calls the LLM, validates JSON output
   If no API key is configured (or the LLM fails), a deterministic offline fallback
   answers from detected signals + retrieved docs so the demo never breaks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from rag_engine import RetrievedChunk

logger = logging.getLogger("log_analyzer")


# ============================================================================
# Response schemas (shared with main.py)
# ============================================================================
class SourceRef(BaseModel):
    source: str
    section: str = ""
    score: float
    snippet: str


class TroubleshootingResponse(BaseModel):
    summary: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    probable_causes: List[str] = Field(default_factory=list)
    recommended_steps: List[str] = Field(default_factory=list)
    optimization_tips: List[str] = Field(default_factory=list)
    detected_signals: List[str] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)
    mode: Literal["llm", "offline"] = "offline"
    model: Optional[str] = None


# ============================================================================
# 1. Signal extraction from logs
# ============================================================================
ERROR_CODES = [
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "InvalidClientTokenId",
    "ExpiredToken", "ThrottlingException", "TooManyRequestsException", "Throttling",
    "ProvisionedThroughputExceededException", "SlowDown", "NoSuchBucket", "NoSuchKey",
    "InsufficientInstanceCapacity", "InstanceLimitExceeded", "VcpuLimitExceeded",
    "ResourceNotFoundException", "ValidationException", "CannotPullContainerError",
    "Task timed out", "Runtime exited with error", "OutOfMemory", "ConnectTimeout",
    "Too many connections", "InternalServerError", "ServiceUnavailable",
]
_CODE_RE = re.compile("|".join(re.escape(c) for c in ERROR_CODES), re.IGNORECASE)
_ARN_RE = re.compile(r"arn:aws[a-z\-]*:([a-z0-9\-]+):[a-z0-9\-]*:\d*:[^\s\"',]+")
_REQID_RE = re.compile(r"RequestId:?\s*([0-9a-fA-F\-]{16,})")
_STATUS_RE = re.compile(r"\b(?:HTTP/\d\.\d\"?\s+|status(?:Code)?[=: ]+)(4\d\d|5\d\d)\b", re.IGNORECASE)
_MEM_RE = re.compile(r"Memory Size:\s*(\d+)\s*MB.*?Max Memory Used:\s*(\d+)\s*MB", re.IGNORECASE)


def extract_signals(text: str) -> Dict[str, List[str]]:
    """Cheap, deterministic fact extraction so the LLM gets grounded hints."""
    if not text:
        return {}
    codes = sorted({m.group(0) for m in _CODE_RE.finditer(text)}, key=str.lower)
    services = sorted({m.group(1) for m in _ARN_RE.finditer(text)})
    arns = sorted({m.group(0) for m in _ARN_RE.finditer(text)})[:5]
    req_ids = sorted(set(_REQID_RE.findall(text)))[:3]
    statuses = sorted(set(_STATUS_RE.findall(text)))

    notes: List[str] = []
    for size, used in _MEM_RE.findall(text):
        if int(used) >= 0.9 * int(size):
            notes.append(f"Lambda memory nearly exhausted ({used}/{size} MB)")
    return {
        "error_codes": codes,
        "services": services,
        "arns": arns,
        "request_ids": req_ids,
        "http_statuses": statuses,
        "notes": notes,
    }


# ============================================================================
# 2. Config lint (works on JSON or YAML text via regex - no parser deps)
# ============================================================================
_CONFIG_RULES = [
    (re.compile(r'"?Action"?\s*:\s*\[?\s*"\*"'), "high", "IAM policy allows all actions ('Action': '*')"),
    (re.compile(r'"?Principal"?\s*:\s*\{?\s*(?:"AWS"\s*:\s*)?"\*"'), "critical", "Resource policy grants access to any principal ('*')"),
    (re.compile(r'"?Resource"?\s*:\s*\[?\s*"\*"'), "medium", "Policy applies to all resources ('Resource': '*')"),
    (re.compile(r"0\.0\.0\.0/0"), "high", "Open to the internet (0.0.0.0/0) - verify this is intentional"),
    (re.compile(r'"?PubliclyAccessible"?\s*:\s*"?true', re.I), "high", "RDS instance is publicly accessible"),
    (re.compile(r'"?(?:StorageEncrypted|Encrypted)"?\s*:\s*"?false', re.I), "medium", "Encryption at rest is disabled"),
    (re.compile(r'"?Block(?:Public)?(?:Acls|Policy)"?\s*:\s*"?false', re.I), "high", "S3 Block Public Access is disabled"),
]


def scan_config(text: str) -> List[Dict[str, str]]:
    findings = []
    for rx, sev, msg in _CONFIG_RULES:
        if rx.search(text or ""):
            findings.append({"severity": sev, "finding": msg})
    return findings


# ============================================================================
# 3. LLM client (Anthropic / OpenAI / none)
# ============================================================================
class LLMClient:
    """Thin provider-agnostic wrapper (Bedrock / Anthropic / OpenAI). ``available`` is False when no key is set."""

    def __init__(self) -> None:
        self.provider: Optional[str] = None
        self.model: Optional[str] = os.getenv("LLM_MODEL")
        self._client = None

        forced = os.getenv("LLM_PROVIDER", "auto").lower()  # auto | bedrock | anthropic | openai
        if forced == "bedrock" or (forced == "auto" and os.getenv("BEDROCK_MODEL_ID")):
            import boto3

            self.provider = "bedrock"
            self.model = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
            self._client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
        elif forced in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
            import anthropic

            self.provider = "anthropic"
            self.model = self.model or "claude-sonnet-5-5"
            self._client = anthropic.Anthropic()
        elif forced in ("auto", "openai") and os.getenv("OPENAI_API_KEY"):
            import openai

            self.provider = "openai"
            self.model = self.model or "gpt-4o-mini"
            self._client = openai.OpenAI()
        else:
            logger.warning("No LLM configured - running in offline fallback mode.")

    @property
    def available(self) -> bool:
        return self._client is not None

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        if not self.available:
            raise RuntimeError("LLM not configured")
        if self.provider == "bedrock":
            # Bedrock Converse API: one request shape for Nova, Claude, Llama, etc.
            resp = self._client.converse(
                modelId=self.model,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
            )
            return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.2,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


# ============================================================================
# 4. Analyzer
# ============================================================================
SYSTEM_PROMPT = """You are a senior AWS Solutions Architect and SRE.
Diagnose AWS errors and review cloud architecture using ONLY the provided context
plus well-established AWS knowledge. Be concrete (name services, settings, CLI commands).
If the context is insufficient, say so in the summary instead of inventing facts.

Respond with ONE JSON object and nothing else, using exactly this schema:
{
  "summary": "2-3 sentence diagnosis",
  "severity": "low" | "medium" | "high" | "critical",
  "probable_causes": ["..."],
  "recommended_steps": ["ordered, actionable steps"],
  "optimization_tips": ["cost / security / reliability improvements"]
}"""

# Offline fallback knowledge: error code (lowercase) -> (cause, fix, severity)
_OFFLINE_HINTS = {
    "accessdenied": ("IAM policy, resource policy, SCP or KMS key policy denies the action.",
                     "Run `aws sts get-caller-identity`, then check identity + resource policies and SCPs; test with the IAM Policy Simulator.", "high"),
    "accessdeniedexception": ("Caller lacks permission for the API call.",
                              "Add the missing action/resource to the role's policy (least privilege) and re-test.", "high"),
    "task timed out": ("Function exceeded its timeout or is blocked on a slow/unreachable dependency.",
                       "Increase timeout/memory, check VPC egress (NAT or VPC endpoints) and downstream latency.", "medium"),
    "throttlingexception": ("API or service rate limit exceeded.",
                            "Add exponential backoff with jitter, reduce request rate, or request a quota increase.", "medium"),
    "toomanyrequestsexception": ("Lambda concurrency limit reached.",
                                 "Set reserved concurrency or request a concurrency quota increase.", "medium"),
    "provisionedthroughputexceededexception": ("DynamoDB capacity exceeded or hot partition.",
                                               "Switch to on-demand, enable auto scaling, and spread partition keys.", "medium"),
    "slowdown": ("S3 request rate too high on a prefix.", "Distribute keys across prefixes and retry with backoff.", "medium"),
    "nosuchbucket": ("Bucket name or region is wrong.", "Verify bucket name/region and that the caller targets the right account.", "medium"),
    "insufficientinstancecapacity": ("No capacity for that instance type in the AZ.",
                                     "Retry later or use another AZ / instance type / capacity reservation.", "medium"),
    "cannotpullcontainererror": ("Task cannot pull the image.",
                                 "Check ECR permissions on the execution role and NAT/VPC endpoints for ECR + S3.", "high"),
    "too many connections": ("Database max_connections exceeded.", "Use RDS Proxy / pooling and scale the instance.", "high"),
}

_SEV_ORDER = ["low", "medium", "high", "critical"]


class LogAnalyzer:
    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()

    # ---- prompt ------------------------------------------------------------
    @staticmethod
    def _build_user_prompt(question: str, extra: Optional[str], signals: dict,
                           findings: list, chunks: List[RetrievedChunk]) -> str:
        ctx = "\n\n".join(
            f"[Doc {i + 1}] source={c.source} section={c.section!r} score={c.score}\n{c.content}"
            for i, c in enumerate(chunks)
        ) or "(no relevant documentation retrieved)"
        parts = [
            f"USER QUESTION:\n{question}",
            f"RETRIEVED DOCUMENTATION:\n{ctx}",
        ]
        if extra:
            parts.append(f"USER-SUPPLIED LOG / CONFIG (truncated):\n{extra[:6000]}")
        if any(signals.values()):
            parts.append(f"EXTRACTED SIGNALS:\n{json.dumps(signals, indent=2)}")
        if findings:
            parts.append(f"CONFIG LINT FINDINGS:\n{json.dumps(findings, indent=2)}")
        return "\n\n".join(parts)

    # ---- JSON parsing ------------------------------------------------------
    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON object in LLM output")
        return json.loads(raw[start:end + 1])

    # ---- offline fallback --------------------------------------------------
    def _offline(self, signals: dict, findings: list, chunks: List[RetrievedChunk]) -> dict:
        causes, steps, sev = [], [], "low"
        for code in signals.get("error_codes", []):
            hint = _OFFLINE_HINTS.get(code.lower())
            if hint:
                causes.append(f"{code}: {hint[0]}")
                steps.append(hint[1])
                if _SEV_ORDER.index(hint[2]) > _SEV_ORDER.index(sev):
                    sev = hint[2]
        for f in findings:
            causes.append(f"Config issue: {f['finding']}")
            if _SEV_ORDER.index(f["severity"]) > _SEV_ORDER.index(sev):
                sev = f["severity"]
        for c in chunks[:3]:
            steps.append(f"See '{c.section or c.source}' in {c.source} (relevance {c.score}).")
        summary = ("Offline analysis (no LLM key configured). "
                   + ("Matched known AWS error patterns and retrieved related docs."
                      if causes or chunks else "No known patterns or relevant docs were found."))
        return {
            "summary": summary,
            "severity": sev,
            "probable_causes": causes,
            "recommended_steps": list(dict.fromkeys(steps)),
            "optimization_tips": [],
        }

    # ---- public API --------------------------------------------------------
    def analyze(self, question: str, chunks: List[RetrievedChunk],
                extra_text: Optional[str] = None) -> TroubleshootingResponse:
        corpus = f"{question}\n{extra_text or ''}"
        signals = extract_signals(corpus)
        findings = scan_config(corpus)

        data, mode = None, "offline"
        if self.llm.available:
            try:
                raw = self.llm.complete(SYSTEM_PROMPT,
                                        self._build_user_prompt(question, extra_text, signals, findings, chunks))
                data, mode = self._parse_json(raw), "llm"
            except Exception as exc:  # network, auth, bad JSON... -> degrade gracefully
                logger.error("LLM call failed, using offline fallback: %s", exc)
        if data is None:
            data = self._offline(signals, findings, chunks)

        detected = [f"error: {c}" for c in signals.get("error_codes", [])]
        detected += [f"service: {s}" for s in signals.get("services", [])]
        detected += [f"http: {s}" for s in signals.get("http_statuses", [])]
        detected += signals.get("notes", [])
        detected += [f"config[{f['severity']}]: {f['finding']}" for f in findings]

        sev = data.get("severity", "medium")
        return TroubleshootingResponse(
            summary=data.get("summary", "No summary produced."),
            severity=sev if sev in _SEV_ORDER else "medium",
            probable_causes=[str(x) for x in data.get("probable_causes", [])],
            recommended_steps=[str(x) for x in data.get("recommended_steps", [])],
            optimization_tips=[str(x) for x in data.get("optimization_tips", [])],
            detected_signals=detected,
            sources=[SourceRef(source=c.source, section=c.section, score=c.score,
                               snippet=c.content[:240].replace("\n", " ")) for c in chunks],
            mode=mode,
            model=self.llm.model if mode == "llm" else None,
        )
