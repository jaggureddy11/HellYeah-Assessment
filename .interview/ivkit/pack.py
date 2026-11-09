"""A problem pack: everything specific to one exercise, in one directory.

The platform knows nothing about any particular problem. It reads `problem.toml`,
seals what the manifest lists, enforces the declared timings, and at assessment
time hands the repository to the evaluator the pack declares. Contributing a new
exercise means writing a directory, not touching this package.

    problems/<name>/
      problem.toml     the manifest below
      BRIEF.md         what the candidate reads at minute 0
      FAQ.md           what the panel oracle is allowed to answer
      data/            whatever the exercise supplies
      eval/            the pack's own evaluation entry point
      internal/        rubric, ground truth. Never sealed, never shipped.

Manifest:

    name = "my-exercise"
    title = "Human-readable title"
    build_minutes = 60
    review_minutes = 0

    brief = "BRIEF.md"
    faq = "FAQ.md"
    sealed = ["BRIEF.md", "FAQ.md", "data"]
    never_ship = ["internal"]

    [deliverable]
    executable = "solution"        # optional
    python_module = "solution.py"  # optional
    python_min = "3.11"

    [evaluate]
    command = "python3 eval/run.py --repo {repo} --seeds {seeds}"

A pack can hand each candidate a world of their own, its data and its hidden
environment, from a list of validated seeds. data/ is the first one's and the
others' are kept, file for file, where [world] says; `build` seals the chosen
one's in place of data/, and `evaluate` passes the one recorded in the
session's schedule as {world}:

    world_seeds = [20261004, 20261206]

    [world]
    data = "internal/worlds/{seed}"
"""
from __future__ import annotations
import collections, json, pathlib, sys

if sys.version_info >= (3, 11):
    import tomllib
else:                                  # 3.9 and 3.10
    try:
        import tomli as tomllib        # type: ignore
    except ImportError:
        tomllib = None                 # type: ignore

REQUIRED = ("name", "title", "build_minutes", "brief", "sealed")


class PackError(RuntimeError):
    pass


def _minimal_toml(text: str) -> dict:
    """A tiny reader for the subset a manifest uses, so a pack can be loaded on
    an interpreter without tomllib and without adding a dependency."""
    out: dict = {}
    section = out
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            arr = out.setdefault(line[2:-2].strip(), [])
            if isinstance(arr, dict):
                arr = out[line[2:-2].strip()] = [arr]
            section = {}
            arr.append(section)
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if "." in name:
                head, tail = name.split(".", 1)
                parent = out.get(head)
                if isinstance(parent, list) and parent:
                    section = parent[-1].setdefault(tail, {})
                else:
                    section = out.setdefault(head, {}).setdefault(tail, {})
            else:
                section = out.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if v.startswith("{") and v.endswith("}"):
            inner = {}
            for part in v[1:-1].split(","):
                if "=" in part:
                    ik, iv = (x.strip() for x in part.split("=", 1))
                    if iv in ("true", "false"):
                        inner[ik] = iv == "true"
                    else:
                        inner[ik] = iv.strip("\"'")
            section[k] = inner
        elif v.startswith("[") and v.endswith("]"):
            items = [i.strip().strip('"\'') for i in v[1:-1].split(",")]
            section[k] = [i for i in items if i]
        elif v.startswith(('"', "'")):
            section[k] = v.strip("\"'")
        else:
            try:
                section[k] = float(v) if "." in v else int(v)
            except ValueError:
                section[k] = v
    return out


class Pack:
    def __init__(self, root: pathlib.Path, meta: dict):
        self.root = root
        self.meta = meta

    @classmethod
    def load(cls, root) -> "Pack":
        root = pathlib.Path(root).resolve()
        f = root / "problem.toml"
        if not f.exists():
            raise PackError(f"no problem.toml in {root}")
        text = f.read_text()
        meta = tomllib.loads(text) if tomllib else _minimal_toml(text)
        missing = [k for k in REQUIRED if k not in meta]
        if missing:
            raise PackError(f"{f} is missing: {', '.join(missing)}")
        for rel in meta["sealed"] + [meta["brief"]]:
            if not (root / rel).exists():
                raise PackError(f"{f} lists {rel}, which does not exist")
        _check_worlds(f, root, meta)
        return cls(root, meta)

    # -- accessors -------------------------------------------------------
    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def title(self) -> str:
        return self.meta["title"]

    @property
    def build_minutes(self) -> float:
        return float(self.meta["build_minutes"])

    @property
    def review_minutes(self) -> float:
        return float(self.meta.get("review_minutes", 0))

    @property
    def sealed(self) -> list[str]:
        return list(self.meta["sealed"])

    @property
    def never_ship(self) -> list[str]:
        return list(self.meta.get("never_ship", ["internal"]))

    @property
    def faq(self) -> pathlib.Path | None:
        f = self.meta.get("faq")
        return self.root / f if f else None

    @property
    def world_seeds(self) -> list[int]:
        """The worlds a candidate can be handed, validated ones only; data/ is the
        first one's. Empty for a pack with one fixed world. The minimal reader
        gives the numbers as strings."""
        return [int(s) for s in self.meta.get("world_seeds") or []]

    @property
    def tools(self) -> list[dict]:
        """Tool declarations. TOML arrays of tables arrive as a list under
        'tools'; the minimal reader stores them as a dict keyed 'tools' when
        there is exactly one, so normalise both shapes."""
        t = self.meta.get("tools") or []
        if isinstance(t, dict):
            t = [t]
        return list(t)

    def world_data(self, seed: int) -> pathlib.Path | None:
        """Where world `seed`'s data is kept: data/ for the first world."""
        if self.world_seeds and int(seed) == self.world_seeds[0]:
            return self.root / "data"
        where = (self.meta.get("world") or {}).get("data")
        return self.root / where.format(seed=int(seed)) if where else None

    def evaluate_command(self, repo: pathlib.Path, seeds: int = 20, world: int | None = None) -> str | None:
        cmd = (self.meta.get("evaluate") or {}).get("command")
        if not cmd:
            return None
        if world is None:
            world = self.world_seeds[0] if self.world_seeds else ""
        return cmd.format(repo=str(repo), seeds=seeds, pack=str(self.root), world=world)

    def describe(self) -> str:
        d = self.meta.get("deliverable") or {}
        ways = []
        if d.get("executable"):
            ways.append(f"an executable `{d['executable']}`")
        if d.get("python_module"):
            ways.append(f"`{d['python_module']}` on Python {d.get('python_min', '3.11')}+")
        review = f", review {self.review_minutes:g} min" if self.review_minutes else ""
        return (f"{self.title} ({self.name})\n"
                f"  build {self.build_minutes:g} min{review}\n"
                f"  deliverable: {' or '.join(ways) if ways else 'declared by the pack'}\n"
                f"  sealed: {', '.join(self.sealed)}")


def _check_worlds(f: pathlib.Path, root: pathlib.Path, meta: dict) -> None:
    """A pack that hands out worlds has to pass the world to its evaluation and
    keep each one's data: otherwise every candidate is sealed one world and
    scored in another, and nothing says so."""
    try:
        seeds = [int(x) for x in meta.get("world_seeds") or []]
    except (TypeError, ValueError):
        raise PackError(f"{f}: world_seeds {meta.get('world_seeds')!r} are not all seeds") from None
    if not seeds:
        return
    if "{world}" not in ((meta.get("evaluate") or {}).get("command") or ""):
        raise PackError(f"{f} lists world_seeds, but its [evaluate] command does not pass {{world}}: "
                        f"every candidate would be scored in one world")
    where = (meta.get("world") or {}).get("data")
    if len(seeds) > 1 and (not where or "{seed}" not in where):
        raise PackError(f"{f} lists world_seeds, but no [world] data = \"...{{seed}}...\" says where their data is")
    for w in seeds[1:]:
        if not (root / where.format(seed=w)).is_dir():
            raise PackError(f"{f}: world {w} has no data at {where.format(seed=w)}")


def choose_world(p: Pack, choice: str, log: pathlib.Path) -> int | None:
    """The world `handover --world <choice>` seals. No choice is the first world,
    the shipped data; `auto` is the one the handover log shows handed out to the
    fewest candidates, the earlier listed on a tie (a candidate handed a second
    kit is one candidate); a seed must be one of the validated ones."""
    seeds = p.world_seeds
    if not seeds:
        if choice:
            raise PackError(f"{p.name} has one fixed world: --world does not apply")
        return None
    if not choice:
        return seeds[0]
    if choice == "auto":
        used = collections.Counter(w for _, w in {(e.get("slug"), e.get("world")) for e in handovers(log)
                                                   if e.get("pack") == p.name})
        return min(seeds, key=lambda s: (used[s], seeds.index(s)))
    try:
        w = int(choice)
    except ValueError:
        raise PackError(f"--world takes a seed or auto, not {choice!r}") from None
    if w not in seeds:
        raise PackError(f"world {w} is not one of {p.name}'s validated world_seeds "
                        f"({', '.join(map(str, seeds))})")
    return w


def handovers(log: pathlib.Path) -> list[dict]:
    """The panel's handover log, one JSON object per line; a line that is not
    one is skipped rather than stopping the next handover."""
    out = []
    try:
        lines = pathlib.Path(log).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def log_handover(log: pathlib.Path, entry: dict) -> None:
    """Appends `entry`, once per (pack, slug, session): a handover re-run for
    the same session is the same handover."""
    log = pathlib.Path(log)
    key = tuple(entry.get(k) for k in ("pack", "slug", "session"))
    if any(tuple(e.get(k) for k in ("pack", "slug", "session")) == key for e in handovers(log)):
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def discover(base) -> list[Pack]:
    base = pathlib.Path(base)
    out = []
    for d in sorted(base.glob("*")):
        if (d / "problem.toml").exists():
            try:
                out.append(Pack.load(d))
            except PackError:
                continue            # not a pack, or a broken one; `packs` lists only the valid ones
    return out
