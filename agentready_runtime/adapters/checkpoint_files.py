"""Atomic files and legacy-compatible checkpoint index; no Hermes dependency."""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from agentready_runtime.checkpoints import (
    CHECKPOINT_REFERENCE_HEADING,
    strip_checkpoint_reference,
)
from agentready_runtime.sessions import HOME_SESSION_ID

CHECKPOINT_INDEX_LIMIT = 256


def atomic_write_text(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        "." + target.name + ".tmp-" + str(os.getpid()) + "-" + str(time.time_ns())
    )
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


class FileCheckpointArchive:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / ".agentready/checkpoints"

    def checkpoint_parent(self, previous_summary):
        # Resolve only our persisted footer, never a path invented by the new LLM
        # summary. Rehydrating the footer from history also survives process restarts.
        footer = str(previous_summary or "").split(CHECKPOINT_REFERENCE_HEADING, 1)
        if len(footer) != 2:
            return None
        match = re.search(
            r"/opt/data/workspace/\.agentready/checkpoints/[^\s]+\.md", footer[1]
        )
        if not match:
            raise RuntimeError("Previous checkpoint footer has no archive path")
        container_path = match.group(0)
        relative = container_path.removeprefix("/opt/data/workspace/")
        archive = (self.workspace / relative).resolve()
        if not archive.is_relative_to(self.root.resolve()) or not archive.is_file():
            raise RuntimeError("Previous checkpoint archive is unavailable")
        return container_path

    def save(self, session_id, summary, previous_summary, estimate):
        clean = strip_checkpoint_reference(str(summary or "").strip())
        if not clean:
            return summary
        safe_session = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or HOME_SESSION_ID)).strip(
                "-"
            )
            or HOME_SESSION_ID
        )
        previous_checkpoint = self.checkpoint_parent(previous_summary)
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        stamp = (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + "-"
            + str(time.time_ns())[-9:]
        )
        directory = self.root / safe_session
        checkpoint = directory / (stamp + "-" + digest[:12] + ".md")
        container_path = "/opt/data/workspace/" + str(
            checkpoint.relative_to(self.workspace)
        )
        archive = (
            "# AgentReady compaction checkpoint\n\n"
            "- Logical session: " + HOME_SESSION_ID + "\n"
            "- Physical session: " + str(session_id or HOME_SESSION_ID) + "\n"
            "- Created UTC: "
            + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            + "\n"
            "- SHA-256: " + digest + "\n"
            "- Previous checkpoint: " + (previous_checkpoint or "(none)") + "\n"
            "- Estimated tokens before live bounding: " + str(estimate(clean)) + "\n\n"
            "## Full structured checkpoint\n\n" + clean + "\n"
        )
        atomic_write_text(checkpoint, archive)

        index_path = directory / "index.json"
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
        referenced_artifacts = sorted(
            set(re.findall(r"/opt/data/workspace/[^\s\x60\"']+", clean))
        )[:64]
        entries.append({
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sessionId": str(session_id or HOME_SESSION_ID),
            "path": container_path,
            "sha256": digest,
            "characters": len(clean),
            "roughTokens": estimate(clean),
            "referencedArtifacts": referenced_artifacts,
            "previousCheckpoint": previous_checkpoint,
        })
        atomic_write_text(
            index_path,
            json.dumps(entries[-CHECKPOINT_INDEX_LIMIT:], ensure_ascii=False, indent=2)
            + "\n",
        )

        reference = (
            CHECKPOINT_REFERENCE_HEADING + "\n"
            "The full pre-bound checkpoint was atomically offloaded to "
            + container_path
            + ".\n"
            "SHA-256: "
            + digest
            + ". Read it only when exact older detail is required; "
            "follow Previous checkpoint links inside archives for older detail. "
            "The live summary is the default source of continuity."
        )
        return reference
