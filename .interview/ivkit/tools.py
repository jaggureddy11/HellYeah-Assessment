"""Tool registry for the MCP server.

Two kinds of tools live behind one server:

  built-in     ask_panel, time_left. Come with the platform.
  pack tools   declared by a problem pack in problem.toml, implemented by a
               Python module the pack ships. The platform has no idea what they
               do; it only wires them in and logs every call.

A pack declares tools like this:

    [[tools]]
    name = "query_db"
    description = "Run a read-only SQL query against the exercise database."
    module = "tools/db.py"
    function = "query"
    [tools.input]
    sql = { type = "string", description = "SQL, SELECT only" }
    limit = { type = "integer", description = "max rows, default 200" }

The module exposes one function taking keyword arguments and returning either a
string or a JSON-serialisable value. Anything it raises becomes an error result
the agent can read; it never crashes the server.

Every call, built-in or pack, is appended to .interview/tool_calls.jsonl with a
timestamp, the arguments, a hash of the result and the wall time. For exercises
that sit on real data this log is often worth more than the code: it shows what
was looked at, in what order, and when things were noticed.
"""
from __future__ import annotations
import datetime, hashlib, importlib.util, json, pathlib, time, traceback


class Tool:
    def __init__(self, name, description, schema, fn, source):
        self.name, self.description, self.schema, self.fn, self.source = \
            name, description, schema, fn, source

    def descriptor(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.schema}


class Registry:
    def __init__(self, repo: pathlib.Path):
        self.repo = repo
        self.tools: dict[str, Tool] = {}
        self.log_path = repo / ".interview" / "tool_calls.jsonl"

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def list(self) -> list[dict]:
        return [t.descriptor() for t in self.tools.values()]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (text, is_error). Logs regardless of outcome."""
        t0 = time.monotonic()
        tool = self.tools.get(name)
        if tool is None:
            self._log(name, args, None, True, 0.0, "unknown tool")
            raise KeyError(name)
        try:
            out = tool.fn(**(args or {}))
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False,
                                                                   indent=1, default=str)
            self._log(name, args, text, False, time.monotonic() - t0, None)
            return text, False
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self._log(name, args, None, True, time.monotonic() - t0, err)
            return (f"The tool failed: {err}\n"
                    f"{traceback.format_exc(limit=3)}"), True

    def _log(self, name, args, text, is_error, secs, err):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "tool": name,
                "args": args,
                "ok": not is_error,
                "secs": round(secs, 3),
                # Metadata and a digest only. The result itself may hold rows from
                # a real database; it must not be persisted into the submission.
                "result_bytes": len(text) if text else 0,
                "result_sha256": hashlib.sha256(text.encode()).hexdigest()[:16] if text else None,
                "error": err,
            }
            with self.log_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Logging is best effort. A tool call must never fail because the
            # log could not be written; the result still goes back to the agent.
            pass


# ----------------------------------------------------------------- loading

def _schema_from_toml(spec: dict) -> dict:
    props, required = {}, []
    for k, v in (spec.get("input") or {}).items():
        v = dict(v)
        req = v.pop("required", False)
        props[k] = v
        if req:
            required.append(k)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _load_function(repo: pathlib.Path, module_rel: str, fn_name: str):
    path = (repo / module_rel).resolve()
    if not path.exists():
        raise FileNotFoundError(f"tool module {module_rel} not found in the repo")
    spec = importlib.util.spec_from_file_location(f"packtool_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, fn_name, None)
    if fn is None:
        raise AttributeError(f"{module_rel} has no function {fn_name}")
    return fn


def load_pack_tools(repo: pathlib.Path, registry: Registry, log) -> None:
    """Reads the unsealed problem.json for declared tools and wires them in.
    A broken declaration is reported and skipped; it never takes the server down."""
    meta_path = repo / "problem.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
    except Exception as e:
        log(f"problem.json unreadable: {e}")
        return
    for spec in meta.get("tools") or []:
        try:
            fn = _load_function(repo, spec["module"], spec.get("function", "run"))
            registry.add(Tool(spec["name"], spec.get("description", ""),
                              _schema_from_toml(spec), fn, f"pack:{spec['module']}"))
            log(f"pack tool loaded: {spec['name']}")
        except Exception as e:
            log(f"pack tool {spec.get('name','?')} skipped: {type(e).__name__}: {e}")
