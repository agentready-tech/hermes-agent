"""Policy contracts run without importing Hermes or calling a model."""

import json
from agentready_runtime.checkpoints import (
    CHECKPOINT_REFERENCE_HEADING,
    checkpoint_fits,
    persist_bounded_checkpoint,
)
from agentready_runtime.adapters.checkpoint_files import FileCheckpointArchive
from agentready_runtime.events import combine_event_content, event_prompt


def test_checkpoint_chain_accepts_only_bounded_candidates(tmp_path):
    archive = FileCheckpointArchive(tmp_path / "workspace")
    estimate = lambda text: max(1, len(text) // 3)
    requests = []

    def shrink(prompt):
        requests.append(prompt)
        return "## Active Task\nContinue invoice INV-42."

    first = persist_bounded_checkpoint(
        "home", "Initial request", archive=archive, estimate=estimate
    )
    huge = "## Active Task\n" + ("Important detail. " * 3000).rstrip()
    second = persist_bounded_checkpoint(
        "home",
        huge,
        archive=archive,
        estimate=estimate,
        summarize=shrink,
        previous_summary=first,
    )
    assert checkpoint_fits(second, estimate)
    assert len(requests) == 1 and requests[0].endswith(huge)
    assert second.count(CHECKPOINT_REFERENCE_HEADING) == 1
    entries = json.loads((archive.root / "home/index.json").read_text())
    assert entries[-1]["previousCheckpoint"] == entries[0]["path"]
    full = (
        tmp_path
        / "workspace"
        / entries[-1]["path"].removeprefix("/opt/data/workspace/")
    )
    assert huge in full.read_text()
    try:
        persist_bounded_checkpoint(
            "home",
            huge,
            archive=archive,
            estimate=estimate,
            summarize=lambda _: "",
            previous_summary=second,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Empty recompression must fail")
    persist_bounded_checkpoint(
        "home",
        "Later progress",
        archive=FileCheckpointArchive(archive.workspace),
        estimate=estimate,
        previous_summary=second,
    )
    entries = json.loads((archive.root / "home/index.json").read_text())
    assert entries[-1]["previousCheckpoint"] == entries[1]["path"]
    assert entries[-1]["previousCheckpoint"] != entries[-2]["path"]


def test_events_preserve_metadata_and_multimodal_content():
    prompt = event_prompt("untrusted text", "schedule", "event-42", {"job_id": "job-7"})
    envelope = json.loads(prompt.splitlines()[1])
    assert envelope["event_id"] == "event-42" and envelope["metadata"] == {
        "job_id": "job-7"
    }
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert combine_event_content([prompt, [image]]) == [
        {"type": "text", "text": prompt},
        image,
    ]
