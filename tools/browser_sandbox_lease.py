"""The sandbox reads only the holder and epoch from its read-only screenshot mount."""

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

VIEW = Path("cache/screenshots/.browser-control/lease.json")


@dataclass(frozen=True)
class BrowserLeaseView:
    holder: str
    epoch: int


def publish(home: Path, view: BrowserLeaseView) -> None:
    path = home / VIEW
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"holder": view.holder, "epoch": view.epoch}, stream)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read() -> BrowserLeaseView:
    try:
        value = json.loads((Path("/root/.hermes") / VIEW).read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("holder") in {"agent", "human"}
                and type(value.get("epoch")) is int and value["epoch"] >= 0):
            return BrowserLeaseView(value["holder"], value["epoch"])
    except (OSError, ValueError):
        pass
    return BrowserLeaseView("human", -1)
