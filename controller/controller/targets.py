"""The objective: one finished node per CPU model listed in targets.csv.

One row per x86 CPU model of Grid5000, with the cluster to run it on (and the other clusters that
have the same model). Edit that file to change what the dashboard counts."""
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("targets.csv")

_NOISE = re.compile(r"\((r|tm)\)|\bcpu\b|\bprocessor\b|\b\d+-core\b|@.*$", re.I)


@dataclass
class Target:
    cpu_model: str
    microarch: str
    cluster: str
    site: str
    nodes: int
    queue: str
    others: list[str] = field(default_factory=list)

    @property
    def abaca(self) -> bool:
        return "abaca" in self.queue


def norm_cpu(name: str) -> str:
    """'Intel(R) Xeon(R) Gold 5220 CPU @ 2.20GHz' -> 'intel xeon gold 5220'."""
    s = " ".join(_NOISE.sub(" ", name or "").lower().split())
    # Sandy Bridge reports e.g. 'E5-2620 0'.
    return s[:-2] if s.endswith(" 0") else s


def load_targets(path=None) -> list[Target]:
    try:
        with open(path or DEFAULT_FILE, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return [Target(r["cpu_model"], r["microarch"], r["cluster"], r["site"], int(r["nodes"]), r["queue"],
                   r["other_clusters"].split())
            for r in rows]


def match(targets: list[Target], cpu_model, cluster):
    """Target a node counts for: by reported CPU model, else by cluster name."""
    n = norm_cpu(cpu_model or "")
    for t in targets:
        if n and norm_cpu(t.cpu_model) == n:
            return t
    for t in targets:
        if cluster and (cluster == t.cluster or cluster in t.others):
            return t
    return None
